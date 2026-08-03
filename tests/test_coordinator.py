"""Coordinator tool loop with a fake LLM: executes a tool call, extracts
metrics, and records a run through the record callback."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend.agents.coordinator import Coordinator
from backend.agents.tools import ToolContext
from backend.artifacts.store import ArtifactStore
from backend.permissions import PermissionManager
from backend.store import ProjectStore


class FakeKernel:
    async def run_code(self, code, timeout=30.0):
        return {"output": "accuracy: 0.9"}

    async def list_variables(self):
        return {"x": 1}


class FakeKernels:
    def __init__(self):
        self.python = FakeKernel()
        self.r = FakeKernel()

    async def get_env(self):
        return {"python": "3.12"}

    async def reset(self):
        pass


class FakeLLM:
    def __init__(self):
        self.calls = 0
        self.tool_calls = [{
            "id": "call_1",
            "type": "function",
            "function": {"name": "run_python",
                         "arguments": {"code": "print('hello')"}},
        }]

    async def stream(self, messages, tools=None, temperature=None, on_delta=None):
        self.calls += 1
        if self.calls == 1:
            return {"role": "assistant", "content": "", "tool_calls": self.tool_calls}
        return {"role": "assistant", "content": "Done, ran the experiment."}


class TestCoordinatorLoop(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = ProjectStore(self.tmp)
        self.artifacts = ArtifactStore(self.tmp)
        self.ctx = ToolContext(kernels=FakeKernels(), artifacts=self.artifacts,
                               store=self.store,
                               permissions=PermissionManager(self.store))
        self.emitted = []
        self.recorded = None
        self.emitted_payloads = []

    async def _emit(self, t: str, p: dict):
        self.emitted.append(t)
        self.emitted_payloads.append((t, p))

    async def test_tool_loop_runs_tool_and_records_run(self):
        coordinator = Coordinator(FakeLLM(), self.ctx, emit=self._emit,
                                  persist=lambda r, c, m: None,
                                  record=lambda r: self._set_record(r),
                                  max_iters=4, mcp=None)
        result = await coordinator.run_turn([
            {"role": "user", "content": "run the experiment"},
        ])
        self.assertEqual(result["text"], "Done, ran the experiment.")
        self.assertIn("tool_start", self.emitted)
        self.assertIn("tool_result", self.emitted)
        self.assertIsNotNone(self.recorded)
        self.assertEqual(self.recorded["status"], "done")
        self.assertEqual(self.recorded["metrics"], {"accuracy": 0.9})
        self.assertEqual(len(self.recorded["tool_sequence"]), 1)
        self.assertEqual(self.recorded["tool_sequence"][0]["name"], "run_python")
        self.assertTrue(self.recorded["tool_sequence"][0]["ok"])

    def _set_record(self, r: dict):
        self.recorded = r
        return 1

    async def test_rich_status_payloads(self):
        coordinator = Coordinator(FakeLLM(), self.ctx, emit=self._emit,
                                  persist=lambda r, c, m: None,
                                  record=lambda r: self._set_record(r),
                                  max_iters=4, mcp=None)
        await coordinator.run_turn([
            {"role": "user", "content": "run the experiment"},
        ])
        statuses = [p for t, p in self.emitted_payloads if t == "status"]
        self.assertTrue(statuses, "coordinator should emit status events")
        self.assertEqual(statuses[0]["phase"], "starting")
        self.assertEqual(statuses[0]["agent"], "Fox")
        self.assertIn("model", statuses[0])
        tool_status = next((p for p in statuses if p.get("phase") == "tool"), None)
        self.assertIsNotNone(tool_status)
        self.assertEqual(tool_status["tool"], "run_python")
        self.assertEqual(tool_status["mcp"], "")
        self.assertIn("skills", tool_status)

    async def test_tool_failure_records_error_status(self):
        def boom_tool(code):
            raise RuntimeError("kernel exploded")
        coordinator = Coordinator(FakeLLM(), self.ctx, emit=self._emit,
                                  persist=lambda r, c, m: None,
                                  record=lambda r: self._set_record(r),
                                  max_iters=4, mcp=None)
        coordinator.tools["run_python"] = boom_tool
        result = await coordinator.run_turn([
            {"role": "user", "content": "run the experiment"},
        ])
        self.assertIsNotNone(self.recorded)
        seq = self.recorded["tool_sequence"]
        self.assertEqual(seq[0]["ok"], False)
        self.assertIn("error", seq[0]["result"])


if __name__ == "__main__":
    unittest.main()
