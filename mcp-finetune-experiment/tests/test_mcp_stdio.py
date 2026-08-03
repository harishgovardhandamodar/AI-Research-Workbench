"""Real MCP stdio roundtrip: launch the combined server, list tools, call tools."""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))


class TestMcpStdio(unittest.IsolatedAsyncioTestCase):
    async def _client(self, project: str):
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client

        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "mcp_servers", "--server", "all", "--transport", "stdio",
                  "--project", project],
            env={"PYTHONPATH": str(SRC), "MCPFT_PROJECT_DIR": project},
        )
        ctx = stdio_client(params)
        read, write = await ctx.__aenter__()
        session = await ClientSession(read, write).__aenter__()
        await session.initialize()
        self.addAsyncCleanup(_close, session, ctx)
        return session

    async def test_combined_server_exposes_all_tools(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = await self._client(tmp)
            names = {t.name for t in (await s.list_tools()).tools}
            for expect in ("mcp.dataset.list", "mcp.dataset.generate",
                           "mcp.train.start_stage", "mcp.train.resume",
                           "mcp.train.set_hyperparams",
                           "mcp.eval.run", "mcp.eval.compare_to_paper",
                           "mcp.eval.llm_judge",
                           "mcp.experiment.create", "mcp.experiment.list_stages",
                           "mcp.experiment.rollback_to_stage",
                           "mcp.experiment.export_report"):
                self.assertIn(expect, names, f"missing tool {expect}")

    async def test_full_experiment_via_mcp(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = await self._client(tmp)

            # data
            gen = _decode(await s.call_tool("mcp.dataset.generate", {
                "name": "d", "n_trajectories": 20, "n_teacher": 20,
                "n_rubric": 10, "seed": 0}))
            self.assertEqual(gen["records"], 50)
            split = _decode(await s.call_tool("mcp.dataset.split", {"name": "d", "seed": 0}))
            train_hash = split["d_train"]["sha256"]
            self.assertTrue(_decode(await s.call_tool("mcp.dataset.validate",
                                                      {"name": "d_train"}))["valid"])

            # experiment + stages
            await s.call_tool("mcp.experiment.create", {
                "name": "demo", "base_model": "x",
                "paper": {"reported_metrics": {"accuracy": 0.8, "success_rate": 0.7}},
                "seed": 0})
            stages = _decode(await s.call_tool("mcp.experiment.list_stages", {}))
            self.assertEqual(stages["stages"][0]["id"], "stage_0")
            await s.call_tool("mcp.experiment.create_stage", {
                "stage_id": "s0", "data_hashes": [train_hash]})

            # train
            res = _decode(await s.call_tool("mcp.train.start_stage", {
                "stage_id": "s0", "new_data": "d_train", "lora_rank": 8}))
            self.assertEqual(res["adapter"]["metrics"]["mode"], "simulate")
            self.assertEqual(_decode(await s.call_tool("mcp.train.get_status",
                                                       {"stage_id": "s0"}))["status"], "done")
            self.assertEqual(len(_decode(await s.call_tool("mcp.train.list_checkpoints", {}))
                                 ["checkpoints"]), 1)

            # eval + paper compare + failures
            ev = _decode(await s.call_tool("mcp.eval.run", {"stage_id": "s0"}))
            self.assertIn("accuracy", ev["metrics"])
            cmp = _decode(await s.call_tool("mcp.eval.compare_to_paper", {"stage_id": "s0"}))
            self.assertTrue(cmp["table"])
            fc = _decode(await s.call_tool("mcp.eval.failure_cases", {"stage_id": "s0"}))
            self.assertIn("count", fc)

            # report
            rep = _decode(await s.call_tool("mcp.experiment.export_report", {"stage_id": "s0"}))
            self.assertTrue(rep["report_path"].endswith(".md"))

            # incremental stage 1
            await s.call_tool("mcp.experiment.create_stage", {"stage_id": "s1", "parent": "s0"})
            r1 = _decode(await s.call_tool("mcp.train.start_stage", {
                "stage_id": "s1", "from_adapter": "s0-adapter", "lora_rank": 16}))
            self.assertEqual(r1["adapter"]["from_adapter"], "s0-adapter")

            # rollback
            rb = _decode(await s.call_tool("mcp.experiment.rollback_to_stage", {"stage_id": "s0"}))
            self.assertEqual(rb["removed_stages"], ["s1"])


def _decode(res) -> dict:
    if getattr(res, "structuredContent", None) is not None:
        return res.structuredContent
    import json

    texts = [c.text for c in res.content if getattr(c, "type", "") == "text"]
    return json.loads(texts[0]) if texts else {}


async def _close(session, ctx):
    try:
        await session.__aexit__(None, None, None)
    except Exception:  # noqa: BLE001
        pass
    try:
        await ctx.__aexit__(None, None, None)
    except Exception:  # noqa: BLE001
        pass


if __name__ == "__main__":
    unittest.main()
