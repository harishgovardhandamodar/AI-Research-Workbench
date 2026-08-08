"""LangGraph orchestrator tests (fake bound LLM + scripted check gate).

Skipped when the optional ``langgraph`` dependency is not installed. The
orchestrator reuses ``Coordinator._exec_tool_call`` for every side-effect, so
these tests focus on the graph's control flow: tool loop, check/refine gate,
cooperative Stop and the step budget.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend.agents.coordinator import Coordinator, TurnAborted
from backend.agents.tools import ToolContext
from backend.artifacts.store import ArtifactStore
from backend.permissions import PermissionManager
from backend.store import ProjectStore

try:
    from langchain_core.messages import AIMessageChunk, ToolCallChunk

    from backend.agents.orchestrator import CheckVerdict, LangChainOrchestrator

    HAVE_LANGGRAPH = True
except ImportError:  # pragma: no cover
    HAVE_LANGGRAPH = False


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


class FakeBoundLLM:
    """Scripted ``astream``: emits the queued tool calls, then a final answer."""

    def __init__(self, calls=None, final="Final answer, accuracy 0.9."):
        self.calls = list(calls or [])   # list of (name, args_json)
        self.final = final
        self.astream_calls = 0

    async def astream(self, messages):
        self.astream_calls += 1
        if self.calls:
            name, args = self.calls.pop(0)
            yield AIMessageChunk(
                content="",
                tool_call_chunks=[
                    ToolCallChunk(name=name, args=args, index=0, id="call_1")])
            return
        yield AIMessageChunk(content=self.final)


class ScriptedVerdict:
    def __init__(self, verdicts):
        self.verdicts = list(verdicts)
        self.calls = 0

    async def __call__(self, state):
        self.calls += 1
        if self.verdicts:
            return self.verdicts.pop(0)
        return CheckVerdict(valid=True)


@unittest.skipUnless(HAVE_LANGGRAPH, "langgraph not installed (pip install -e '.[agent]')")
class TestLangGraphOrchestrator(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = ProjectStore(self.tmp)
        self.artifacts = ArtifactStore(self.tmp)
        self.ctx = ToolContext(kernels=FakeKernels(), artifacts=self.artifacts,
                               store=self.store,
                               permissions=PermissionManager(self.store))
        self.emitted = []
        self.recorded = None

    async def _emit(self, t: str, p: dict):
        self.emitted.append(t)

    def _set_record(self, r: dict):
        self.recorded = r
        return 1

    def _coord(self, **kw):
        reliability = kw.pop("reliability", True)
        max_iters = kw.pop("max_iters", 4)
        coord = Coordinator(object(), self.ctx, emit=self._emit,
                            persist=lambda r, c, m: None,
                            record=lambda r: self._set_record(r),
                            max_iters=max_iters, mcp=None, **kw)
        coord.orchestrator = "langgraph"
        coord.orchestrator_reliability = reliability
        coord.model_name = "qwen3.6:latest"
        return coord

    async def test_tool_then_answer_with_check_gate(self):
        coord = self._coord()
        orch = LangChainOrchestrator(coord)
        fake = FakeBoundLLM([("run_python", '{"code":"print(1)"}')])
        orch._bound_llm = lambda: fake
        orch.check_verdict_fn = ScriptedVerdict([CheckVerdict(valid=True)])

        result = await orch.run([{"role": "user", "content": "run the experiment"}])
        self.assertEqual(result["text"], "Final answer, accuracy 0.9.")
        self.assertEqual(result["model"], "qwen3.6:latest")
        self.assertIn("tool_start", self.emitted)
        self.assertIn("tool_result", self.emitted)
        self.assertIsNotNone(self.recorded)
        self.assertEqual(self.recorded["status"], "done")
        self.assertEqual([t["name"] for t in self.recorded["tool_sequence"]],
                         ["run_python"])
        self.assertTrue(self.recorded["tool_sequence"][0]["ok"])

    async def test_check_gate_refines_invalid_answer(self):
        coord = self._coord()
        orch = LangChainOrchestrator(coord)
        fake = FakeBoundLLM([("run_python", '{"code":"print(1)"}')],
                            final="Corrected final answer.")
        orch._bound_llm = lambda: fake
        verdicts = ScriptedVerdict([
            CheckVerdict(valid=False, feedback="The accuracy 0.95 is not in the outputs."),
            CheckVerdict(valid=True),
        ])
        orch.check_verdict_fn = verdicts

        result = await orch.run([{"role": "user", "content": "run the experiment"}])
        # tool call -> draft -> (reflect) -> corrected final -> check(valid)
        self.assertEqual(fake.astream_calls, 3)
        self.assertEqual(verdicts.calls, 2)
        self.assertEqual(result["text"], "Corrected final answer.")

    async def test_reliability_off_skips_check_gate(self):
        coord = self._coord(reliability=False)
        orch = LangChainOrchestrator(coord)
        fake = FakeBoundLLM([("run_python", '{"code":"print(1)"}')])
        orch._bound_llm = lambda: fake
        verdicts = ScriptedVerdict([])
        orch.check_verdict_fn = verdicts

        result = await orch.run([{"role": "user", "content": "run the experiment"}])
        self.assertEqual(result["text"], "Final answer, accuracy 0.9.")
        self.assertEqual(fake.astream_calls, 2)   # tool + answer, no check LLM
        self.assertEqual(verdicts.calls, 0)       # gate not reached

    async def test_abort_raises_and_records_stopped(self):
        checks = {"n": 0}

        def check_abort():
            checks["n"] += 1
            return checks["n"] > 1  # allow first tool, then Stop

        coord = self._coord(check_abort=check_abort)
        orch = LangChainOrchestrator(coord)
        fake = FakeBoundLLM([("run_python", '{"code":"print(1)"}')])
        orch._bound_llm = lambda: fake
        orch.check_verdict_fn = ScriptedVerdict([])

        with self.assertRaises(TurnAborted):
            await orch.run([{"role": "user", "content": "run it"}])
        self.assertIsNotNone(self.recorded)
        self.assertEqual(self.recorded["status"], "stopped")
        self.assertEqual(len(self.recorded["tool_sequence"]), 1)

    async def test_budget_exhaustion_falls_back(self):
        # The scripted LLM never finishes: always asks for a tool.
        class NeverDone(FakeBoundLLM):
            async def astream(self, messages):
                self.astream_calls += 1
                yield AIMessageChunk(
                    content="",
                    tool_call_chunks=[
                        ToolCallChunk(name="run_python", args='{"code":"x=1"}',
                                      index=0, id="call_1")])

        coord = self._coord(max_iters=2)
        orch = LangChainOrchestrator(coord)
        orch._bound_llm = lambda: NeverDone()
        orch.check_verdict_fn = ScriptedVerdict([])

        result = await orch.run([{"role": "user", "content": "loop forever"}])
        self.assertIn("maximum number of tool steps", result["text"])
        self.assertIsNotNone(self.recorded)
        self.assertEqual(self.recorded["status"], "done")
        self.assertEqual(len(self.recorded["tool_sequence"]), 2)

    async def test_parity_with_classic_loop_side_effects(self):
        # Same script through both loops must yield the same tool trail and the
        # same set of streaming events.
        batches = [
            [{"id": "c1", "type": "function",
              "function": {"name": "run_python",
                           "arguments": {"code": "x = 1"}}}],
        ]

        class ClassicLLM:
            async def stream(self, messages, tools=None, temperature=None, on_delta=None):
                if batches:
                    return {"role": "assistant", "content": "",
                            "tool_calls": batches.pop(0)}
                return {"role": "assistant", "content": "Done."}

        coord_c = Coordinator(ClassicLLM(), self.ctx, emit=self._emit,
                              persist=lambda r, c, m: None,
                              record=lambda r: self._set_record(r),
                              max_iters=4, mcp=None)
        classic_res = await coord_c.run_turn([{"role": "user", "content": "run it"}])
        classic_emitted = list(self.emitted)
        classic_tools = list(self.recorded["tool_sequence"])

        self.emitted = []
        self.recorded = None
        coord_g = self._coord(reliability=False)
        orch = LangChainOrchestrator(coord_g)
        fake = FakeBoundLLM([("run_python", '{"code":"x = 1"}')], final="Done.")
        orch._bound_llm = lambda: fake
        orch.check_verdict_fn = ScriptedVerdict([])
        graph_res = await orch.run([{"role": "user", "content": "run it"}])

        self.assertEqual([t["name"] for t in classic_tools],
                         [t["name"] for t in self.recorded["tool_sequence"]])
        self.assertEqual({t["ok"] for t in classic_tools},
                         {t["ok"] for t in self.recorded["tool_sequence"]})
        self.assertIn("tool_start", classic_emitted)
        self.assertIn("tool_start", self.emitted)
        self.assertIn("tool_result", self.emitted)
        self.assertEqual(classic_res["text"], graph_res["text"])


if __name__ == "__main__":
    unittest.main()
