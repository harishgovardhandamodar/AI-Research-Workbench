"""MCP Server 4: eda-visualizer.

Generates plot artifacts (PNG) with matplotlib and extracts visual insights.
No seaborn/missingno dependency: everything is drawn with pure matplotlib.
Run standalone:

    python -m mcp_servers.eda_mcp.visualizer

Plots are saved into the shared workspace under ``plots/<dataset_id>/``; every
plot carries a sidecar ``.json`` with its caption so :func:`extract_visual_insights`
can describe it later without a vision model.
"""

from __future__ import annotations

import json

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from .common.store import DatasetStore
from .common import utils

mcp = MCPServer("eda-visualizer", version="0.1.0")
RO = ToolAnnotations(read_only_hint=True)
_STORE = DatasetStore()

_PLOT_TYPES = ("histogram", "box", "violin", "bar", "heatmap",
               "scatter", "pairplot_subset", "missingness_matrix",
               "correlation_heatmap")


def _err(e: Exception) -> str:
    rec = ""
    if isinstance(e, (KeyError, ValueError)):
        rec = "Check plot_type (supported: %s) and column names via get_schema." % ", ".join(_PLOT_TYPES)
    elif isinstance(e, FileNotFoundError):
        rec = "Load the dataset first with load_dataset (or run list_datasets for valid ids)."
    return json.dumps(utils.err(str(e), recovery=rec), default=str)


def _setup() -> "Any":
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    return plt


def _save_fig(store: DatasetStore, dataset_id: str, name: str, caption: str,
              fig) -> str:
    import os

    d = store.plots_dir_for(dataset_id)
    path = d / f"{utils.safe_filename(name)}.png"
    fig.tight_layout()
    fig.savefig(path, dpi=110, bbox_inches="tight")
    import matplotlib.pyplot as plt

    plt.close(fig)
    utils.json_dump({"name": name, "caption": caption, "path": str(path)},
                    d / f"{utils.safe_filename(name)}.json")
    return str(path)


# ------------------------------------------------------------ plot builders ----

def _plot_histogram(df, column: str, store: DatasetStore, dataset_id: str, name: str) -> str:
    import numpy as np  # lazy

    plt = _setup()
    s = df[column].dropna()
    fig, ax = plt.subplots(figsize=(7, 4))
    counts, edges, _ = ax.hist(s, bins="auto", color="#6b4fb0", alpha=0.85,
                               edgecolor="#2a2138")
    ax.set_title(f"Histogram — {column}")
    ax.set_xlabel(column)
    ax.set_ylabel("count")
    mean = float(s.mean())
    ax.axvline(mean, color="#e06c6c", ls="--", lw=1.5, label=f"mean {utils.fmt_num(mean)}")
    ax.legend()
    cap = (f"Histogram of `{column}` ({int(len(s))} values, mean "
           f"{utils.fmt_num(mean)}). {'Right-skewed' if mean > np.median(s) else 'Left-skewed' if mean < np.median(s) else 'Roughly symmetric'}.")
    return _save_fig(store, dataset_id, name, cap, fig)


def _plot_box(df, columns: list[str], store: DatasetStore, dataset_id: str, name: str) -> str:
    plt = _setup()
    data = [df[c].dropna() for c in columns]
    fig, ax = plt.subplots(figsize=(7, 4))
    ax.boxplot(data, labels=columns, vert=False, patch_artist=True,
               boxprops=dict(facecolor="#6b4fb0", alpha=0.7))
    ax.set_title("Box plots")
    ax.grid(axis="x", alpha=0.3)
    cap = f"Box plots for {len(columns)} numeric column(s): {', '.join(columns)} — outliers beyond whiskers (1.5×IQR) are visible as dots."
    return _save_fig(store, dataset_id, name, cap, fig)


def _plot_violin(df, columns: list[str], store: DatasetStore, dataset_id: str, name: str) -> str:
    plt = _setup()
    fig, ax = plt.subplots(figsize=(7, 4))
    parts = ax.violinplot([df[c].dropna() for c in columns], vert=False, showmedians=True)
    ax.set_yticks(range(1, len(columns) + 1), labels=columns)
    ax.set_title("Violin plots")
    cap = f"Violin plots (density shape + median) for {len(columns)} numeric column(s)."
    return _save_fig(store, dataset_id, name, cap, fig)


def _plot_bar(df, column: str, top_n: int, store: DatasetStore, dataset_id: str, name: str) -> str:
    plt = _setup()
    vc = df[column].value_counts().head(top_n)
    fig, ax = plt.subplots(figsize=(7, 4))
    ax.bar([str(k) for k in vc.index], vc.values, color="#8b5cf6")
    ax.set_title(f"Top categories — {column}")
    ax.set_ylabel("count")
    ax.tick_params(axis="x", rotation=45)
    fig.tight_layout()
    cap = (f"Top {len(vc)} categories of `{column}` by frequency "
           f"(of {int(df[column].nunique(dropna=True))} total); mode = {str(vc.index[0])}.")
    return _save_fig(store, dataset_id, name, cap, fig)


def _plot_heatmap(df, columns: list[str], store: DatasetStore, dataset_id: str, name: str) -> str:
    import numpy as np  # lazy

    plt = _setup()
    corr = df[columns].corr().round(2)
    fig, ax = plt.subplots(figsize=(7, 5.5))
    im = ax.imshow(corr.to_numpy(), cmap="coolwarm", vmin=-1, vmax=1)
    ax.set_xticks(range(len(columns)), labels=columns, rotation=45, ha="right")
    ax.set_yticks(range(len(columns)), labels=columns)
    for i in range(len(columns)):
        for j in range(len(columns)):
            v = corr.iloc[i, j]
            ax.text(j, i, f"{v:.2f}", ha="center", va="center",
                    color="white" if abs(v) > 0.5 else "#222")
    fig.colorbar(im, ax=ax, shrink=0.8)
    ax.set_title("Correlation heatmap")
    cap = "Correlation heatmap of numeric columns (warm = positive, cool = negative)."
    return _save_fig(store, dataset_id, name, cap, fig)


def _plot_scatter(df, x: str, y: str, hue: str | None,
                  store: DatasetStore, dataset_id: str, name: str) -> str:
    plt = _setup()
    fig, ax = plt.subplots(figsize=(7, 4.5))
    if hue:
        for val, sub in df.groupby(hue, dropna=False):
            ax.scatter(sub[x], sub[y], s=14, alpha=0.6, label=str(val))
        ax.legend(fontsize=8)
    else:
        ax.scatter(df[x], df[y], s=14, alpha=0.6, color="#6b4fb0")
    ax.set_xlabel(x)
    ax.set_ylabel(y)
    ax.set_title(f"{x} vs {y}")
    cap = f"Scatter of `{x}` vs `{y}`{f', colored by `{hue}`' if hue else ''} — check for linear/monotonic association and clusters."
    return _save_fig(store, dataset_id, name, cap, fig)


def _plot_pairplot(df, columns: list[str], store: DatasetStore, dataset_id: str, name: str) -> str:
    import numpy as np  # lazy

    plt = _setup()
    cols = columns[:4]
    n = len(cols)
    fig, axes = plt.subplots(n, n, figsize=(3.2 * n, 3.2 * n))
    for i, ci in enumerate(cols):
        for j, cj in enumerate(cols):
            ax = axes[i, j]
            if i == j:
                ax.hist(df[ci].dropna(), bins="auto", color="#6b4fb0", alpha=0.8)
                ax.set_title(ci, fontsize=9)
            else:
                ax.scatter(df[cj], df[ci], s=8, alpha=0.5, color="#6b4fb0")
            ax.tick_params(labelsize=7)
    cap = f"Pairwise scatter/histogram matrix for up to 4 numeric columns ({', '.join(cols)})."
    return _save_fig(store, dataset_id, name, cap, fig)


def _plot_missingness(df, store: DatasetStore, dataset_id: str, name: str) -> str:
    import numpy as np  # lazy

    plt = _setup()
    miss = df.isna()
    cols = [c for c in df.columns if miss[c].any()]
    if not cols:
        return _save_fig(store, dataset_id, name, "No missing values in any column.", plt.subplots()[0])
    m = miss[cols].to_numpy()
    fig, ax = plt.subplots(figsize=(7, 3.5))
    ax.imshow(m.T, aspect="auto", cmap="viridis", interpolation="nearest")
    ax.set_yticks(range(len(cols)), labels=cols, fontsize=7)
    ax.set_xlabel("row index")
    ax.set_title(f"Missingness matrix ({len(cols)} columns with gaps)")
    cap = f"Missingness matrix: rows with any gap; {len(cols)} column(s) contain missing values (yellow = missing)."
    return _save_fig(store, dataset_id, name, cap, fig)


_PLOT_BUILDERS = {
    "histogram": lambda df, p, s, d, n: _plot_histogram(df, p["column"], s, d, n),
    "box": lambda df, p, s, d, n: _plot_box(df, p.get("columns") or [p["column"]], s, d, n),
    "violin": lambda df, p, s, d, n: _plot_violin(df, p.get("columns") or [p["column"]], s, d, n),
    "bar": lambda df, p, s, d, n: _plot_bar(df, p["column"], int(p.get("top_n", 20)), s, d, n),
    "heatmap": lambda df, p, s, d, n: _plot_heatmap(df, p.get("columns") or [], s, d, n),
    "scatter": lambda df, p, s, d, n: _plot_scatter(df, p["x"], p["y"], p.get("hue"), s, d, n),
    "pairplot_subset": lambda df, p, s, d, n: _plot_pairplot(df, p.get("columns") or [], s, d, n),
    "missingness_matrix": lambda df, p, s, d, n: _plot_missingness(df, s, d, n),
    "correlation_heatmap": lambda df, p, s, d, n: _plot_heatmap(df, p.get("columns") or [], s, d, n),
}


def generate_plot_data(store: DatasetStore, dataset_id: str, plot_type: str,
                       params: dict) -> dict:
    df = store.get(dataset_id)
    if plot_type not in _PLOT_BUILDERS:
        raise ValueError(f"unsupported plot_type '{plot_type}'. Supported: {', '.join(_PLOT_TYPES)}")
    params = params or {}
    name = params.get("name") or f"{plot_type}_{int(__import__('time').time())}"
    path = _PLOT_BUILDERS[plot_type](df, params, store, dataset_id, name)
    return {"dataset_id": store.get_meta(dataset_id)["dataset_id"],
            "plot_type": plot_type, "plot_path": path, "caption": utils.json_load(
                store.plots_dir_for(dataset_id) / f"{utils.safe_filename(name)}.json")["caption"]}


def auto_visualize_plots(store: DatasetStore, dataset_id: str,
                         max_plots: int = 12) -> list[dict]:
    """Pick and generate the most informative plots for a dataset."""
    df = store.get(dataset_id)
    meta = store.get_meta(dataset_id)
    types = meta.get("col_types", {})
    numeric = types.get("numeric", [])
    categorical = types.get("categorical", [])
    plots: list[dict] = []
    idx = 0

    def add(plot_type, params):
        nonlocal idx
        if len(plots) >= max_plots:
            return
        idx += 1
        try:
            params = dict(params)
            params["name"] = f"auto_{idx}_{plot_type}"
            res = generate_plot_data(store, dataset_id, plot_type, params)
            plots.append({"plot_path": res["plot_path"], "caption": res["caption"],
                          "plot_type": plot_type})
        except Exception:
            pass

    # 1. numeric histograms (up to 3, most skewed first)
    from .univariate import numeric_summary

    ranked = []
    for c in numeric:
        try:
            ranked.append((abs(numeric_summary(store, dataset_id, c).get("skew") or 0), c))
        except Exception:
            pass
    ranked.sort(reverse=True)
    for _, c in ranked[:3]:
        add("histogram", {"column": c})
    # 2. categorical bars (top cardinality-ish)
    for c in categorical[:3]:
        add("bar", {"column": c, "top_n": 15})
    # 3. correlation heatmap
    if len(numeric) >= 2:
        add("correlation_heatmap", {"columns": numeric[:12]})
    # 4. missingness matrix
    if df.isna().any().any():
        add("missingness_matrix", {})
    # 5. box plots of a few numerics
    if numeric:
        add("box", {"columns": numeric[:6]})
    # 6. scatter of strongest correlated pair
    if len(numeric) >= 2:
        corr = df[numeric].corr()
        vals = corr.where(~corr.isna(), -1)
        best = vals.stack().idxmax()
        if best[0] != best[1]:
            hue = categorical[0] if categorical else None
            add("scatter", {"x": best[0], "y": best[1], "hue": hue})
    return plots


def extract_visual_insights_data(store: DatasetStore, plot_path: str) -> dict:
    """Rule-based description of a generated plot (reads its sidecar caption)."""
    path = utils.safe_filename(plot_path).replace(".png", "")
    for d in store.plots_dir.iterdir():
        jp = d / f"{path}.json"
        if jp.exists():
            meta = utils.json_load(jp)
            return {"plot_path": plot_path, "caption": meta.get("caption"),
                    "insight": meta.get("caption")}
    # fall back to a plain description
    return {"plot_path": plot_path,
            "caption": "Plot generated during EDA.",
            "insight": "No caption metadata found for this plot path."}


# ------------------------------------------------------------------- tools ----

@mcp.tool()
def generate_plot(dataset_id: str, plot_type: str, params: dict) -> str:
    """Generate a plot (PNG) and return its path + a caption. Supported types:
    histogram, box, violin, bar, heatmap, scatter, pairplot_subset,
    missingness_matrix, correlation_heatmap. params keys: column(s), x, y, hue,
    top_n, name."""
    try:
        return json.dumps(utils.ok(**generate_plot_data(_STORE, dataset_id, plot_type, params)), default=str)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool()
def auto_visualize(dataset_id: str, max_plots: int = 12) -> str:
    """Automatically select and generate the most informative plots, returning
    paths + captions for each."""
    try:
        return json.dumps(utils.ok(plots=auto_visualize_plots(_STORE, dataset_id, max_plots)), default=str)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool(annotations=RO)
def extract_visual_insights(plot_path: str) -> str:
    """Read the caption/description recorded for a generated plot path."""
    try:
        return json.dumps(utils.ok(**extract_visual_insights_data(_STORE, plot_path)), default=str)
    except Exception as e:  # noqa: BLE001
        return _err(e)


if __name__ == "__main__":
    mcp.run(transport="stdio")
