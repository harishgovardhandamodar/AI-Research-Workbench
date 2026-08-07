"""Tests for univariate + multivariate analysis functions."""

from __future__ import annotations

from mcp_servers.eda_mcp import univariate, multivariate


def test_numeric_summary(store, did):
    s = univariate.numeric_summary(store, did, "sepal length (cm)")
    assert s["n"] == 149  # one missing value injected
    assert abs(s["mean"] - 5.8433) < 0.1
    assert s["histogram"]["bin_edges"][0] <= s["min"]
    assert s["iqr_outliers"]["count"] >= 0


def test_categorical_summary(store, did):
    c = univariate.categorical_summary(store, did, "species", top_n=3)
    assert c["cardinality"] == 3
    assert len(c["top"]) == 3
    assert c["entropy_bits"] > 0


def test_missing_patterns(store, did):
    m = univariate.missing_patterns(store, did)
    cols = {c["column"] for c in m["columns_with_missing"]}
    assert {"sepal length (cm)", "petal width (cm)"} <= cols
    assert m["row_completion"]["rows_any_missing"] >= 6


def test_correlation(store, did):
    corr = multivariate.correlation_data(store, did)
    assert len(corr["columns"]) == 4  # numeric only
    assert len(corr["matrix"]) == 4
    # iris sepal vs petal correlate strongly
    strong = any(abs(p["value"]) > 0.8 for p in corr["significant_pairs"])
    assert strong


def test_target_relationships(store, did):
    tr = multivariate.target_relations_data(store, did, "species")
    assert tr["target_type"] == "categorical"
    assert tr["top_numeric_associations"]  # ANOVA F vs numeric features


def test_pca(store, did):
    p = multivariate.pca_data(store, did, n_components=3)
    assert p["n_components_used"] == 3
    assert abs(sum(p["explained_variance_ratio"]) - p["cumulative_variance"][-1]) < 5e-3


def test_clustering(store, did):
    c = multivariate.clustering_preview_data(store, did, max_k=5)
    assert c["best_k"] is not None
    assert 2 <= c["best_k"] <= 5
