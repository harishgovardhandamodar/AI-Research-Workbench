"""Round-8 tests: run integrity hashes, run↔trace linkage, and the coordinator
recording the turn's message_id."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend.store import ProjectStore, _canonical_run


class IntegrityTests(unittest.TestCase):
    def setUp(self):
        self.store = ProjectStore(Path(tempfile.mkdtemp()))

    def test_add_run_records_message_id_and_hash(self):
        rid = self.store.add_run(
            "prompt", "reply", "done", 1.0, 2.0,
            metrics={"acc": 0.7}, config={"eps": 1},
            code=[{"name": "run_python", "code": "print(1)"}],
            env={"python": "3.12"}, message_id=42)
        run = self.store.get_run(rid, include_code=True)
        self.assertEqual(run["message_id"], 42)
        self.assertTrue(run["integrity_hash"])

    def test_verify_ok(self):
        rid = self.store.add_run("p", "r", "done", 1.0, 2.0,
                                 metrics={"acc": 0.7})
        res = self.store.verify_run_integrity(rid)
        self.assertTrue(res["ok"])

    def test_verify_detects_tampering(self):
        rid = self.store.add_run("p", "r", "done", 1.0, 2.0,
                                 metrics={"acc": 0.7})
        self.store._conn.execute("UPDATE runs SET reply='TAMPERED' WHERE id=?", (rid,))
        self.store._conn.commit()
        res = self.store.verify_run_integrity(rid)
        self.assertFalse(res["ok"])

    def test_legacy_run_no_hash(self):
        # Simulate a pre-round-8 row: wipe the hash.
        rid = self.store.add_run("p", "r", "done", 1.0, 2.0)
        self.store._conn.execute("UPDATE runs SET integrity_hash=NULL WHERE id=?", (rid,))
        self.store._conn.commit()
        res = self.store.verify_run_integrity(rid)
        self.assertIsNone(res["ok"])

    def test_canonical_roundtrip_stable(self):
        run = {"prompt": "p", "reply": "r", "status": "done", "kind": "agent_run",
               "label": None, "experiment_id": 1, "parent_run_id": None,
               "model": "", "git_commit": "", "config": {"eps": 1},
               "metrics": {"acc": 0.7}, "tool_sequence": [],
               "code": [], "env": {"python": "3.12"}}
        self.assertEqual(_canonical_run(run), _canonical_run(run))
        changed = dict(run, metrics={"acc": 0.8})
        self.assertNotEqual(_canonical_run(run), _canonical_run(changed))


class TraceLinkageTests(unittest.IsolatedAsyncioTestCase):
    async def test_coordinator_records_message_id(self):
        from backend.agents.coordinator import Coordinator
        from backend.agents.tools import ToolContext
        from backend.artifacts.store import ArtifactStore
        from backend.permissions import PermissionManager
        from tests.test_coordinator import FakeKernels
        from tests.test_experiment_loop import StubCtx

        tmp = Path(tempfile.mkdtemp())
        store = ProjectStore(tmp)
        artifacts = ArtifactStore(tmp)
        ctx = ToolContext(kernels=FakeKernels(), artifacts=artifacts, store=store,
                          permissions=PermissionManager(store))
        records = []

        class LLM:
            calls = 0

            async def stream(self, messages, tools=None, temperature=None, on_delta=None):
                self.calls += 1
                if self.calls == 1:
                    return {"role": "assistant", "content": "", "tool_calls": [{
                        "id": "c1", "type": "function",
                        "function": {"name": "run_python",
                                     "arguments": {"code": "print('hi')"}}}]}
                return {"role": "assistant", "content": "done"}

        coord = Coordinator(LLM(), ctx, emit=None,
                            persist=lambda r, c, m: None,
                            record=lambda r: records.append(r), max_iters=2, mcp=None)
        ctx.message_id = "42"
        await coord.run_turn([{"role": "user", "content": "go"}])
        self.assertTrue(records)
        self.assertEqual(records[0]["message_id"], 42)
        # The stored run carries the trace id too.
        rid = store.add_run("p", "r", "done", 1.0, 2.0,
                            message_id=records[0]["message_id"])
        self.assertEqual(store.get_run(rid)["message_id"], 42)


if __name__ == "__main__":
    unittest.main()
