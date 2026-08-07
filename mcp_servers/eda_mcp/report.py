"""MCP Server 5: eda-report-generator.

Compiles the EDA findings (profiler → univariate → multivariate → visualizer)
into a structured Markdown / HTML / PDF report. Run standalone:

    python -m mcp_servers.eda_mcp.report

Reports land in the shared workspace ``reports/`` and can be extended with
custom sections and re-exported. Narrative sections (Executive Summary,
Recommendations) are rule-based by default; pass ``use_llm=True`` to have a
**local** model write them (see ``common/narrative.py``).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from mcp.server import MCPServer

from .common.store import DatasetStore
from .common import utils
from .common import narrative

mcp = MCPServer("eda-report-generator", version="0.1.0")
_STORE = DatasetStore()

_TEMPLATES = Path(__file__).parent / "templates"
REPORT_SECTIONS = ("executive_summary", "dataset_overview", "data_quality",
                   "univariate", "multivariate", "visual_insights",
                   "recommendations", "appendix")


def _err(e: Exception) -> str:
    rec = ""
    if isinstance(e, FileNotFoundError):
        rec = "Load the dataset first with load_dataset (or run list_datasets for valid ids)."
    return json.dumps(utils.err(str(e), recovery=rec), default=str)


def _report_sections_path(report_id: str) -> Path:
    return _STORE.reports_dir_for() / f"{report_id}_sections.json"


def _plot_rel(path: str) -> str:
    """Workspace-root-relative plot path for markdown image links."""
    try:
        p = Path(path).resolve()
        return p.relative_to(_STORE.root.resolve()).as_posix()
    except Exception:
        return str(path)


def _plot_datauri(path: str) -> str:
    """Self-contained base64 data URI so HTML reports embed their plots."""
    import base64

    try:
        p = Path(path)
        if not p.exists():
            return ""
        return f"data:image/png;base64,{base64.b64encode(p.read_bytes()).decode()}"
    except Exception:
        return ""


def _render(template_name: str, context: dict) -> str:
    from jinja2 import Environment, FileSystemLoader, select_autoescape

    env = Environment(loader=FileSystemLoader(str(_TEMPLATES)),
                      autoescape=select_autoescape(["html", "xml"]),
                      trim_blocks=True, lstrip_blocks=True)
    env.filters["fmt"] = utils.fmt_num
    env.filters["plotrel"] = _plot_rel
    env.filters["datauri"] = _plot_datauri
    return env.get_template(template_name).render(**context)


def _build_context(store: DatasetStore, dataset_id: str, use_llm: bool = False) -> dict:
    from . import profiler, univariate, multivariate, visualizer

    meta = store.get_meta(dataset_id)
    df = store.get(dataset_id)
    schema = profiler.schema_data(store, dataset_id)
    profile = profiler.profile_data(store, dataset_id)
    quality = profiler.quality_issues_data(store, dataset_id)
    dist = univariate.distribution_summary_data(store, dataset_id)
    missing = univariate.missing_patterns(store, dataset_id)
    num_cols = meta.get("col_types", {}).get("numeric", [])
    cat_cols = meta.get("col_types", {}).get("categorical", [])
    corr = multivariate.correlation_data(store, dataset_id) if len(num_cols) >= 2 else None
    plots = visualizer.auto_visualize_plots(store, dataset_id, max_plots=12)

    # numeric summaries table (top 12 by something)
    num_sum = []
    for c in num_cols[:15]:
        try:
            num_sum.append(univariate.numeric_summary(store, dataset_id, c))
        except Exception:
            pass
    cat_sum = []
    for c in cat_cols[:15]:
        try:
            cat_sum.append(univariate.categorical_summary(store, dataset_id, c, top_n=5))
        except Exception:
            pass

    overview_text = (
        f"Dataset {meta['dataset_id']}: {meta['rows']} rows x {meta['columns']} "
        f"columns ({meta.get('memory_mb')} MB). Numeric: {len(num_cols)}, "
        f"categorical: {len(cat_cols)}. {len(quality['issues'])} quality issue(s) "
        f"found; {missing['row_completion']['rows_any_missing']} rows have at least "
        f"one missing value. Best correlations: "
        + "; ".join(f"{p['col_a']}~{p['col_b']} ({p['value']})"
                    for p in (corr or {}).get("significant_pairs", [])[:3]) or "none"
    )

    llm_facts = {
        "use_llm": bool(use_llm),
        "dataset": meta["dataset_id"], "rows": meta["rows"], "columns": meta["columns"],
        "shape": meta.get("shape"), "memory_mb": meta.get("memory_mb"),
        "column_types": meta.get("col_types"),
        "quality_issue_count": len(quality["issues"]),
        "quality_issues": quality["issues"][:8],
        "worst_missing": missing["columns_with_missing"][:5],
        "top_correlations": (corr or {}).get("significant_pairs", [])[:5],
        "numeric_summary": num_sum[:8],
        "skewed": dist.get("notably_skewed", [])[:5],
    }
    exec_summary = narrative.write_summary(llm_facts) or _rule_summary(llm_facts)
    recommendations = narrative.write_recommendations(overview_text) or _rule_recommendations(llm_facts)

    return {
        "dataset_id": meta["dataset_id"],
        "source": meta.get("source"),
        "rows": meta["rows"], "columns": meta["columns"],
        "memory_mb": meta.get("memory_mb"),
        "generated_at": time.strftime("%Y-%m-%d %H:%M"),
        "schema": schema, "profile": profile, "quality": quality,
        "distribution": dist, "missing": missing,
        "correlation": corr, "num_summary": num_sum, "cat_summary": cat_sum,
        "plots": plots,
        "executive_summary": exec_summary,
        "recommendations": recommendations,
        "overview_text": overview_text,
        "sections": list(REPORT_SECTIONS),
    }


def _rule_summary(facts: dict) -> str:
    shape = facts.get("shape") or [0, 0]
    q = facts.get("quality_issue_count", 0)
    miss = facts.get("worst_missing") or []
    corr = facts.get("top_correlations") or []
    lines = [
        f"This dataset has **{shape[0]:,} rows and {shape[1]} columns** "
        f"(~{facts.get('memory_mb')} MB in memory).",
        f"The profile found **{q} data-quality issue(s)**, "
        f"{'most notably missing values in ' + ', '.join(c['column'] for c in miss[:3]) + '.' if miss else 'and the data is largely complete.'}",
    ]
    if corr:
        lines.append("The strongest associations are "
                     + ", ".join(f"**{c['col_a']} ↔ {c['col_b']}** (r={c['value']})" for c in corr[:3]) + ".")
    else:
        lines.append("No strong pairwise numeric correlations were detected.")
    lines.append("Overall the data appears usable for further modeling; see the sections below for detail.")
    return " ".join(lines)


def _rule_recommendations(facts: dict) -> str:
    q = facts.get("quality_issues") or []
    out = []
    for issue in q[:5]:
        out.append(f"- **{issue['type'].replace('_', ' ')}** ({issue['column']}): {issue['message']} — {issue['suggestion']}")
    out.append("- **Feature engineering**: encode categoricals, consider interactions for the "
               "strongest correlated pairs, and drop constant/ID columns from the feature set.")
    out.append("- **Modeling**: start with a simple baseline; use the numeric columns least "
               "collinear with each other to avoid multicollinearity.")
    return "\n".join(out) if out else (
        "- Impute or drop columns with high missingness.\n"
        "- Encode categoricals and normalize numerics before modeling.")


def compile_report_impl(store: DatasetStore, dataset_id: str, sections: list | None = None,
                   fmt: str = "markdown", use_llm: bool = False) -> dict:
    ctx = _build_context(store, dataset_id, use_llm)
    if sections:
        ctx["sections"] = [s for s in sections if s in REPORT_SECTIONS]
    report_id = f"{utils.slugify(dataset_id)}_{int(time.time())}"
    if fmt in ("markdown", "md"):
        body = _render("report.md.j2", ctx)
    else:
        body = _render("report.html.j2", ctx)
    ext = "md" if fmt in ("markdown", "md") else "html"
    path = store.reports_dir_for() / f"{report_id}.{ext}"
    path.write_text(body)
    utils.json_dump(ctx, _report_sections_path(report_id))
    return {
        "report_id": report_id,
        "format": ext,
        "report_path": str(path),
        "dataset_id": dataset_id,
        "sections": ctx["sections"],
        "executive_summary": ctx["executive_summary"],
    }


def export_report_impl(store: DatasetStore, report_id: str, fmt: str = "md") -> dict:
    """Export an existing report. md/html are direct; pdf needs pandoc."""
    if fmt not in ("md", "markdown", "html", "pdf"):
        raise ValueError(f"unsupported format '{fmt}' (md|html|pdf)")
    base = store.reports_dir_for() / report_id
    if not base.with_suffix(".md").exists():
        raise FileNotFoundError(f"unknown report_id '{report_id}'. Reports: "
                                f"{', '.join(p.stem for p in store.reports_dir_for().glob('*.md'))}")
    if fmt in ("md", "markdown"):
        return {"report_id": report_id, "format": "md", "report_path": str(base.with_suffix(".md"))}
    if fmt == "html":
        src = base.with_suffix(".md").read_text()
        import html as _html

        out = "<html><head><meta charset='utf-8'><title>" + _html.escape(report_id) + \
              "</title><style>body{max-width:880px;margin:2rem auto;font-family:system-ui;line-height:1.5}" \
              "table{border-collapse:collapse}td,th{border:1px solid #ccc;padding:3px 8px}img{max-width:100%}</style></head><body>" \
              + _html.escape(src) + "</body></html>"
        out = out.replace("&lt;", "<").replace("&gt;", ">")  # allow tags from md (tables)
        dest = base.with_suffix(".html")
        dest.write_text(out)
        return {"report_id": report_id, "format": "html", "report_path": str(dest)}
    # pdf via pandoc (must be installed)
    import shutil
    import subprocess

    if not shutil.which("pandoc"):
        raise RuntimeError("PDF export needs `pandoc` (install it, or export markdown/html instead)")
    dest = base.with_suffix(".pdf")
    subprocess.run(["pandoc", str(base.with_suffix(".md")), "-o", str(dest)],
                   check=True, capture_output=True)
    return {"report_id": report_id, "format": "pdf", "report_path": str(dest)}


# ------------------------------------------------------------------- tools ----

@mcp.tool()
def compile_report(dataset_id: str, sections: list = None, format: str = "markdown",
                   use_llm: bool = False) -> str:
    """Compile a full EDA report for a loaded dataset (Markdown by default).
    sections (optional) filters to a subset; use_llm (False) lets a local model
    write the narrative sections."""
    try:
        return json.dumps(utils.ok(**compile_report_impl(_STORE, dataset_id, sections, format, use_llm)), default=str)
    except Exception as e:  # noqa: BLE001
        return _err(e)


def _add_custom_section_impl(store: DatasetStore, report_id: str, title: str,
                             content: str) -> dict:
    ctx = utils.json_load(_report_sections_path(report_id))
    if ctx is None:
        raise FileNotFoundError(f"unknown report_id '{report_id}'")
    custom = ctx.setdefault("custom_sections", [])
    custom.append({"title": title, "content": content})
    utils.json_dump(ctx, _report_sections_path(report_id))
    return {"report_id": report_id, "sections_added": len(custom)}


@mcp.tool()
def add_custom_section(report_id: str, title: str, content: str) -> str:
    """Append an agent-authored section to a compiled report (stored alongside
    the report, so it can be merged on the next export)."""
    try:
        return json.dumps(utils.ok(**_add_custom_section_impl(_STORE, report_id, title, content)), default=str)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool()
def export_report(report_id: str, format: str = "md") -> str:
    """Export a compiled report as md / html / pdf (pdf needs pandoc installed)."""
    try:
        return json.dumps(utils.ok(**export_report_impl(_STORE, report_id, format)), default=str)
    except Exception as e:  # noqa: BLE001
        return json.dumps(utils.err(str(e), recovery="Export markdown or html, or install pandoc for pdf."), default=str)


if __name__ == "__main__":
    mcp.run(transport="stdio")
