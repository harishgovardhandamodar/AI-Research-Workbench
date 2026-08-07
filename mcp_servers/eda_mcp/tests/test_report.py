"""Tests for the report generator + visualizer."""

from __future__ import annotations

from mcp_servers.eda_mcp import report, visualizer


def test_auto_visualize(store, did):
    plots = visualizer.auto_visualize_plots(store, did, max_plots=6)
    assert len(plots) >= 3
    for p in plots:
        assert p["plot_path"].endswith(".png")
        assert p["caption"]


def test_compile_report_markdown(store, did):
    res = report.compile_report_impl(store, did, fmt="markdown", use_llm=False)
    assert res["report_id"]
    path = res["report_path"]
    assert path.endswith(".md")
    text = open(path).read()
    for heading in ("Executive Summary", "Dataset Overview", "Data Quality",
                    "Univariate", "Multivariate", "Visual Insights",
                    "Recommendations", "Appendix"):
        assert heading in text
    assert res["executive_summary"]


def test_compile_report_html(store, did):
    res = report.compile_report_impl(store, did, fmt="html", use_llm=False)
    assert res["report_path"].endswith(".html")
    assert "<html>" in open(res["report_path"]).read()


def test_add_custom_section_and_export(store, did):
    res = report.compile_report_impl(store, did, fmt="markdown", use_llm=False)
    add = report._add_custom_section_impl(store, res["report_id"], "Extra", "agent notes")
    assert add["sections_added"] == 1
    out = report.export_report_impl(store, res["report_id"], "html")
    assert out["format"] == "html"
    assert out["report_path"].endswith(".html")
