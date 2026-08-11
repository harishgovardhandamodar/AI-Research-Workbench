"""Flint-charts integration tests: PNG capture from the flint MCP server,
experiment/run chart endpoints, and report chart embedding (with graceful
degradation when the flint server is unavailable)."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend.flint_charts import (chart_png, chart_spec_from_request,
                                  fallback_png, resolve_column)
from backend.mcp import MCPRegistry

PNG = b"\x89PNG-fake-chart-data"


def _tool(name, ro=True):
    return type("T", (), {"name": name, "description": "desc",
                          "annotations": type("A", (), {"read_only_hint": ro})(),
                          "input_schema": {"type": "object",
                                           "properties": {}}})()


class _FlintConn:
    def __init__(self):
        self.spec = None

    async def list_tools(self):
        return [_tool("render_chart")]

    async def call_tool(self, name, args):
        self.spec = args
        return ("ok", False, [("image/png", PNG)])

    async def close(self):
        pass


def _flint_registry():
    r = MCPRegistry([{"name": "flint", "transport": "stdio", "enabled": True}])
    r._available = True
    conn = _FlintConn()
    r._conns["flint"] = conn
    return r, conn


class TestChartPng(unittest.IsolatedAsyncioTestCase):
    async def test_extracts_png(self):
        reg, conn = _flint_registry()
        out = await chart_png(reg, {"type": "line", "data": [{"x": 1, "y": 2}]})
        self.assertEqual(out, PNG)
        self.assertEqual(conn.spec["spec"]["type"], "line")

    async def test_theme_passed(self):
        reg, conn = _flint_registry()
        await chart_png(reg, {"type": "bar"}, theme="dark")
        self.assertEqual(conn.spec.get("theme"), "dark")

    async def test_degrades_when_unavailable(self):
        self.assertIsNone(await chart_png(MCPRegistry([]), {"type": "line"}))


def _upi_df():
    import pandas as pd
    return pd.DataFrame({
        "transaction type": ["UPI"] * 5 + ["IMPS"] * 3 + ["NEFT"] * 2,
        "amount (INR)": list(range(10)),
        "sender_bank": ["HDFC"] * 5 + ["SBI"] * 5,
    })


class TestChartSpecs(unittest.TestCase):
    def test_distribution_of_categorical(self):
        spec = chart_spec_from_request("make a distribution of transaction type",
                                       _upi_df())
        self.assertEqual(spec["type"], "bar")
        self.assertEqual(spec["x"], "category")
        counts = {r["category"]: r["count"] for r in spec["data"]}
        self.assertEqual(counts, {"UPI": 5, "IMPS": 3, "NEFT": 2})

    def test_histogram_of_numeric(self):
        spec = chart_spec_from_request("show a histogram of amount (INR)", _upi_df())
        self.assertTrue(spec.get("data"))

    def test_scatter_and_grouped(self):
        spec = chart_spec_from_request("scatter amount (INR) vs sender_bank", _upi_df())
        self.assertEqual(spec["type"], "bar")  # categorical -> grouped mean
        self.assertIn("sender_bank", spec["title"])

    def test_resolve_column_fuzzy(self):
        df = _upi_df()
        self.assertEqual(resolve_column(df, "transaction type"), "transaction type")
        self.assertEqual(resolve_column(df, "Transaction"), "transaction type")
        self.assertIsNone(resolve_column(df, "nope"))

    def test_fallback_png(self):
        spec = chart_spec_from_request("make a distribution of transaction type",
                                       _upi_df())
        png = fallback_png(spec)
        self.assertTrue(png)
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")

    def test_chart_request_detection(self):
        from backend.main import _is_chart_request
        self.assertTrue(_is_chart_request("make a distribution of transaction type"))
        self.assertTrue(_is_chart_request("scatter amount vs bank"))
        self.assertFalse(_is_chart_request("explain the peer identification accuracy"))
        self.assertFalse(_is_chart_request("improve the experiment"))


class TestChartIntent(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import backend.project_runtime as pr
        from backend.state import runtimes
        self.tmp = Path(tempfile.mkdtemp())
        self._orig = pr.PROJECTS_DIR
        pr.PROJECTS_DIR = self.tmp
        self.rt = pr.ProjectRuntime("chartintent")
        runtimes["chartintent"] = self.rt
        _upi_df().to_csv(self.rt.dir / "upi.csv", index=False)
        from backend import main as mainmod
        self.mainmod = mainmod
        self.events = []

        async def emit(etype, payload):
            self.events.append((etype, payload))
        self.emit = emit

    async def asyncTearDown(self):
        import backend.project_runtime as pr
        from backend.state import runtimes
        pr.PROJECTS_DIR = self._orig
        runtimes.pop("chartintent", None)
        await self.rt.stop()

    def _chart_msgs(self):
        return [p["content"] for t, p in self.events if t == "assistant_message"]

    async def test_chart_intent_with_flint(self):
        from backend.mcp import MCPRegistry
        reg, _conn = _flint_registry()
        with mock.patch("backend.state.mcp_registry", reg):
            await self.mainmod._handle_chart_request(
                self.rt, self.emit, "make a distribution of transaction type")
        msgs = self._chart_msgs()
        self.assertTrue(any("artifacts/" in m and "transaction type" in m for m in msgs))
        self.assertTrue(any(a.kind == "figure"
                            for a in self.rt.artifacts.list()))

    async def test_chart_intent_fallback_without_flint(self):
        with mock.patch("backend.state.mcp_registry", MCPRegistry([])):
            await self.mainmod._handle_chart_request(
                self.rt, self.emit, "histogram of amount (INR)")
        msgs = self._chart_msgs()
        self.assertTrue(any("artifacts/" in m for m in msgs))
        # unknown column -> helpful columns message, no crash
        self.events.clear()
        with mock.patch("backend.state.mcp_registry", MCPRegistry([])):
            await self.mainmod._handle_chart_request(
                self.rt, self.emit, "make a distribution of nothing_relevant")
        help_msg = [m for m in self._chart_msgs() if "Columns:" in m]
        self.assertTrue(help_msg)


class TestChartEndpoints(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import backend.project_runtime as pr
        from backend.state import runtimes
        self.tmp = Path(tempfile.mkdtemp())
        self._orig = pr.PROJECTS_DIR
        pr.PROJECTS_DIR = self.tmp
        self.rt = pr.ProjectRuntime("flproj")
        runtimes["flproj"] = self.rt
        from backend.routers import runs as runsmod
        self.runsmod = runsmod
        self._gr = mock.patch.object(runsmod, "get_runtime",
                                     lambda name: self.rt)
        self._gr.start()

    async def asyncTearDown(self):
        import backend.project_runtime as pr
        from backend.state import runtimes
        self._gr.stop()
        pr.PROJECTS_DIR = self._orig
        runtimes.pop("flproj", None)
        await self.rt.stop()

    def _exp(self):
        eid = self.rt.store.create_experiment("FlintExp", "h", "accuracy", 0.9, True)
        for v in (0.5, 0.7, 0.95):
            self.rt.store.add_run("r", "ok", "done", 0.0, 1.0,
                                  metrics={"accuracy": float(v)},
                                  experiment_id=eid, kind="agent_run")
        return eid

    async def test_experiment_chart(self):
        eid = self._exp()
        with mock.patch("backend.state.mcp_registry", _flint_registry()[0]):
            res = await self.runsmod.experiment_chart("flproj", eid)
        self.assertTrue(res["ok"])
        self.assertEqual(res["metric"], "accuracy")
        art = self.rt.artifacts.get(res["artifact_id"])
        self.assertEqual(art.data_type, "png")
        self.assertEqual(art.kind, "figure")

    async def test_experiment_chart_fallback_without_flint(self):
        # No flint server -> the matplotlib fallback renders the chart (200).
        eid = self._exp()
        with mock.patch("backend.state.mcp_registry", MCPRegistry([])):
            res = await self.runsmod.experiment_chart("flproj", eid)
        self.assertTrue(res["ok"])
        art = self.rt.artifacts.get(res["artifact_id"])
        self.assertEqual(art.data_type, "png")

    async def test_experiment_chart_503_when_both_renderers_fail(self):
        eid = self._exp()
        with mock.patch("backend.state.mcp_registry", MCPRegistry([])):
            with mock.patch("backend.flint_charts.fallback_png",
                            return_value=None):
                res = await self.runsmod.experiment_chart("flproj", eid)
        self.assertEqual(res.status_code, 503)

    async def test_run_chart(self):
        rid = self.rt.store.add_run("p", "r", "done", 0.0, 1.0,
                                    metrics={"acc": 0.9, "loss": 0.1})
        with mock.patch("backend.state.mcp_registry", _flint_registry()[0]):
            res = await self.runsmod.run_chart("flproj", rid)
        self.assertTrue(res["ok"])
        self.assertIsNotNone(self.rt.artifacts.get(res["artifact_id"]))

    async def test_experiment_report_embeds_chart(self):
        eid = self._exp()
        with mock.patch("backend.state.mcp_registry", _flint_registry()[0]):
            res = await self.runsmod.publish_experiment_report("flproj", eid)
        self.assertIn("/artifacts/", res["report"])

    async def test_report_without_chart_when_both_renderers_fail(self):
        eid = self._exp()
        with mock.patch("backend.state.mcp_registry", MCPRegistry([])):
            with mock.patch("backend.flint_charts.fallback_png",
                            return_value=None):
                res = await self.runsmod.publish_experiment_report("flproj", eid)
        self.assertNotIn("/artifacts/", res["report"])


if __name__ == "__main__":
    unittest.main()
