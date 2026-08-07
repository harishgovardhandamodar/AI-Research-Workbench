"""Shared pytest fixtures for the EDA MCP tests."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(scope="session")
def iris_df():
    from sklearn.datasets import load_iris

    import pandas as pd

    data = load_iris()
    df = pd.DataFrame(data.data, columns=data.feature_names)
    df["species"] = data.target_names[data.target]
    # inject a couple of missing values + a constant + an id-like column
    df.loc[0, "sepal length (cm)"] = None
    df.loc[5:9, "petal width (cm)"] = None
    df["constant_col"] = 7
    df["row_id"] = range(len(df))
    return df


@pytest.fixture(scope="session")
def store(iris_df, tmp_path_factory):
    from mcp_servers.eda_mcp.common.store import DatasetStore

    s = DatasetStore(tmp_path_factory.mktemp("eda"))
    s.load(iris_df, fmt="auto", dataset_id="iris")
    return s


@pytest.fixture()
def did():
    return "iris"
