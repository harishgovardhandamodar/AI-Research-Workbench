"""Core tests for the EDA MCP analysis functions (no MCP protocol involved).

Run from the repo root:  python -m pytest mcp_servers/eda_mcp/tests
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mcp_servers.eda_mcp.common import utils  # noqa: E402
from mcp_servers.eda_mcp.common.store import DatasetStore  # noqa: E402


def test_jsonable_scalars():
    import numpy as np

    assert utils.jsonable(float("nan")) is None
    assert utils.jsonable(np.int64(3)) == 3
    assert utils.jsonable({"a": np.float32(1.5)}) == {"a": 1.5}


def test_slugify_and_ids():
    assert utils.slugify("My Dataset-v2.csv") == "my_dataset_v2"
    assert utils.dataset_id_for("x.csv") != utils.dataset_id_for("y.csv")


def test_split_columns(iris_df):
    types = utils.split_columns(iris_df)
    assert "sepal length (cm)" in types["numeric"]
    assert "species" in types["categorical"]
    assert "constant_col" in types["constant"]
    assert "row_id" in types["id"]


def test_store_roundtrip(store, did):
    df = store.get(did)
    assert df.shape[0] == 150
    overview = store.overview(did, df)
    assert overview["ok"] is True
    assert overview["rows"] == 150


def test_store_missing_id():
    with pytest.raises(FileNotFoundError):
        DatasetStore().get("does-not-exist")
