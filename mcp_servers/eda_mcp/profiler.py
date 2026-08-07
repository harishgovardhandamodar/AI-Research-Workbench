"""MCP Server 1: eda-data-profiler.

Safe loading, schema inference and basic quality checks. Run standalone (stdio):

    python -m mcp_servers.eda_mcp.profiler

Everything returns JSON strings the agent can reason over. Heavy imports
(pandas) happen lazily so the process starts fast.
"""

from __future__ import annotations

import json

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from .common.store import DatasetStore
from .common import utils

mcp = MCPServer("eda-data-profiler", version="0.1.0")
RO = ToolAnnotations(read_only_hint=True)
_STORE = DatasetStore()


def _err(e: Exception) -> str:
    rec = ""
    if isinstance(e, KeyError):
        rec = "List available columns with get_schema, then retry with a valid column name."
    elif isinstance(e, FileNotFoundError):
        rec = "Load the dataset first with load_dataset (or run list_datasets for valid ids)."
    return json.dumps(utils.err(str(e), recovery=rec), default=str)


# ------------------------------------------------------------------- schema ----

def schema_data(store: DatasetStore, dataset_id: str) -> dict:
    df = store.get(dataset_id)
    meta = store.get_meta(dataset_id)
    cols = [
        {
            "name": c["name"], "dtype": c["dtype"], "type": c["type"],
            "null_count": c["null_count"], "null_pct": c["null_pct"],
            "unique_count": c["unique_count"], "sample": c["sample"],
        }
        for c in meta.get("columns_meta", [])
    ]
    return {
        "dataset_id": meta["dataset_id"],
        "rows": meta["rows"], "columns": meta["columns"],
        "memory_mb": meta.get("memory_mb"),
        "column_types": meta.get("col_types"),
        "columns": cols,
    }


def profile_data(store: DatasetStore, dataset_id: str) -> dict:
    df = store.get(dataset_id)
    meta = store.get_meta(dataset_id)
    rows, cols = df.shape
    cols_meta = meta.get("columns_meta", [])
    by_name = {c["name"]: c for c in cols_meta}
    constant, high_card, id_like = [], [], []
    for c in cols_meta:
        if c["type"] == "constant":
            constant.append(c["name"])
        if c["type"] == "id" or c["unique_count"] >= max(2, int(rows * 0.95)):
            id_like.append(c["name"])
        if c["type"] == "categorical" and c["unique_count"] > utils.HIGH_CARDINALITY:
            high_card.append({"column": c["name"], "unique_count": c["unique_count"]})
    dup_rows = int(df.duplicated().sum()) if rows else 0
    # target candidates: numeric columns with low-ish missing and variance
    target_cands = []
    for c in cols_meta:
        if c["type"] != "numeric":
            continue
        s = df[c["name"]]
        try:
            sd = float(s.std())
        except Exception:
            sd = 0.0
        if c["null_pct"] < 20 and sd > 1e-12:
            target_cands.append(c["name"])
    return {
        "dataset_id": meta["dataset_id"],
        "shape": [rows, cols],
        "memory_mb": meta.get("memory_mb"),
        "duplicate_rows": dup_rows,
        "missing_summary": {
            "columns_with_missing": [c["name"] for c in cols_meta if c["null_count"]],
            "worst": sorted(cols_meta, key=lambda c: -c["null_pct"])[:5],
        },
        "constant_columns": constant,
        "high_cardinality_columns": high_card,
        "potential_id_columns": id_like[:20],
        "target_candidates": target_cands[:20],
    }


def quality_issues_data(store: DatasetStore, dataset_id: str) -> dict:
    df = store.get(dataset_id)
    meta = store.get_meta(dataset_id)
    rows, cols = df.shape
    cols_meta = meta.get("columns_meta", [])
    by_name = {c["name"]: c for c in cols_meta}
    issues: list[dict] = []

    if rows == 0:
        issues.append({"severity": "critical", "type": "empty_dataset",
                       "column": "*", "message": "Dataset has zero rows.",
                       "suggestion": "Verify the source file."})

    dup = int(df.duplicated().sum()) if rows else 0
    if dup:
        issues.append({"severity": "high", "type": "duplicate_rows", "column": "*",
                       "message": f"{dup} duplicate row(s) ({100 * dup / rows:.1f}%).",
                       "suggestion": "Consider drop_duplicates() before analysis."})

    for c in cols_meta:
        if c["null_pct"] > 30:
            issues.append({"severity": "high", "type": "high_missing", "column": c["name"],
                           "message": f"{c['null_pct']:.1f}% missing.",
                           "suggestion": "Impute or drop; check missingness causes."})
        if c["type"] == "constant":
            issues.append({"severity": "low", "type": "constant_column",
                           "column": c["name"],
                           "message": "Single unique value; adds no signal.",
                           "suggestion": "Drop for modeling."})
        if c["type"] == "id":
            issues.append({"severity": "low", "type": "id_column", "column": c["name"],
                           "message": "High-cardinality unique-like identifier.",
                           "suggestion": "Exclude from modeling features."})

    # mixed-type detection + extreme numeric outlier candidates
    numeric_cols = meta.get("col_types", {}).get("numeric", [])
    import pandas as pd  # lazy

    for c in numeric_cols:
        s = pd.to_numeric(df[c], errors="coerce")
        vals = s.dropna()
        if len(vals) < 2:
            continue
        try:
            q1, q3 = float(vals.quantile(0.25)), float(vals.quantile(0.75))
            iqr = q3 - q1
            lo, hi = q1 - 3 * iqr, q3 + 3 * iqr
            n_out = int(((vals < lo) | (vals > hi)).sum())
            if n_out and n_out / len(vals) > 0.02:
                issues.append({
                    "severity": "medium", "type": "extreme_outliers",
                    "column": c,
                    "message": f"{n_out} extreme values ({100 * n_out / len(vals):.1f}%) "
                               f"outside ±3×IQR [{utils.fmt_num(lo)}, {utils.fmt_num(hi)}].",
                    "suggestion": "Investigate with a box plot before imputing/clipping."})
        except Exception:
            pass
    return {"dataset_id": meta["dataset_id"], "issues": issues}


# ------------------------------------------------------------------- tools ----

@mcp.tool()
def load_dataset(path_or_url: str, format: str = "auto") -> str:
    """Load a dataset (local path or URL) from CSV/Parquet/Excel/JSON and register
    it in the shared workspace. Returns dataset_id, shape, columns, types, sample
    rows and memory usage. Pass dataset_id to every other tool."""
    try:
        return json.dumps(_STORE.load(path_or_url, format), default=str)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool(annotations=RO)
def list_datasets() -> str:
    """List datasets already loaded in the shared workspace (dataset_id + source)."""
    try:
        return json.dumps(utils.ok(datasets=_STORE.list_datasets()), default=str)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool(annotations=RO)
def get_schema(dataset_id: str) -> str:
    """Detailed schema for a loaded dataset: per-column dtype, inferred type,
    null counts, unique counts and sample values."""
    try:
        return json.dumps(utils.ok(**schema_data(_STORE, dataset_id)), default=str)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool(annotations=RO)
def profile_basic(dataset_id: str) -> str:
    """Basic profile: missing %, unique counts, constant columns,
    high-cardinality flags, potential ID columns and target candidates."""
    try:
        return json.dumps(utils.ok(**profile_data(_STORE, dataset_id)), default=str)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool(annotations=RO)
def detect_data_quality_issues(dataset_id: str) -> str:
    """List data-quality issues: duplicates, high missingness, constant columns,
    ID columns and extreme-outlier candidates, each with a suggestion."""
    try:
        return json.dumps(utils.ok(**quality_issues_data(_STORE, dataset_id)), default=str)
    except Exception as e:  # noqa: BLE001
        return _err(e)


if __name__ == "__main__":
    mcp.run(transport="stdio")
