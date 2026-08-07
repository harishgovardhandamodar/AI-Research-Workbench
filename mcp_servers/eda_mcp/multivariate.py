"""MCP Server 3: eda-multivariate.

Relationships and structure: correlation matrices, pairwise plot data, target
associations, PCA and a clustering preview. Run standalone:

    python -m mcp_servers.eda_mcp.multivariate
"""

from __future__ import annotations

import json

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from .common.store import DatasetStore
from .common import utils

mcp = MCPServer("eda-multivariate", version="0.1.0")
RO = ToolAnnotations(read_only_hint=True)
_STORE = DatasetStore()


def _err(e: Exception) -> str:
    rec = ""
    if isinstance(e, (KeyError, ValueError)):
        rec = "Check column names with get_schema; only numeric columns support correlation/PCA."
    elif isinstance(e, FileNotFoundError):
        rec = "Load the dataset first with load_dataset (or run list_datasets for valid ids)."
    return json.dumps(utils.err(str(e), recovery=rec), default=str)


def _numeric_frame(store: DatasetStore, dataset_id: str, columns=None):
    df = store.get(dataset_id)
    meta = store.get_meta(dataset_id)
    if columns:
        for c in columns:
            utils.ensure_column(df, c)
        return df[list(columns)].apply(pd_numeric)
    num = meta.get("col_types", {}).get("numeric", [])
    if not num:
        raise ValueError("no numeric columns available for this analysis")
    return df[num].apply(pd_numeric)


def pd_numeric(series) -> "Any":
    import pandas as pd  # lazy

    return pd.to_numeric(series, errors="coerce")


def correlation_data(store: DatasetStore, dataset_id: str, method: str = "pearson",
                     columns=None) -> dict:
    import numpy as np  # lazy

    frame = _numeric_frame(store, dataset_id, columns)
    if frame.shape[1] < 2:
        raise ValueError("need at least two numeric columns for correlations")
    corr = frame.corr(method=method).round(3)
    names = list(frame.columns)
    mat = [[corr.loc[a, b] for b in names] for a in names]
    pairs = []
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            v = corr.loc[names[i], names[j]]
            if abs(v) >= 0.5:
                pairs.append({"col_a": names[i], "col_b": names[j],
                              "value": float(v), "strength": "strong" if abs(v) >= 0.7 else "moderate"})
    pairs.sort(key=lambda p: -abs(p["value"]))
    # multicollinearity clusters (|r| > 0.8)
    clusters = [
        {"col_a": names[i], "col_b": names[j], "value": float(corr.loc[names[i], names[j]])}
        for i in range(len(names)) for j in range(i + 1, len(names))
        if abs(corr.loc[names[i], names[j]]) > 0.8
    ]
    return {
        "dataset_id": store.get_meta(dataset_id)["dataset_id"],
        "method": method,
        "columns": names,
        "matrix": mat,
        "significant_pairs": pairs,
        "potential_multicollinearity": clusters,
    }


def pairwise_plots_data(store: DatasetStore, dataset_id: str, columns) -> dict:
    df = store.get(dataset_id)
    if not columns:
        raise ValueError("columns is required for pairwise_plots_data")
    frame = df[list(columns)].copy()
    num_cols = [c for c in columns if frame[c].dtype.kind in "fiuf"]
    cat_cols = [c for c in columns if c not in num_cols]
    # cap the payload so scatter/hexbin data stays manageable for the agent
    sample = frame
    if len(sample) > 2000:
        sample = sample.sample(2000, random_state=42)
    num_summary = {}
    for c in num_cols:
        try:
            from .univariate import numeric_summary

            num_summary[c] = numeric_summary(store, dataset_id, c)
        except Exception:
            pass
    return {
        "dataset_id": store.get_meta(dataset_id)["dataset_id"],
        "numeric_columns": num_cols,
        "categorical_columns": cat_cols,
        "sampled_rows": int(len(sample)),
        "points": sample.astype(object).to_dict(orient="records"),
        "numeric_summary": num_summary,
    }


def target_relations_data(store: DatasetStore, dataset_id: str, target: str) -> dict:
    import numpy as np  # lazy
    import pandas as pd  # lazy
    from scipy import stats  # lazy

    df = store.get(dataset_id)
    utils.ensure_column(df, target)
    meta = store.get_meta(dataset_id)
    types = meta.get("col_types", {})
    t_series = pd.to_numeric(df[target], errors="coerce")
    is_numeric_target = t_series.notna().sum() / max(1, len(df)) > 0.9
    numeric_assoc, cat_assoc = [], []

    if is_numeric_target:
        y = t_series.fillna(t_series.median())
        for c in types.get("numeric", []):
            if c == target:
                continue
            x = pd.to_numeric(df[c], errors="coerce").fillna(
                pd.to_numeric(df[c], errors="coerce").median())
            if x.notna().sum() < 2:
                continue
            r, p = stats.pearsonr(x, y)
            numeric_assoc.append({"column": c, "pearson_r": round(float(r), 3),
                                  "p_value": float(p)})
        numeric_assoc.sort(key=lambda a: -abs(a["pearson_r"]))
        # point-biserial vs boolean / 2-level categoricals
        for c in types.get("boolean", []) + [
            cc for cc in types.get("categorical", [])
            if int(df[cc].nunique(dropna=True)) == 2]:
            if c == target:
                continue
            s = df[c].dropna()
            y_s = y[s.index]
            grp = s.astype(str).map(lambda v: int(v in ("True", "true", "1", "1.0")))
            if len(set(grp)) < 2:
                continue
            try:
                r, p = stats.pointbiserialr(grp, y_s)
                cat_assoc.append({"column": c, "point_biserial": round(float(r), 3),
                                  "p_value": float(p)})
            except Exception:
                pass
        cat_assoc.sort(key=lambda a: -abs(a["point_biserial"]))
    else:
        y = df[target].astype(str).fillna("(missing)")
        for c in types.get("numeric", []):
            if c == target:
                continue
            x = pd.to_numeric(df[c], errors="coerce")
            try:
                f, p = stats.f_oneway(*[x[y == k].dropna() for k in y.unique() if len(x[y == k].dropna()) > 1])
                numeric_assoc.append({"column": c, "anova_f": round(float(f), 3),
                                      "p_value": float(p)})
            except Exception:
                pass
        numeric_assoc.sort(key=lambda a: a["p_value"])
        for c in types.get("categorical", []):
            if c == target:
                continue
            try:
                ct = pd.crosstab(df[c].astype(str).fillna("(missing)"), y)
                chi2 = stats.chi2_contingency(ct)
                cramer = float(np.sqrt(chi2[0] / (chi2[1][0] * min(ct.shape) - 1))) if min(ct.shape) > 1 else 0.0
                cat_assoc.append({"column": c, "cramers_v": round(cramer, 3),
                                  "chi2_p": float(chi2[1])})
            except Exception:
                pass
        cat_assoc.sort(key=lambda a: -a["cramers_v"])
    return {
        "dataset_id": meta["dataset_id"],
        "target": target,
        "target_type": "numeric" if is_numeric_target else "categorical",
        "top_numeric_associations": numeric_assoc[:15],
        "top_categorical_associations": cat_assoc[:15],
    }


def pca_data(store: DatasetStore, dataset_id: str, n_components: int = 5) -> dict:
    import numpy as np  # lazy
    from sklearn.decomposition import PCA  # lazy
    from sklearn.impute import SimpleImputer  # lazy
    from sklearn.preprocessing import StandardScaler  # lazy

    frame = _numeric_frame(store, dataset_id)
    if frame.shape[1] < 2:
        raise ValueError("PCA needs at least two numeric columns")
    X = SimpleImputer(strategy="median").fit_transform(frame.to_numpy())
    X = StandardScaler().fit_transform(X)
    k = min(n_components, X.shape[1], X.shape[0])
    pca = PCA(n_components=k)
    pca.fit(X)
    loadings = []
    for i, col in enumerate(frame.columns):
        loadings.append({"column": col, "loadings": [round(float(v), 3) for v in pca.components_[:, i]]})
    return {
        "dataset_id": store.get_meta(dataset_id)["dataset_id"],
        "n_components_used": k,
        "explained_variance_ratio": [round(float(v), 4) for v in pca.explained_variance_ratio_],
        "cumulative_variance": [round(float(v), 4) for v in np.cumsum(pca.explained_variance_ratio_)],
        "loadings": loadings,
        "note": "columns standardized; missing values median-imputed",
    }


def clustering_preview(store: DatasetStore, dataset_id: str, method: str = "kmeans",
                       max_k: int = 8) -> dict:
    import numpy as np  # lazy
    from sklearn.cluster import KMeans  # lazy
    from sklearn.impute import SimpleImputer  # lazy
    from sklearn.metrics import silhouette_score  # lazy
    from sklearn.preprocessing import StandardScaler  # lazy

    frame = _numeric_frame(store, dataset_id)
    if frame.shape[1] < 2 or frame.shape[0] < max_k + 2:
        raise ValueError("clustering preview needs at least 2 numeric columns and "
                         "more rows than clusters")
    X = SimpleImputer(strategy="median").fit_transform(frame.to_numpy())
    X = StandardScaler().fit_transform(X)
    kmax = min(max_k, X.shape[0] - 2, 10)
    scores = []
    for k in range(2, kmax + 1):
        km = KMeans(n_clusters=k, n_init=5, random_state=42).fit(X)
        try:
            sil = float(silhouette_score(X, km.labels_))
        except Exception:
            sil = None
        scores.append({"k": k, "silhouette": sil,
                       "inertia": round(float(km.inertia_), 1)})
    valid = [s for s in scores if s["silhouette"] is not None]
    best_k = max(valid, key=lambda s: s["silhouette"])["k"] if valid else None
    out = {"dataset_id": store.get_meta(dataset_id)["dataset_id"],
           "method": method, "max_k": kmax, "scores": scores, "best_k": best_k}
    if best_k:
        km = KMeans(n_clusters=best_k, n_init=5, random_state=42).fit(X)
        frame2 = frame.copy()
        frame2["cluster"] = km.labels_
        sizes = frame2["cluster"].value_counts().sort_index()
        top_cols = frame.columns[:5]
        out["cluster_sizes"] = {f"cluster_{int(k)}": int(v) for k, v in sizes.items()}
        out["cluster_means"] = {
            f"cluster_{int(k)}": {c: round(float(v), 3) for c, v in
                                  frame2[frame2["cluster"] == k][list(top_cols)].mean().items()}
            for k in sorted(sizes.index)
        }
    return out


# ------------------------------------------------------------------- tools ----

@mcp.tool(annotations=RO)
def correlation_matrix(dataset_id: str, method: str = "pearson", columns: list = None) -> str:
    """Correlation matrix for numeric columns (pearson/spearman), with significant
    pairs and potential multicollinearity clusters."""
    try:
        return json.dumps(utils.ok(**correlation_data(_STORE, dataset_id, method, columns)), default=str)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool(annotations=RO)
def pairwise_plots_data(dataset_id: str, columns: list) -> str:
    """Sampled points + summaries for scatter / hexbin / box plots between the
    given columns (numeric + categorical)."""
    try:
        return json.dumps(utils.ok(**pairwise_plots_data(_STORE, dataset_id, columns)), default=str)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool(annotations=RO)
def target_relationships(dataset_id: str, target: str) -> str:
    """Association of every other column with a target: Pearson / point-biserial
    for numeric targets, ANOVA / Cramér's V for categorical targets."""
    try:
        return json.dumps(utils.ok(**target_relations_data(_STORE, dataset_id, target)), default=str)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool(annotations=RO)
def pca_summary(dataset_id: str, n_components: int = 5) -> str:
    """PCA on standardized numeric columns: explained variance, cumulative
    variance and loadings."""
    try:
        return json.dumps(utils.ok(**pca_data(_STORE, dataset_id, n_components)), default=str)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool(annotations=RO)
def clustering_preview(dataset_id: str, method: str = "kmeans", max_k: int = 8) -> str:
    """Silhouette scores for k=2..max_k, best k, cluster sizes and per-cluster
    means of the top numeric columns."""
    try:
        return json.dumps(utils.ok(**clustering_preview(_STORE, dataset_id, method, max_k)), default=str)
    except Exception as e:  # noqa: BLE001
        return _err(e)


if __name__ == "__main__":
    mcp.run(transport="stdio")
