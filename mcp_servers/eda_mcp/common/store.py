"""Shared, disk-backed :class:`DatasetStore`.

The five EDA MCP servers run as *separate processes* (one per server, per the
architecture), so state can only be shared through the filesystem. The store
keeps every loaded dataset in a workspace directory:

    <workspace>/
      index.json                    dataset_id -> {path, meta, created}
      data/<dataset_id>.parquet     cleaned, typed frame
      meta/<dataset_id>.json        schema + profile metadata
      plots/<dataset_id>/...        generated plot artifacts
      reports/...                   compiled reports

``dataset_id`` is the reference the agent passes between servers — no data is
ever re-uploaded. Writes are atomic (tmp file + rename); the index is guarded
by an advisory file lock so concurrent server processes don't corrupt it.
"""

from __future__ import annotations

import contextlib
import os
import time
from pathlib import Path
from typing import Any

from .. import EDA_WORKSPACE_ENV
from . import utils

DEFAULT_WORKSPACE = "~/.fox/eda"


def workspace_dir() -> Path:
    return Path(os.environ.get(EDA_WORKSPACE_ENV, DEFAULT_WORKSPACE)).expanduser()


@contextlib.contextmanager
def _index_lock(index_path: Path):
    """Best-effort advisory lock (fcntl on POSIX) around index mutations."""
    try:
        import fcntl
        fh = index_path.with_suffix(".lock").open("a")
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
    except Exception:  # pragma: no cover - non-POSIX or missing fcntl
        yield
        return
    try:
        yield
    finally:
        with contextlib.suppress(Exception):
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            fh.close()


class DatasetStore:
    """Loads datasets and keeps their frames + metadata addressable by id."""

    def __init__(self, root: str | Path | None = None):
        self.root = Path(root) if root else workspace_dir()
        self.data_dir = self.root / "data"
        self.meta_dir = self.root / "meta"
        self.plots_dir = self.root / "plots"
        self.reports_dir = self.root / "reports"
        self._cache: dict[str, Any] = {}
        for d in (self.data_dir, self.meta_dir, self.plots_dir, self.reports_dir):
            d.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------ index ----
    def _index_path(self) -> Path:
        return self.root / "index.json"

    def _read_index(self) -> dict:
        return utils.json_load(self._index_path()) or {}

    def _write_index(self, index: dict) -> None:
        utils.json_dump(index, self._index_path())

    def list_datasets(self) -> list[dict]:
        idx = self._read_index()
        return [
            {"dataset_id": k, **{kk: vv for kk, vv in v.items() if kk != "meta"}}
            for k, v in sorted(idx.items())
        ]

    # ------------------------------------------------------------- load ----
    def load(self, path_or_url: str | "Any", fmt: str = "auto",
             dataset_id: str | None = None) -> dict:
        """Load a dataset (path, URL or an already-built DataFrame), register it
        in the workspace and return an overview."""
        if hasattr(path_or_url, "to_parquet"):  # already a DataFrame
            df = path_or_url
            source = "<dataframe>"
        else:
            df = utils.read_frame(path_or_url, fmt)
            source = str(path_or_url)
        did = dataset_id or utils.dataset_id_for(source)
        did = utils.slugify(did)
        frame_path = self.data_dir / f"{did}.parquet"
        try:
            df.to_parquet(frame_path, index=False)
        except Exception:
            # pyarrow may be missing: persist to a CSV fallback
            frame_path = self.data_dir / f"{did}.csv"
            df.to_csv(frame_path, index=False)
        meta = self._schema_meta(df)
        meta.update({
            "dataset_id": did,
            "source": source,
            "created_at": time.time(),
            "frame_file": frame_path.name,
            "is_url": source.lower().startswith(("http://", "https://")),
        })
        utils.json_dump(meta, self.meta_dir / f"{did}.json")
        with _index_lock(self._index_path()):
            idx = self._read_index()
            idx[did] = {"source": source, "meta_file": f"{did}.json",
                        "created_at": meta["created_at"]}
            self._write_index(idx)
        self._cache[did] = df
        return self.overview(did, df)

    # ------------------------------------------------------------ access ----
    def get(self, dataset_id: str) -> Any:
        """Return the cached DataFrame, loading from parquet if needed."""
        did = utils.slugify(dataset_id)
        if did in self._cache:
            return self._cache[did]
        frame_path = self.data_dir / f"{did}.parquet"
        if frame_path.exists():
            import pandas as pd  # lazy

            df = pd.read_parquet(frame_path)
        else:
            csv_path = self.data_dir / f"{did}.csv"
            if not csv_path.exists():
                raise FileNotFoundError(
                    f"unknown dataset_id '{did}'. Load a dataset first with "
                    f"load_dataset, or use a valid dataset_id from "
                    f"list_datasets: {', '.join(d['dataset_id'] for d in self.list_datasets())}")
            import pandas as pd  # lazy

            df = pd.read_csv(csv_path)
        self._cache[did] = df
        return df

    def get_meta(self, dataset_id: str) -> dict:
        did = utils.slugify(dataset_id)
        meta = utils.json_load(self.meta_dir / f"{did}.json")
        if meta is None:
            raise FileNotFoundError(
                f"unknown dataset_id '{did}' (no metadata found). Load it first "
                f"with load_dataset, or run list_datasets for valid ids.")
        return meta

    # ---------------------------------------------------------- schema ----
    def _schema_meta(self, df: Any) -> dict:
        import pandas as pd  # lazy

        rows, cols = df.shape
        col_meta = []
        for col in df.columns:
            s = df[col]
            nun = int(s.nunique(dropna=True))
            nulls = int(s.isna().sum())
            inferred = utils.infer_col_type(s, nun, rows)
            dtype = str(s.dtype)
            sample = s.dropna().astype(object).tolist()[:3]
            col_meta.append({
                "name": str(col),
                "dtype": dtype,
                "type": inferred,
                "null_count": nulls,
                "null_pct": round(100.0 * nulls / rows, 2) if rows else 0.0,
                "unique_count": nun,
                "sample": sample,
            })
        return {
            "shape": [rows, cols],
            "rows": rows,
            "columns": cols,
            "memory_mb": round(utils.memory_usage_mb(df), 2),
            "columns_meta": col_meta,
            "col_types": utils.split_columns(df),
        }

    def overview(self, dataset_id: str, df: Any = None) -> dict:
        df = df if df is not None else self.get(dataset_id)
        meta = self.get_meta(dataset_id)
        return utils.ok(
            dataset_id=meta["dataset_id"],
            source=meta.get("source"),
            shape=list(df.shape),
            rows=int(df.shape[0]),
            columns=int(df.shape[1]),
            memory_mb=meta.get("memory_mb"),
            column_types=meta.get("col_types"),
            sample_rows=df.head(5).astype(object).to_dict(orient="records"),
        )

    # ------------------------------------------------------------ plots ----
    def plots_dir_for(self, dataset_id: str) -> Path:
        d = self.plots_dir / utils.slugify(dataset_id)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def reports_dir_for(self) -> Path:
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        return self.reports_dir
