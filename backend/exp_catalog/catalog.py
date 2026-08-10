"""Built-in deterministic experiments for the experiment planner.

Each entry exposes:
  id, name, description, needs_dataset, requires_columns (optional),
  plan_steps(request, dataset) -> [str], expected_outputs(request, dataset) ->
  [str], run(df, seed) -> dict (with a flat 'metrics'), render_report(res) ->
  str, render_figures(res) -> {filename: png_bytes}.

All are pure pandas/numpy/matplotlib (Agg) — no LLM, no external MCP deps —
so plans are reproducible given the same seed + dataset.
"""

from __future__ import annotations

import io
import json
import time

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402


def _fig_bytes(fig, name: str, into: dict):
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=140)
    buf.seek(0)
    into[name] = buf.read()


def _num_cols(df: pd.DataFrame) -> list[str]:
    return [c for c in df.columns if pd.api.types.is_numeric_dtype(df[c])]


def _cat_cols(df: pd.DataFrame) -> list[str]:
    return [c for c in df.columns
            if not pd.api.types.is_numeric_dtype(df[c])
            and df[c].nunique(dropna=True) < 100]


# --------------------------------------------------------------- eda --------
def _eda_run(df, seed=42):
    num = _num_cols(df)
    stats = df[num].describe().T if num else pd.DataFrame()
    nulls = {c: float(df[c].isna().mean()) for c in df.columns}
    dups = int(df.duplicated().sum())
    metrics = {
        "rows": int(len(df)), "columns": int(len(df.columns)),
        "numeric": len(num), "duplicates": dups,
    }
    if num:
        metrics["null_pct_max"] = round(max(nulls.values()), 4)
    res = {"n": int(len(df)), "stats": stats, "nulls": nulls,
           "duplicates": dups, "num_cols": num, "metrics": metrics}
    res["_df"] = df  # for figure rendering
    return res


def _eda_report(res):
    lines = ["# EDA — dataset overview", "",
             f"- **Rows:** {res['n']:,} · **Columns:** {len(res['num_cols'])} numeric",
             f"- **Duplicate rows:** {res['duplicates']:,}",
             "", "## Numeric columns", ""]
    if res["stats"].empty:
        lines.append("_No numeric columns._")
    else:
        lines.append("| Column | count | mean | std | min | 25% | 50% | 75% | max |")
        lines.append("|--------|-------|------|-----|-----|-----|-----|-----|-----|")
        for col, row in res["stats"].iterrows():
            lines.append(f"| {col} | {int(row['count'])} | {row['mean']:.2f} | "
                         f"{row['std']:.2f} | {row['min']:.2f} | {row['25%']:.2f} | "
                         f"{row['50%']:.2f} | {row['75%']:.2f} | {row['max']:.2f} |")
    lines += ["", "## Missing values", ""]
    missing = {k: v for k, v in res["nulls"].items() if v > 0}
    if missing:
        for k, v in sorted(missing.items(), key=lambda x: -x[1])[:15]:
            lines.append(f"- `{k}`: {v:.1%} null")
    else:
        lines.append("_No missing values._")
    return "\n".join(lines)


def _eda_figures(res):
    figs = {}
    num = res.get("num_cols") or []
    if num:
        # Histograms of the first 6 numeric columns.
        fig, axes = plt.subplots(2, 3, figsize=(12, 6))
        axes = axes.ravel()
        for i, c in enumerate(num[:6]):
            ax = axes[i]
            try:
                ax.hist(res["_df"][c].dropna(), bins=30, color="#4f8cff")
            except Exception:  # noqa: BLE001
                pass
            ax.set_title(c, fontsize=8)
        for j in range(len(num[:6]), len(axes)):
            axes[j].axis("off")
        fig.suptitle("Numeric distributions")
        fig.tight_layout()
        _fig_bytes(fig, "eda_histograms.png", figs)
        plt.close(fig)
    return figs


# ------------------------------------------------------- dp_privacy --------
def _dp_run(df, seed=42):
    rng = np.random.default_rng(seed)
    num = _num_cols(df)
    epsilons = [0.1, 0.5, 1.0, 2.0, 5.0]
    if not num:
        num = []
    target = num[0] if num else None
    rows = []
    if target is not None:
        real_mean = float(df[target].mean())
        scale = float(df[target].std() or 1.0)
        for eps in epsilons:
            # Laplace mechanism: sensitivity ~ range, noise = Lap(scale/eps).
            sens = float(np.ptp(df[target].values))
            noise = rng.laplace(0.0, sens / max(eps, 1e-9), size=500)
            est = float((df[target].sum() + noise.sum()) / len(df))
            rows.append({"epsilon": eps, "real_mean": real_mean,
                         "dp_mean": est, "error": abs(est - real_mean)})
    metrics = {"target": target or "none", "epsilons": len(epsilons)}
    if rows:
        metrics["min_mae"] = round(min(r["error"] for r in rows), 4)
    return {"n": int(len(df)), "target": target, "rows": rows,
            "num_cols": num, "metrics": metrics}


def _dp_report(res):
    lines = ["# Differential Privacy — mean estimation", "",
             f"- **Target column:** `{res.get('target') or '—'}`",
             f"- **Epsilons:** {[r['epsilon'] for r in res.get('rows', [])]}", "",
             "## Laplace mechanism (DP mean vs real mean)", "",
             "| ε | Real mean | DP mean | Abs error |",
             "|---|-----------|---------|-----------|"]
    for r in res.get("rows", []):
        lines.append(f"| {r['epsilon']} | {r['real_mean']:.2f} | "
                     f"{r['dp_mean']:.2f} | {r['error']:.4f} |")
    return "\n".join(lines)


def _dp_figures(res):
    figs = {}
    rows = res.get("rows") or []
    if rows:
        fig, ax = plt.subplots(figsize=(7, 4))
        ax.plot([r["epsilon"] for r in rows], [r["error"] for r in rows],
                "o-", color="#d9a441")
        ax.set_xlabel("ε"); ax.set_ylabel("|DP mean − real mean|")
        ax.set_title("DP error vs epsilon")
        ax.grid(alpha=0.3)
        fig.tight_layout()
        _fig_bytes(fig, "dp_error_vs_eps.png", figs)
        plt.close(fig)
    return figs


# ------------------------------------------------------ correlation --------
def _corr_run(df, seed=42):
    num = _num_cols(df)
    corr = df[num].corr() if len(num) >= 2 else pd.DataFrame()
    top = []
    if len(num) >= 2:
        m = corr.where(~corr.isna(), -1)
        pairs = m.stack()
        pairs = pairs[abs(pairs) < 0.9999]
        pairs = pairs.abs().sort_values(ascending=False)
        for (a, b), v in list(pairs.items())[:8]:
            top.append({"x": a, "y": b, "corr": float(m.loc[a, b])})
    metrics = {"numeric_cols": len(num), "pairs": len(top)}
    if top:
        metrics["max_abs_corr"] = round(max(abs(t["corr"]) for t in top), 4)
    return {"n": int(len(df)), "num_cols": num, "corr": corr,
            "top": top, "metrics": metrics}


def _corr_report(res):
    lines = ["# Correlation analysis", "",
             f"- **Numeric columns:** {', '.join(res['num_cols']) or '—'}", "",
             "## Top correlated pairs", ""]
    if not res["top"]:
        lines.append("_Fewer than 2 numeric columns — nothing to correlate._")
    else:
        lines.append("| X | Y | Pearson r |")
        lines.append("|---|---|-----------|")
        for t in res["top"]:
            lines.append(f"| {t['x']} | {t['y']} | {t['corr']:.3f} |")
    return "\n".join(lines)


def _corr_figures(res):
    figs = {}
    if len(res["num_cols"]) >= 2 and not res["corr"].empty:
        fig, ax = plt.subplots(figsize=(7, 6))
        im = ax.imshow(res["corr"].values, cmap="RdBu_r", vmin=-1, vmax=1)
        ax.set_xticks(range(len(res["corr"].columns)))
        ax.set_xticklabels(res["corr"].columns, rotation=45, ha="right", fontsize=8)
        ax.set_yticks(range(len(res["corr"].index)))
        ax.set_yticklabels(res["corr"].index, fontsize=8)
        fig.colorbar(im)
        fig.tight_layout()
        _fig_bytes(fig, "correlation_matrix.png", figs)
        plt.close(fig)
    return figs


# ---------------------------------------------------------- anomaly --------
def _anom_run(df, seed=42):
    num = _num_cols(df)
    found = []
    for c in num:
        s = pd.to_numeric(df[c], errors="coerce")
        q1, q3 = s.quantile(0.25), s.quantile(0.75)
        iqr = (q3 - q1) or 1.0
        lo, hi = q1 - 1.5 * iqr, q3 + 1.5 * iqr
        mask = (s < lo) | (s > hi)
        n_out = int(mask.sum())
        if n_out:
            found.append({"column": c, "outliers": n_out,
                          "pct": round(n_out / max(len(s), 1), 4),
                          "bounds": [round(float(lo), 2), round(float(hi), 2)]})
    found.sort(key=lambda x: -x["outliers"])
    metrics = {"numeric_cols": len(num), "outlier_cols": len(found)}
    if found:
        metrics["max_outlier_pct"] = found[0]["pct"]
    return {"n": int(len(df)), "found": found, "num_cols": num,
            "metrics": metrics}


def _anom_report(res):
    lines = ["# Outlier detection (IQR 1.5×)", "",
             f"- **Numeric columns checked:** {len(res['num_cols'])}", "",
             "## Outliers per column", ""]
    if not res["found"]:
        lines.append("_No IQR outliers detected._")
    else:
        lines.append("| Column | Outliers | % | Bounds (lo, hi) |")
        lines.append("|--------|----------|-----|----------------|")
        for f in res["found"][:15]:
            lines.append(f"| {f['column']} | {f['outliers']:,} | {f['pct']:.1%} | "
                         f"{f['bounds']} |")
    return "\n".join(lines)


def _anom_figures(res):
    figs = {}
    return figs


# --------------------------------------------------------- catalog ----
CATALOG = [
    {
        "id": "eda",
        "name": "EDA — dataset overview",
        "description": "Profile the dataset: numeric stats, missing values, "
                       "duplicates, and histograms.",
        "needs_dataset": True,
        "plan_steps": lambda req, ds: [
            f"Load `{ds}` and infer schema/column types",
            "Compute numeric stats (count/mean/std/quartiles)",
            "Report missing-value fractions and duplicate rows",
            "Render histograms of the first numeric columns",
        ],
        "expected_outputs": lambda req, ds: [
            "numeric summary table", "missing-value report", "histograms"],
        "run": _eda_run,
        "render_report": _eda_report,
        "render_figures": _eda_figures,
    },
    {
        "id": "dp_privacy",
        "name": "Differential-privacy mean estimation",
        "description": "Estimate a numeric column's mean under the Laplace "
                       "mechanism at several ε and show the privacy-utility "
                       "tradeoff.",
        "needs_dataset": True,
        "plan_steps": lambda req, ds: [
            f"Load `{ds}` and pick a numeric target column",
            "Compute the real mean and sensitivity",
            "Add Laplace noise at ε ∈ {0.1, 0.5, 1, 2, 5}",
            "Report DP mean error vs ε and render the curve",
        ],
        "expected_outputs": lambda req, ds: [
            "DP mean vs real mean table", "error-vs-ε chart"],
        "run": _dp_run,
        "render_report": _dp_report,
        "render_figures": _dp_figures,
    },
    {
        "id": "correlation",
        "name": "Correlation analysis",
        "description": "Compute the Pearson correlation matrix over numeric "
                       "columns and list the strongest pairs.",
        "needs_dataset": True,
        "plan_steps": lambda req, ds: [
            f"Load `{ds}` and select numeric columns",
            "Compute the Pearson correlation matrix",
            "Rank the strongest (non-self) pairs",
            "Render the correlation heatmap",
        ],
        "expected_outputs": lambda req, ds: [
            "top correlated pairs", "correlation heatmap"],
        "run": _corr_run,
        "render_report": _corr_report,
        "render_figures": _corr_figures,
    },
    {
        "id": "anomaly",
        "name": "Outlier detection (IQR)",
        "description": "Flag IQR outliers (1.5×) per numeric column and report "
                       "their share + bounds.",
        "needs_dataset": True,
        "plan_steps": lambda req, ds: [
            f"Load `{ds}` and select numeric columns",
            "Compute Q1/Q3 and the IQR bounds per column",
            "Count outliers and their share per column",
            "Report the outlier table",
        ],
        "expected_outputs": lambda req, ds: ["outlier-per-column table"],
        "run": _anom_run,
        "render_report": _anom_report,
        "render_figures": _anom_figures,
    },
]
