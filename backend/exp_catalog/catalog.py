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


# ---------------------------------------------------- privacy / PII ----
def _pii_run(df, seed=42):
    """Detect PII-like columns (emails, phones, ids, cards, addresses)."""
    import re as _re
    patterns = {
        "email": r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
        "phone": r"(?<!\d)[+]?[\d][\d\s\-()]{7,}\d(?!\d)",
        "credit_card": r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)",
        "ssn": r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)",
        "uuid": r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    }
    found = []
    for col in df.columns:
        sample = df[col].astype(str).head(2000)
        for kind, rx in patterns.items():
            hits = int(sample.str.contains(rx, regex=True).sum())
            if hits:
                found.append({"column": str(col), "kind": kind, "hits": hits,
                              "pct": round(hits / max(len(sample), 1), 4)})
    # Identifier-like columns: high-cardinality + unique-ish strings.
    for col in df.columns:
        try:
            nuniq = df[col].nunique(dropna=True)
            if nuniq > max(50, len(df) * 0.5) and "id" in str(col).lower():
                found.append({"column": str(col), "kind": "identifier",
                              "hits": int(nuniq), "pct": round(nuniq / max(len(df), 1), 4)})
        except Exception:  # noqa: BLE001
            continue
    metrics = {"columns": int(len(df.columns)), "rows": int(len(df)),
               "pii_columns": len(found)}
    if found:
        metrics["max_pii_pct"] = max(f["pct"] for f in found)
    return {"n": int(len(df)), "found": found, "metrics": metrics}


def _pii_report(res):
    lines = ["# PII & identifier scan", "",
             f"- **Rows:** {res['n']:,} · **Columns:** {res.get('metrics', {}).get('columns')}",
             f"- **PII-like findings:** {len(res['found'])}", "",
             "## Findings", "",
             "| Column | Kind | Hits | % |",
             "|--------|------|------|---|"]
    if not res["found"]:
        lines.append("_No PII-like patterns detected._")
    else:
        for f in res["found"][:20]:
            lines.append(f"| {f['column']} | {f['kind']} | {f['hits']:,} | {f['pct']:.1%} |")
    return "\n".join(lines)


def _pii_figures(res):
    figs = {}
    found = res.get("found") or []
    if found:
        fig, ax = plt.subplots(figsize=(7, 4))
        kinds = {}
        for f in found:
            kinds[f["kind"]] = kinds.get(f["kind"], 0) + f["hits"]
        names = list(kinds)
        ax.bar(range(len(names)), [kinds[n] for n in names], color="#e05b5b")
        ax.set_xticks(range(len(names))); ax.set_xticklabels(names, rotation=45, ha="right")
        ax.set_ylabel("hits"); ax.set_title("PII hits by type")
        fig.tight_layout()
        _fig_bytes(fig, "pii_by_type.png", figs)
        plt.close(fig)
    return figs


# ------------------------------------------- re-identification risk ----
def _reid_run(df, seed=42):
    """k-anonymity over quasi-identifiers: what share of rows is unique (k=1)?"""
    num = _num_cols(df)
    # Pick up to 4 quasi-identifier-ish categorical columns.
    cands = [c for c in _cat_cols(df)
             if c.lower() not in ("is_fraud", "fraud_flag", "transaction_status")]
    qis = cands[:4]
    rows, total = len(df), len(df)
    metrics = {"rows": total, "qi_columns": len(qis),
               "k_anonymity_1": None, "k_anonymity_5": None}
    if qis:
        counts = df.groupby(qis, dropna=False).size()
        k1 = int((counts < 2).sum())
        k5 = int((counts < 6).sum())
        metrics["k_anonymity_1"] = round(k1 / max(total, 1), 4)
        metrics["k_anonymity_5"] = round(k5 / max(total, 1), 4)
        top = counts.sort_values().head(10)
        res = {"n": total, "qis": qis, "counts": counts, "top": top,
               "metrics": metrics}
    else:
        res = {"n": total, "qis": [], "counts": None, "top": None,
               "metrics": metrics}
    return res


def _reid_report(res):
    m = res["metrics"]
    lines = ["# Re-identification risk (k-anonymity)", "",
             f"- **Rows:** {res['n']:,}",
             f"- **Quasi-identifiers:** {', '.join(res['qis']) or '—'}",
             "",
             "## k-anonymity",
             "",
             f"- **k=1 (uniquely identifiable):** "
             f"{m.get('k_anonymity_1') if m.get('k_anonymity_1') is not None else '—'}",
             f"- **k=5 (near-unique):** "
             f"{m.get('k_anonymity_5') if m.get('k_anonymity_5') is not None else '—'}",
             ""]
    if res.get("counts") is not None:
        lines += ["## Rarest QI combinations", "",
                  "| Count | Combination |", "|-------|-------------|"]
        for idx, count in res["top"].items():
            combo = (" · ".join(str(x) for x in idx)
                     if isinstance(idx, tuple) else str(idx))
            lines.append(f"| {count} | {combo} |")
    return "\n".join(lines)


def _reid_figures(res):
    figs = {}
    if res.get("counts") is not None:
        counts = res["counts"].head(30)
        fig, ax = plt.subplots(figsize=(8, 4.5))
        ax.bar(range(len(counts)), counts.values, color="#b98cff")
        ax.set_ylabel("rows per combination")
        ax.set_title("QI-combination class sizes (lowest)")
        ax.set_xticks([])
        fig.tight_layout()
        _fig_bytes(fig, "reid_class_sizes.png", figs)
        plt.close(fig)
    return figs


# ------------------------------------------------------- cleaning plan --------
def _clean_run(df, seed=42):
    """Quantify the remediation a cleaning pass would apply: duplicate rows,
    missing-value columns, and IQR-outlier columns (with the union of affected
    rows as the headline impact metric). Deterministic — no data is mutated."""
    num = _num_cols(df)
    dup_mask = df.duplicated()
    null_mask = df.isna().any(axis=1)
    outlier_rows = {}
    for c in num:
        s = pd.to_numeric(df[c], errors="coerce")
        q1, q3 = s.quantile(0.25), s.quantile(0.75)
        iqr = (q3 - q1) or 1.0
        lo, hi = q1 - 1.5 * iqr, q3 + 1.5 * iqr
        n_out = int(((s < lo) | (s > hi)).sum())
        if n_out:
            outlier_rows[c] = n_out
    affected = (dup_mask | null_mask).copy()
    for c in num:
        s = pd.to_numeric(df[c], errors="coerce")
        q1, q3 = s.quantile(0.25), s.quantile(0.75)
        iqr = (q3 - q1) or 1.0
        affected |= (s < q1 - 1.5 * iqr) | (s > q3 + 1.5 * iqr)
    null_cols = {c: int(df[c].isna().sum()) for c in df.columns
                 if int(df[c].isna().sum()) > 0}
    metrics = {
        "rows": int(len(df)),
        "duplicates": int(dup_mask.sum()),
        "null_rows": int(null_mask.sum()),
        "null_cols": len(null_cols),
        "outlier_cols": len(outlier_rows),
        "affected_rows": int(affected.sum()),
    }
    return {"n": int(len(df)), "duplicates": int(dup_mask.sum()),
            "null_cols": null_cols, "outlier_rows": outlier_rows,
            "affected_rows": int(affected.sum()), "metrics": metrics}


def _clean_report(res):
    lines = ["# Data cleaning plan (impact assessment)", "",
             f"- **Rows:** {res['n']:,}",
             f"- **Duplicate rows:** {res['duplicates']:,}",
             f"- **Columns with missing values:** {len(res['null_cols'])}",
             f"- **Numeric columns with IQR outliers:** {len(res['outlier_rows'])}",
             f"- **Rows affected by any remediation:** {res['affected_rows']:,} "
             f"({res['affected_rows'] / max(res['n'], 1):.1%})", "",
             "## Missing-value columns", ""]
    if res["null_cols"]:
        for c, n in sorted(res["null_cols"].items(), key=lambda x: -x[1])[:15]:
            lines.append(f"- `{c}`: {n:,} null")
    else:
        lines.append("_No missing values._")
    lines += ["", "## Outlier columns", ""]
    if res["outlier_rows"]:
        for c, n in sorted(res["outlier_rows"].items(), key=lambda x: -x[1])[:15]:
            lines.append(f"- `{c}`: {n:,} IQR outliers")
    else:
        lines.append("_No IQR outliers._")
    return "\n".join(lines)


def _clean_figures(res):
    figs = {}
    dup, null, out = res["duplicates"], sum(res["null_cols"].values()), \
        sum(res["outlier_rows"].values())
    if dup or null or out:
        fig, ax = plt.subplots(figsize=(6, 4))
        cats = ["duplicates", "missing", "outliers"]
        vals = [dup, null, out]
        ax.bar(cats, vals, color=["#6fbf73", "#d9a441", "#e05b5b"])
        ax.set_ylabel("rows / cells affected")
        ax.set_title("Cleaning impact")
        for i, v in enumerate(vals):
            if v:
                ax.text(i, v, f"{v:,}", ha="center", va="bottom", fontsize=9)
        fig.tight_layout()
        _fig_bytes(fig, "clean_impact.png", figs)
        plt.close(fig)
    return figs


# --------------------------------------------------------- catalog ----
CATALOG = [
    {
        "id": "eda",
        "name": "EDA — dataset overview",
        "description": "Profile the dataset: numeric stats, missing values, "
                       "duplicates, and histograms.",
        "goal_metric": "duplicates",
        "higher_better": False,
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
        "id": "clean",
        "name": "Data cleaning plan (dedupe + nulls + outliers)",
        "description": "Quantify the remediation a cleaning pass would apply — "
                       "duplicate rows, missing-value columns and IQR-outlier "
                       "columns — and report the impact (affected rows).",
        "goal_metric": "affected_rows",
        "higher_better": False,
        "needs_dataset": True,
        "plan_steps": lambda req, ds: [
            f"Load `{ds}` and count duplicate rows",
            "Report missing-value columns and their cell counts",
            "Flag IQR-outlier columns and per-column counts",
            "Compute the union of affected rows and render the impact chart",
        ],
        "expected_outputs": [
            "duplicate summary", "missing-value columns",
            "outlier columns", "cleaning-impact chart"],
        "run": _clean_run,
        "render_report": _clean_report,
        "render_figures": _clean_figures,
    },
    {
        "id": "dp_privacy",
        "name": "Differential-privacy mean estimation",
        "description": "Estimate a numeric column's mean under the Laplace "
                       "mechanism at several ε and show the privacy-utility "
                       "tradeoff.",
        "goal_metric": "min_mae",
        "higher_better": False,
        "needs_dataset": True,
        "seed_sensitive": True,
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
        "goal_metric": "max_abs_corr",
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
        "goal_metric": "max_outlier_pct",
        "higher_better": False,
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
    {
        "id": "pii_scan",
        "name": "PII & identifier scan",
        "description": "Scan every column for PII-like patterns (emails, "
                       "phones, cards, SSNs, UUIDs) and high-cardinality "
                       "identifier columns.",
        "goal_metric": "pii_columns",
        "higher_better": False,
        "needs_dataset": True,
        "plan_steps": lambda req, ds: [
            f"Load `{ds}` and sample columns",
            "Scan for email / phone / card / SSN / UUID patterns",
            "Flag high-cardinality identifier-like columns",
            "Report hits per column + chart by PII type",
        ],
        "expected_outputs": lambda req, ds: [
            "PII findings table", "PII-hits-by-type chart"],
        "run": _pii_run,
        "render_report": _pii_report,
        "render_figures": _pii_figures,
    },
    {
        "id": "reid_risk",
        "name": "Re-identification risk (k-anonymity)",
        "description": "Assess how uniquely identifiable rows are under "
                       "k-anonymity over the dataset's quasi-identifiers "
                       "(share of rows with k<2 and k<6).",
        "goal_metric": "k_anonymity_1",
        "higher_better": False,
        "needs_dataset": True,
        "plan_steps": lambda req, ds: [
            f"Load `{ds}` and pick quasi-identifier columns",
            "Group by QI combination and compute class sizes",
            "Report k=1 (unique) and k=5 (near-unique) shares",
            "List the rarest QI combinations + chart class sizes",
        ],
        "expected_outputs": lambda req, ds: [
            "k-anonymity summary", "rarest QI combinations", "class-size chart"],
        "run": _reid_run,
        "render_report": _reid_report,
        "render_figures": _reid_figures,
    },
]
