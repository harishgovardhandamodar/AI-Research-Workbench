"""Shared helpers for the EDA MCP servers: structured results, dtype
inference, column classification, numeric formatting and safe dataset loading.

Kept free of heavy imports at module scope (pandas/numpy are imported lazily
inside functions) so the MCP server processes start fast and only load the
scientific stack when a tool is actually called.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

try:  # numpy is cheap to import and used by several helpers
    import numpy as np  # type: ignore
except Exception:  # pragma: no cover - env without numpy
    np = None  # type: ignore


# ---------------------------------------------------------------- results ----

def ok(**data) -> dict:
    """A successful tool result (plain dict, JSON-serializable)."""
    out: dict[str, Any] = {"ok": True}
    out.update(jsonable(data))
    return out


def err(message: str, recovery: str = "") -> dict:
    """A structured error result with a suggested recovery action."""
    return {"ok": False, "error": message, "recovery": recovery}


def jsonable(obj: Any, depth: int = 0) -> Any:
    """Convert numpy / pandas scalars and other non-JSON types to JSON-safe ones."""
    if obj is None or isinstance(obj, (bool, int, str)):
        return obj
    if isinstance(obj, float):
        if obj != obj:  # NaN
            return None
        return obj
    if isinstance(obj, (np.ndarray, np.generic)) if np is not None else False:
        return jsonable(obj.tolist(), depth + 1)
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, dict):
        return {str(k): jsonable(v, depth + 1) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [jsonable(v, depth + 1) for v in obj]
    try:
        return jsonable(obj.item(), depth + 1)  # numpy scalars
    except Exception:
        return str(obj)


def json_dump(obj: Any, path: Path) -> None:
    """Atomically write JSON to disk (tmp file + rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(jsonable(obj), indent=2, default=str))
    os.replace(tmp, path)


def json_load(path: Path) -> Any:
    if not path.exists():
        return None
    return json.loads(path.read_text())


def fmt_num(v: Any) -> str:
    """Human-friendly number formatting (keeps NaN/None readable)."""
    if v is None or v != v:  # noqa: E712 - NaN check
        return "—"
    if isinstance(v, float):
        if abs(v) >= 1e6 or (abs(v) < 1e-3 and v != 0):
            return f"{v:.3e}"
        return f"{v:,.4f}".rstrip("0").rstrip(".")
    return str(v)


def slugify(name: str) -> str:
    """A filesystem-safe slug from a file name (keeps dataset ids friendly)."""
    base = Path(name).stem.lower()
    keep = "".join(c if c.isalnum() or c in "_-" else "_" for c in base)
    return keep.strip("_")[:48] or "dataset"


def dataset_id_for(path_or_url: str) -> str:
    """Derive a stable dataset_id: slug of the source + short content hash."""
    source = str(path_or_url)
    h = hashlib.sha1(source.encode()).hexdigest()[:8]
    return f"{slugify(source)}_{h}"


def safe_filename(name: str) -> str:
    """Filesystem-safe name for plot/report artifacts."""
    keep = "".join(c if c.isalnum() or c in "_.-" else "_" for c in str(name))
    return keep.strip("_") or "artifact"


# ------------------------------------------------------------- data loading ----

_SUPPORTED_FORMATS = {"auto", "csv", "parquet", "excel", "json"}


def infer_format(path: str) -> str:
    p = str(path).lower().split("?")[0]
    if p.endswith((".parquet", ".pq")):
        return "parquet"
    if p.endswith((".xlsx", ".xls")):
        return "excel"
    if p.endswith(".json"):
        return "json"
    return "csv"


def resolve_source(path_or_url: str) -> tuple[str, bool]:
    """Return (local_path, is_url). URLs are downloaded to a temp cache."""
    lowered = path_or_url.lower()
    if lowered.startswith(("http://", "https://", "ftp://", "file://")):
        parsed = urllib.parse.urlparse(path_or_url)
        name = os.path.basename(parsed.path) or "download"
        cache = Path(tempfile.gettempdir()) / "fox-eda-cache"
        cache.mkdir(parents=True, exist_ok=True)
        dest = cache / (slugify(name) + "_" + hashlib.sha1(path_or_url.encode()).hexdigest()[:8])
        if not dest.exists():
            with urllib.request.urlopen(path_or_url, timeout=60) as resp:
                dest.write_bytes(resp.read())
        return str(dest), True
    return str(Path(path_or_url).expanduser()), False


def read_frame(path_or_url: str, fmt: str = "auto") -> "Any":
    """Load a CSV / Parquet / Excel / JSON file into a pandas DataFrame."""
    import pandas as pd  # lazy

    path, _is_url = resolve_source(path_or_url)
    fmt = fmt or "auto"
    if fmt == "auto":
        fmt = infer_format(path)
    if fmt not in _SUPPORTED_FORMATS:
        raise ValueError(
            f"unsupported format '{fmt}' (expected one of "
            f"{sorted(_SUPPORTED_FORMATS - {'auto'})})")
    if not Path(path).exists():
        raise FileNotFoundError(f"dataset file not found: {path}")
    if fmt == "parquet":
        return pd.read_parquet(path)
    if fmt == "excel":
        return pd.read_excel(path)
    if fmt == "json":
        return pd.read_json(path)
    # CSV: permissive fallbacks for messy public datasets.
    try:
        return pd.read_csv(path, low_memory=False, encoding_errors="replace")
    except Exception:
        return pd.read_csv(path, low_memory=False, encoding_errors="replace",
                           engine="python")


# ------------------------------------------------------- column classification ----

HIGH_CARDINALITY = 500  # above this, a categorical is flagged high-cardinality
ID_LIKE_MAX_UNIQUE = 30   # a column is "id-like" if it's nearly unique


def infer_col_type(series: "Any", n_unique: int, n: int) -> str:
    """A coarse semantic type for a column: numeric / categorical / datetime /
    boolean / text / id / constant."""
    import pandas as pd  # lazy

    if n == 0:
        return "empty"
    if pd.api.types.is_bool_dtype(series) or (n_unique == 2 and set(series.dropna().unique()) <= {True, False, 0, 1, "True", "False", "true", "false"}):
        return "boolean"
    if pd.api.types.is_numeric_dtype(series):
        return "numeric"
    if pd.api.types.is_datetime64_any_dtype(series):
        return "datetime"
    if pd.api.types.is_timedelta64_dtype(series):
        return "numeric"
    if n_unique == 1:
        return "constant"
    if n_unique >= max(2, int(n * 0.95)) and n_unique >= ID_LIKE_MAX_UNIQUE:
        return "id"
    return "categorical"


def split_columns(df: "Any") -> dict[str, list[str]]:
    """Classify dataframe columns into numeric / categorical / datetime / text /
    id / constant / boolean buckets."""
    import pandas as pd  # lazy

    numeric, categorical, datetime_, text, id_, constant, boolean = [], [], [], [], [], [], []
    for col in df.columns:
        series = df[col]
        nun = int(series.nunique(dropna=True))
        n = len(series)
        t = infer_col_type(series, nun, n)
        if t == "numeric":
            numeric.append(str(col))
        elif t == "datetime":
            datetime_.append(str(col))
        elif t == "boolean":
            boolean.append(str(col))
        elif t == "constant":
            constant.append(str(col))
        elif t == "id":
            id_.append(str(col))
        elif t == "empty":
            text.append(str(col))
        else:
            # object/text: high-cardinality strings are "text", the rest categorical
            if nun > 200:
                text.append(str(col))
            else:
                categorical.append(str(col))
    return {
        "numeric": numeric, "categorical": categorical, "datetime": datetime_,
        "text": text, "id": id_, "constant": constant, "boolean": boolean,
    }


def memory_usage_mb(df: "Any") -> float:
    try:
        return float(df.memory_usage(deep=True).sum()) / (1024 * 1024)
    except Exception:
        return 0.0


def ensure_column(df: "Any", column: str):
    """Raise a helpful error (with recovery) when a column is missing."""
    if column not in df.columns:
        cols = ", ".join(map(str, df.columns[:20]))
        raise KeyError(f"column '{column}' not found. Available columns: {cols}")
    return column
