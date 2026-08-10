"""Flint charts integration: render experiment/run metrics as charts via the
built-in ``flint`` MCP server (``flint__render_chart``) and hand back PNG bytes.

The flint server is optional — every helper degrades to ``None`` when the
server is missing, offline, or the spec is rejected, and the chat chart intent
falls back to a deterministic matplotlib renderer, so charts always work.
"""

from __future__ import annotations

import re


FLINT_SERVER = "flint"
FLINT_RENDER = "render_chart"


def metric_evolution_spec(runs: list[dict], metric: str) -> dict:
    """A line-chart spec of a metric's evolution across an experiment's runs."""
    rows = []
    for i, r in enumerate(runs, 1):
        v = (r.get("metrics") or {}).get(metric)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            rows.append({"x": i, "y": round(float(v), 6), "run": r.get("id")})
    return {
        "title": f"{metric} across runs",
        "type": "line",
        "x": "x",
        "y": "y",
        "x_label": "run #",
        "y_label": metric,
        "data": rows,
    }


def run_metrics_spec(run: dict) -> dict:
    """A bar-chart spec of a run's flat numeric metrics."""
    rows = [{"metric": k, "value": round(float(v), 6)}
            for k, v in (run.get("metrics") or {}).items()
            if isinstance(v, (int, float)) and not isinstance(v, bool)]
    return {
        "title": f"Run #{run.get('id')} metrics",
        "type": "bar",
        "x": "metric",
        "y": "value",
        "x_label": "metric",
        "y_label": "value",
        "data": rows,
    }


async def chart_png(registry, spec: dict, theme: str = "") -> bytes | None:
    """Render a Flint chart spec to PNG bytes via the flint MCP server.

    Returns None when the flint server is unavailable or the render fails.
    """
    try:
        from .mcp import call_mcp_tool
    except Exception:  # noqa: BLE001
        return None
    args: dict = {"spec": spec}
    if theme:
        args["theme"] = theme
    try:
        _text, is_err, images = await call_mcp_tool(
            registry, FLINT_SERVER, FLINT_RENDER, args)
    except Exception:  # noqa: BLE001
        return None
    if is_err:
        return None
    for mime, data in images or []:
        if mime.startswith("image/"):
            return data
    return None


async def render_chart_artifact(rt, registry, spec: dict, name: str, *,
                                message_id: str = "", run_id: str = "") -> str | None:
    """Render a chart spec (flint first, matplotlib fallback) and register it as
    a figure artifact. Returns the artifact id, or None if both renderers fail."""
    png = await chart_png(registry, spec)
    if not png:
        png = fallback_png(spec)
    if not png:
        return None
    from .artifacts.store import Artifact
    art = Artifact(kind="figure", name=name, description="Flint chart",
                   code="", env={}, message_id=message_id,
                   run_id=run_id, data_type="png")
    try:
        rt.artifacts.add_artifact(art, data=png, data_type="png")
    except Exception:  # noqa: BLE001
        return None
    return art.id


# -------------------------------------------------- natural-language intents ----

def resolve_column(df, name: str):
    """Best-effort column resolution: exact, case-insensitive, then substring."""
    if name is None:
        return None
    target = str(name).strip().strip("`'\"”’").lower()
    if not target:
        return None
    for c in df.columns:
        if str(c).lower() == target:
            return c
    for c in df.columns:
        if target in str(c).lower():
            return c
    return None


def _n(col, df, default=None):
    return default if col is None else str(col)


def distribution_spec(df, column: str, histogram: bool = False) -> dict:
    """Bar (categorical) or histogram (numeric) of a column's distribution."""
    col = resolve_column(df, column)
    if col is None:
        return {}
    title = _n(col, df, column)
    try:
        import pandas as pd
        s = pd.to_numeric(df[col], errors="coerce")
    except Exception:  # noqa: BLE001
        s = df[col]
    # Treat as numeric only when the column is actually numeric (all-NaN after
    # coercion means it's categorical, e.g. "transaction type").
    if histogram or (s.notna().sum() > 0 and getattr(s.dtype, "kind", "") in "iuf"):
        vals = s.dropna()
        if len(vals) == 0:
            return {}
        bins = pd.cut(vals, bins=min(20, max(2, vals.nunique()))).value_counts().sort_index()
        rows = [{"bin": f"{int(iv.left)}–{int(iv.right)}" if iv.left.is_integer() and iv.right.is_integer()
                 else f"{iv.left:.2f}–{iv.right:.2f}", "count": int(c)}
                for iv, c in bins.items()]
        return {"title": f"Distribution of {title} (histogram)", "type": "bar",
                "x": "bin", "y": "count", "x_label": title, "y_label": "count",
                "data": rows}
    vc = df[col].value_counts(dropna=True)
    if len(vc) == 0:
        return {}
    rows = [{"category": str(k), "count": int(v)} for k, v in vc.head(15).items()]
    return {"title": f"Distribution of {title}", "type": "bar", "x": "category",
            "y": "count", "x_label": title, "y_label": "count", "data": rows}


def scatter_spec(df, a: str, b: str) -> dict:
    col_a, col_b = resolve_column(df, a), resolve_column(df, b)
    if col_a is None or col_b is None:
        return {}
    import pandas as pd
    na = pd.to_numeric(df[col_a], errors="coerce")
    nb = pd.to_numeric(df[col_b], errors="coerce")
    if na.notna().sum() > 0 and nb.notna().sum() > 0:
        tmp = pd.DataFrame({"a": na, "b": nb}).dropna().head(300)
        rows = [{"a": round(float(x), 6), "b": round(float(y), 6)}
                for x, y in zip(tmp["a"], tmp["b"])]
        return {"title": f"{col_a} vs {col_b}", "type": "scatter", "x": "a",
                "y": "b", "x_label": str(col_a), "y_label": str(col_b),
                "data": rows}

    def _grouped(num_col, num_name, cat_col):
        tmp = pd.DataFrame({"g": df[cat_col], "v": num_col}).dropna()
        if len(tmp) == 0:
            return {}
        agg = tmp.groupby("g")["v"].mean().sort_values(ascending=False).head(15)
        rows = [{"category": str(k), "count": round(float(v), 4)}
                for k, v in agg.items()]
        return {"title": f"Average {num_name} by {cat_col}",
                "type": "bar", "x": "category", "y": "count",
                "x_label": str(cat_col), "y_label": "average", "data": rows}

    if na.notna().sum() > 0:
        return _grouped(na, str(col_a), col_b)
    if nb.notna().sum() > 0:
        return _grouped(nb, str(col_b), col_a)
    return {}


def trend_spec(df, metric: str, by: str) -> dict:
    col_m, col_b = resolve_column(df, metric), resolve_column(df, by)
    if col_m is None or col_b is None:
        return {}
    import pandas as pd
    tmp = pd.DataFrame({"b": pd.to_numeric(df[col_b], errors="coerce"),
                        "m": pd.to_numeric(df[col_m], errors="coerce")}).dropna()
    if len(tmp) == 0:
        return {}
    n_bins = min(20, max(2, tmp["b"].nunique()))
    tmp["bucket"] = pd.qcut(tmp["b"], q=n_bins, duplicates="drop")
    agg = tmp.groupby("bucket", observed=True)["m"].mean().sort_index()
    rows = [{"x": f"{iv.left:.2f}–{iv.right:.2f}", "y": round(float(v), 6)}
            for iv, v in agg.items()]
    return {"title": f"{col_m} over {col_b}", "type": "line", "x": "x", "y": "y",
            "x_label": str(col_b), "y_label": str(col_m), "data": rows}


def chart_spec_from_request(text: str, df) -> dict:
    """Parse a natural-language chart request into a Flint spec.

    Supports: "distribution|histogram of X", "correlation between A and B",
    "scatter A vs B", "trend of X over Y".
    """
    low = (text or "").lower()
    m = re.search(r"(?:correlation between|scatter(?:plot)?)\s+(.+?)\s+(?:and|vs\.?)\s+(.+)", low)
    if m:
        return scatter_spec(df, m.group(1), m.group(2))
    m = re.search(r"(?:trend|line chart)\s+of\s+(.+?)\s+over\s+(.+)", low)
    if m:
        return trend_spec(df, m.group(1), m.group(2))
    m = re.search(r"(?:distribution|histogram|bar(?: chart)?|pie(?: chart)?|count|plot|graph|chart)\s+of\s+(.+)", low)
    if m:
        return distribution_spec(df, m.group(1), histogram="histogram" in low)
    return {}


def fallback_png(spec: dict) -> bytes | None:
    """Deterministic matplotlib fallback so charts work without the flint
    server. Renders the spec's ``data`` rows (bar / scatter / line)."""
    import io
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt  # noqa: E402
        data = spec.get("data") or []
        if not data:
            return None
        title = spec.get("title") or "chart"
        stype = (spec.get("type") or "bar").lower()
        fig, ax = plt.subplots(figsize=(7, 4.5))
        if stype == "scatter":
            ax.scatter([r.get("a") for r in data], [r.get("b") for r in data],
                       s=14, alpha=0.6, color="#4f8cff")
            ax.set_xlabel(spec.get("x_label") or spec.get("x") or "a")
            ax.set_ylabel(spec.get("y_label") or spec.get("y") or "b")
        elif stype == "line":
            xs = [str(r.get("x", i)) for i, r in enumerate(data)]
            ax.plot(xs, [r.get("y", 0) for r in data], "-o", color="#d9a441")
            ax.set_ylabel(spec.get("y_label") or "y")
            ax.tick_params(axis="x", rotation=45)
        else:  # bar
            labels = [str(r.get("category") or r.get("bin") or r.get("x", i))
                      for i, r in enumerate(data)]
            vals = [r.get("count", r.get("value") or r.get("y", 0)) for r in data]
            ax.bar(range(len(labels)), vals, color="#4f8cff")
            ax.set_xticks(range(len(labels)))
            ax.set_xticklabels(labels, rotation=45, ha="right", fontsize=8)
            ax.set_ylabel(spec.get("y_label") or "count")
        ax.set_title(title, fontsize=11)
        fig.tight_layout()
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=130)
        plt.close(fig)
        buf.seek(0)
        return buf.read()
    except Exception:  # noqa: BLE001
        return None
