"""MCP Server 2: eda-univariate.

Deep single-variable analysis: numeric statistics, categorical value counts,
missing-data patterns and an overall distribution summary. Run standalone:

    python -m mcp_servers.eda_mcp.univariate
"""

from __future__ import annotations

import json
import math

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from .common.store import DatasetStore
from .common import utils

mcp = MCPServer("eda-univariate", version="0.1.0")
RO = ToolAnnotations(read_only_hint=True)
_STORE = DatasetStore()


def _err(e: Exception) -> str:
    rec = ""
    if isinstance(e, KeyError):
        rec = "List available columns with get_schema, then retry with a valid column name."
    elif isinstance(e, FileNotFoundError):
        rec = "Load the dataset first with load_dataset (or run list_datasets for valid ids)."
    return json.dumps(utils.err(str(e), recovery=rec), default=str)


def _numeric(series) -> "Any":
    import pandas as pd  # lazy

    return pd.to_numeric(series, errors="coerce")


def numeric_summary(store: DatasetStore, dataset_id: str, column: str) -> dict:
    import numpy as np  # lazy
    import pandas as pd  # lazy
    from scipy import stats  # lazy

    df = store.get(dataset_id)
    utils.ensure_column(df, column)
    s = _numeric(df[column]).dropna()
    out = {"column": column, "n": int(len(s)), "missing": int(df[column].isna().sum())}
    if len(s) == 0:
        out["note"] = "column has no non-null numeric values"
        return out
    mean = float(s.mean())
    std = float(s.std(ddof=1)) if len(s) > 1 else 0.0
    q = [float(s.quantile(x)) for x in (0.0, 0.25, 0.5, 0.75, 1.0)]
    iqr = q[3] - q[1]
    out.update({
        "mean": mean, "median": q[2], "std": std,
        "min": q[0], "q1": q[1], "q3": q[3], "max": q[4],
        "iqr": iqr,
        "skew": float(stats.skew(s)) if len(s) > 2 else 0.0,
        "kurtosis": float(stats.kurtosis(s)) if len(s) > 2 else 0.0,
        "variance": float(s.var(ddof=1)) if len(s) > 1 else 0.0,
    })
    lo, hi = q[1] - 1.5 * iqr, q[3] + 1.5 * iqr
    outlier = s[(s < lo) | (s > hi)]
    out["iqr_outliers"] = {"count": int(len(outlier)), "pct": round(100 * len(outlier) / len(s), 2)}
    out["quantiles"] = {f"q{int(x * 100)}": float(s.quantile(x)) for x in (0.05, 0.1, 0.9, 0.95)}
    # histogram data (10-40 bins, Sturges rule capped)
    nb = min(40, max(10, int(math.ceil(math.log2(len(s)) + 1))))
    counts, edges = np.histogram(s, bins=nb)
    out["histogram"] = {
        "bin_edges": [float(e) for e in edges],
        "counts": [int(c) for c in counts],
    }
    # normality note (bounded sample for the test)
    if len(s) >= 8:
        try:
            sample = s.sample(min(5000, len(s)), random_state=42)
            if len(sample) < 5000:
                p = float(stats.shapiro(sample).pvalue)
                test = "Shapiro-Wilk"
            else:
                p = float(stats.normaltest(sample).pvalue)
                test = "D'Agostino"
            out["normality"] = {
                "test": test, "p_value": p,
                "verdict": "likely normal" if p > 0.05 else "deviates from normal",
            }
        except Exception:
            pass
    return out


def categorical_summary(store: DatasetStore, dataset_id: str, column: str,
                        top_n: int = 20) -> dict:
    import math  # lazy

    df = store.get(dataset_id)
    utils.ensure_column(df, column)
    s = df[column].dropna()
    total = int(s.shape[0])
    vc = s.value_counts(dropna=False)
    top = vc.head(top_n)
    out = {
        "column": column,
        "non_null": total,
        "missing": int(df[column].isna().sum()),
        "cardinality": int(vc.shape[0]),
        "mode": str(vc.index[0]) if len(vc) else None,
        "top": [
            {"value": str(k), "count": int(v), "pct": round(100 * v / total, 2) if total else 0.0}
            for k, v in top.items()
        ],
    }
    if total:
        # Shannon entropy in bits over observed non-null values
        p = s.value_counts(normalize=True)
        ent = -float((p * p.map(math.log2)).sum()) if (p > 0).any() else 0.0
        out["entropy_bits"] = round(ent, 3)
        out["max_entropy_bits"] = round(math.log2(max(1, out["cardinality"])), 3)
        rare = vc[vc < max(1, int(total * 0.01))]
        out["rare_categories"] = {"count": int(len(rare)), "share": round(100 * len(rare) / len(vc), 2) if len(vc) else 0.0}
        out["dominate_top1_pct"] = round(100 * vc.iloc[0] / total, 2) if len(vc) else 0.0
    return out


def missing_patterns(store: DatasetStore, dataset_id: str) -> dict:
    import pandas as pd  # lazy
    import numpy as np  # lazy

    df = store.get(dataset_id)
    meta = store.get_meta(dataset_id)
    rows = meta["rows"]
    per_col = [
        {"column": c["name"], "missing": c["null_count"],
         "pct": round(c["null_pct"], 2)}
        for c in meta.get("columns_meta", []) if c["null_count"]
    ]
    per_col.sort(key=lambda x: -x["missing"])
    # missingness correlation (phi) for top missing columns
    corr = []
    miss = df.isna()
    cols = [c["name"] for c in meta.get("columns_meta", []) if c["null_count"]][:8]
    for i in range(len(cols)):
        for j in range(i + 1, len(cols)):
            a = miss[cols[i]].astype(int)
            b = miss[cols[j]].astype(int)
            both = float((a * b).sum())
            if both == 0:
                continue
            n = len(a)
            phi = (both - a.sum() * b.sum() / n) / math.sqrt(
                max(1e-9, a.sum() * (n - a.sum()) * b.sum() * (n - b.sum())))
            if abs(phi) >= 0.3:
                corr.append({"col_a": cols[i], "col_b": cols[j],
                             "phi": round(float(phi), 3),
                             "both_missing": int(both)})
    # combination patterns: rows missing in 2+ columns
    combo = {}
    mask = miss.sum(axis=1)
    sub = miss[mask >= 2]
    for _, row in sub.iterrows():
        key = tuple(sorted(row.index[row]))
        combo[key] = combo.get(key, 0) + 1
    top_combos = [
        {"columns": list(k), "rows": v, "pct": round(100 * v / rows, 2) if rows else 0.0}
        for k, v in sorted(combo.items(), key=lambda kv: -kv[1])[:10]
    ]
    return {
        "dataset_id": meta["dataset_id"],
        "rows": rows,
        "columns_with_missing": per_col,
        "missingness_correlation": corr,
        "row_completion": {
            "rows_complete": int((mask == 0).sum()),
            "rows_any_missing": int((mask > 0).sum()),
            "rows_multiple_missing": int((mask >= 2).sum()),
        },
        "top_missing_combos": top_combos,
    }


def distribution_summary_data(store: DatasetStore, dataset_id: str) -> dict:
    meta = store.get_meta(dataset_id)
    types = meta.get("col_types", {})
    df = store.get(dataset_id)
    numeric, notes = [], []
    for c in types.get("numeric", []):
        try:
            num = numeric_summary(store, dataset_id, c)
            notes.append({
                "column": c, "skew": num.get("skew"), "n": num.get("n"),
                "outlier_pct": (num.get("iqr_outliers") or {}).get("pct"),
            })
        except Exception:
            pass
    cat = []
    for c in types.get("categorical", [])[:40]:
        try:
            cs = categorical_summary(store, dataset_id, c, top_n=5)
            cat.append({"column": c, "cardinality": cs["cardinality"],
                        "top1_pct": cs.get("dominate_top1_pct"),
                        "missing_pct": round(100 * cs["missing"] / max(1, cs["non_null"] + cs["missing"]), 2)})
        except Exception:
            pass
    skewed = sorted([n for n in notes if abs(n.get("skew") or 0) > 1.5],
                    key=lambda x: -abs(x.get("skew") or 0))[:8]
    return {
        "dataset_id": meta["dataset_id"],
        "numeric_columns": len(types.get("numeric", [])),
        "categorical_columns": len(types.get("categorical", [])),
        "notably_skewed": skewed,
        "categorical_overview": cat,
    }


# ------------------------------------------------------------------- tools ----

@mcp.tool(annotations=RO)
def univariate_numeric(dataset_id: str, column: str) -> str:
    """Deep numeric analysis of one column: mean/median/std, skew, kurtosis,
    quantiles, IQR outliers, histogram data and a normality check."""
    try:
        return json.dumps(utils.ok(**numeric_summary(_STORE, dataset_id, column)), default=str)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool(annotations=RO)
def univariate_categorical(dataset_id: str, column: str, top_n: int = 20) -> str:
    """Categorical analysis of one column: value counts, proportions, mode,
    Shannon entropy and rare-category statistics."""
    try:
        return json.dumps(utils.ok(**categorical_summary(_STORE, dataset_id, column, top_n)), default=str)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool(annotations=RO)
def missing_analysis(dataset_id: str) -> str:
    """Missing-data patterns: per-column counts, correlated missingness (phi),
    and rows missing in multiple columns at once."""
    try:
        return json.dumps(utils.ok(**missing_patterns(_STORE, dataset_id)), default=str)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool(annotations=RO)
def distribution_summary(dataset_id: str) -> str:
    """Overall distribution diagnostics for all numeric and categorical columns."""
    try:
        return json.dumps(utils.ok(**distribution_summary_data(_STORE, dataset_id)), default=str)
    except Exception as e:  # noqa: BLE001
        return _err(e)


if __name__ == "__main__":
    mcp.run(transport="stdio")
