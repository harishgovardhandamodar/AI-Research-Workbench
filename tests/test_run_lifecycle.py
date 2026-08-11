"""B1/B3/B4: two-phase run lifecycle + recovery + run_id audit linkage + error
capture — the traceability/robustness additions to the chat→run pipeline."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend.agents.coordinator import Coordinator
from backend.agents.tools import ToolContext
from backend.artifacts.store import ArtifactStore
from backend.audit import make_audit
from backend.permissions import PermissionManager
from backend.store import ProjectStore


class TestRunLifecycle(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = ProjectStore(self.tmp)

    def test_begin_finish_lifecycle(self):
        eid = self.store.create_experiment("lf", "h", "acc", 0.9, True)
        rid = self.store.begin_run(prompt="p", kind="agent_run",
                                   experiment_id=eid, model="fake")
        row = self.store.get_run(rid)
        self.assertEqual(row["status"], "running")
        self.assertEqual(row["experiment_id"], eid)
        self.assertFalse(row["integrity_hash"])

        got = self.store.finish_run(
            rid=rid, reply="r", status="done", metrics={"acc": 0.91},
            tool_sequence=[{"name": "run_python"}], error=None)
        self.assertEqual(got, rid)
        row = self.store.get_run(rid)
        self.assertEqual(row["status"], "done")
        self.assertEqual(row["reply"], "r")
        self.assertEqual(row["metrics"], {"acc": 0.91})
        self.assertTrue(row["integrity_hash"])
        self.assertFalse(row["error"])
        # The hash stays stable: verify recomputes the same bytes.
        self.assertTrue(self.store.verify_run_integrity(rid)["ok"])

    def test_finish_with_error_keeps_hash_verifying(self):
        rid = self.store.begin_run(prompt="boom")
        self.store.finish_run(rid=rid, reply="", status="error",
                              error="Traceback (most recent call last):\nboom")
        row = self.store.get_run(rid)
        self.assertEqual(row["status"], "error")
        self.assertIn("boom", row["error"])
        # error is excluded from the canonical hash, so tamper-evidence still
        # holds for the record's real content.
        self.assertTrue(self.store.verify_run_integrity(rid)["ok"])

    def test_finish_unknown_id_returns_zero(self):
        self.assertEqual(self.store.finish_run(rid=9999, reply="r"), 0)

    def test_mark_interrupted_runs(self):
        a = self.store.begin_run(prompt="a")
        b = self.store.begin_run(prompt="b")
        self.store.add_run("c", "ok", "done", 1.0, 2.0)
        n = self.store.mark_interrupted_runs()
        self.assertEqual(n, 2)
        for rid in (a, b):
            row = self.store.get_run(rid)
            self.assertEqual(row["status"], "interrupted")
            self.assertIn("interrupted", row["error"])
        done = [r for r in self.store.list_runs() if r["id"] != a and r["id"] != b]
        self.assertEqual(done[0]["status"], "done")
        # Recovery is itself traceable: an audit event exists per interrupted run.
        audit_store = make_audit(self.tmp)[0]
        evs = [e for e in audit_store.query() if e["source"] == "system"]
        self.assertEqual(len(evs), 2)
        run_ids = {e["run_id"] for e in evs}
        self.assertEqual(run_ids, {str(a), str(b)})

    def test_mark_interrupted_runs_is_idempotent(self):
        self.store.begin_run(prompt="a")
        self.store.begin_run(prompt="b")
        self.assertEqual(self.store.mark_interrupted_runs(), 2)
        self.assertEqual(self.store.mark_interrupted_runs(), 0)


class TestCoordinatorRunLinkage(unittest.IsolatedAsyncioTestCase):
    """Coordinator + real store: audit events link to the pre-created run row."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = ProjectStore(self.tmp)
        self.artifacts = ArtifactStore(self.tmp)
        self.audit_store, self.emitter = make_audit(self.tmp)
        self.emitter.start()
        self.emitted = []

    async def _noop(self, t, p):
        self.emitted.append((t, p))

    def _ctx(self):
        class FakeKernel:
            async def run_code(self, code, timeout=30.0):
                return {"output": "accuracy: 0.9"}

            async def list_variables(self):
                return {}

        class FakeKernels:
            def __init__(self):
                self.python = FakeKernel()
                self.r = FakeKernel()

            async def get_env(self):
                return {"python": "3.12"}

            async def reset(self):
                pass

        return ToolContext(kernels=FakeKernels(), artifacts=self.artifacts,
                           store=self.store,
                           permissions=PermissionManager(self.store),
                           audit=self.emitter, message_id="7")

    def _record(self, r: dict):
        # Mirror main.py: finish the pre-created row, else add a fresh one.
        if r.get("id"):
            return self.store.finish_run(
                rid=int(r["id"]), reply=r.get("reply", ""),
                status=r.get("status", "done"), finished_at=r.get("finished_at"),
                tool_sequence=r.get("tool_sequence"),
                artifact_ids=r.get("artifact_ids"), metrics=r.get("metrics"),
                config=r.get("config"), label=r.get("label"),
                code=r.get("code"), env=r.get("env"),
                error=r.get("error") or None, review=r.get("review"))
        return self.store.add_run(
            prompt=r.get("prompt", ""), reply=r.get("reply", ""),
            status=r.get("status", "done"), started_at=r.get("started_at", 0.0),
            finished_at=r.get("finished_at", 0.0),
            tool_sequence=r.get("tool_sequence"),
            artifact_ids=r.get("artifact_ids"), metrics=r.get("metrics"),
            experiment_id=r.get("experiment_id") or None)

    class ToolLLM:
        def __init__(self, tool_calls):
            self.calls = 0
            self.tool_calls = tool_calls

        async def stream(self, messages, tools=None, temperature=None, on_delta=None):
            self.calls += 1
            if self.calls == 1:
                return {"role": "assistant", "content": "",
                        "tool_calls": self.tool_calls}
            return {"role": "assistant", "content": "Done."}

    async def test_audit_events_carry_run_id(self):
        llm = self.ToolLLM([{
            "id": "call_1", "type": "function",
            "function": {"name": "run_python",
                         "arguments": {"code": "print('accuracy: 0.9')"}}}])
        coordinator = Coordinator(llm, self._ctx(), emit=self._noop,
                                  persist=lambda r, c, m: None,
                                  record=self._record, max_iters=4,
                                  mcp=None, audit=self.emitter)
        result = await coordinator.run_turn([{"role": "user", "content": "go"}])
        await self.emitter.flush()
        await self.emitter.stop()
        self.assertEqual(result["text"], "Done.")
        runs = self.store.list_runs()
        self.assertEqual(len(runs), 1)
        rid = runs[0]["id"]
        self.assertEqual(runs[0]["status"], "done")
        events = self.audit_store.query()
        self.assertTrue(events)
        # Every event this turn emitted links to the run row by run_id.
        self.assertTrue(all(str(e["run_id"]) == str(rid) for e in events))
        # The per-run audit endpoint can retrieve them by run_id.
        by_run = self.audit_store.query(run_id=str(rid))
        self.assertEqual(len(by_run), len(events))

    class BoomLLM:
        async def stream(self, messages, tools=None, temperature=None, on_delta=None):
            raise RuntimeError("model exploded")

    async def test_run_records_traceback_on_error(self):
        coordinator = Coordinator(self.BoomLLM(), self._ctx(),
                                  emit=self._noop,
                                  persist=lambda r, c, m: None,
                                  record=self._record, max_iters=4,
                                  mcp=None, audit=self.emitter)
        with self.assertRaises(RuntimeError):
            await coordinator.run_turn([{"role": "user", "content": "go"}])
        await self.emitter.flush()
        await self.emitter.stop()
        runs = self.store.list_runs()
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["status"], "error")
        self.assertIn("Traceback", runs[0]["error"])
        self.assertIn("model exploded", runs[0]["error"])
        # The failed turn's audit record flags the error.
        turn_ends = [e for e in self.audit_store.query()
                     if e["method"] == "turn_end"]
        self.assertTrue(turn_ends)
        self.assertEqual(turn_ends[0]["severity"], "critical")


if __name__ == "__main__":
    unittest.main()
