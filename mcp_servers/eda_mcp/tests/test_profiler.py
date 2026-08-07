"""Tests for the profiler's core analysis functions."""

from __future__ import annotations

from mcp_servers.eda_mcp import profiler


def test_schema_data(store, did):
    schema = profiler.schema_data(store, did)
    assert schema["rows"] == 150
    assert schema["column_count"] == 7
    assert len(schema["columns"]) == 7
    names = [c["name"] for c in schema["columns"]]
    assert "species" in names
    # null counts are recorded for the injected missing values
    sepal = next(c for c in schema["columns"] if c["name"] == "sepal length (cm)")
    assert sepal["null_count"] == 1


def test_profile_data(store, did):
    p = profiler.profile_data(store, did)
    assert p["constant_columns"] == ["constant_col"]
    assert "row_id" in p["potential_id_columns"]
    assert "species" in p["target_candidates"] or p["target_candidates"]


def test_quality_issues(store, did):
    q = profiler.quality_issues_data(store, did)
    kinds = {i["type"] for i in q["issues"]}
    assert "constant_column" in kinds
    assert "id_column" in kinds
