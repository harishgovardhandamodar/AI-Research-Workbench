"""Items 1-3: unified plan lineage (runs.plan_id), first-class tool runs
(two-phase begin/finish), and graceful drain in ProjectRuntime.stop()."""

from __future__ import annotations

import asyncio
import json
import logging
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from backend.project_runtime import ProjectRuntime
from backend.store import ProjectStore


async def _noop_emit(t, p):
    return None


class TestPlanLineage(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = ProjectStore(self.tmp)

    def test_add_run_plan_linkage(self):
        eid = self.store.create_experiment("p", "h", "acc", 0.9, True)
        rid = self.store.add_run(
            "p", "r", "done", 1.0, 2.0, metrics={"acc": 0.9},
            experiment_id=eid, plan_id="abc123", plan_step_id="step-2")
        row = self.store.get_run(rid)
        self.assertEqual(row["plan_id"], "abc123")
        self.assertEqual(row["plan_step_id"], "step-2")
        self.assertEqual(row["experiment_id"], eid)
        # plan linkage is lineage metadata, not content: hash still verifies.
        self.assertTrue(self.store.verify_run_integrity(rid)["ok"])

    def test_begin_finish_plan_linkage(self):
        rid = self.store.begin_run(prompt="p", plan_id="abc123",
                                   plan_step_id="step-1")
        self.assertEqual(self.store.get_run(rid)["plan_id"], "abc123")
        self.store.finish_run(rid=rid, reply="r", status="done")
        row = self.store.get_run(rid)
        self.assertEqual(row["plan_id"], "abc123")
        self.assertEqual(row["plan_step_id"], "step-1")
        # finish_run keeps a plan set at begin; an explicit override wins.
        rid2 = self.store.begin_run(prompt="q")
        self.store.finish_run(rid=rid2, reply="r", status="done",
                              plan_id="overridden")
        self.assertEqual(self.store.get_run(rid2)["plan_id"], "overridden")

    def test_upsert_and_query_plan_records(self):
        plan = {"id": "abc123", "experiment_id": "peer", "name": "Peer",
                "request": "run peer", "dataset": "upi.csv", "seed": 42,
                "status": "WAITING_APPROVAL", "parent_id": "",
                "created_at": 1.0, "updated_at": 2.0}
        self.store.upsert_plan(plan)
        rec = self.store.get_plan_record("abc123")
        self.assertIsNotNone(rec)
        self.assertEqual(rec["status"], "WAITING_APPROVAL")
        self.assertEqual(rec["seed"], 42)
        # Update + list round-trips.
        self.store.upsert_plan({**plan, "status": "DONE",
                                "result": {"n": 10}, "metrics": {"acc": 0.5}})
        rec = self.store.get_plan_record("abc123")
        self.assertEqual(rec["status"], "DONE")
        self.assertEqual(rec["metrics"], {"acc": 0.5})
        self.assertEqual(len(self.store.list_plan_records()), 1)
        self.assertEqual(len(self.store.list_plan_records(status="DONE")), 1)
        self.assertEqual(len(self.store.list_plan_records(status="FAILED")), 0)

    def test_plan_runs_lineage(self):
        self.store.upsert_plan({"id": "p1", "experiment_id": "x", "name": "X",
                                "status": "DONE", "created_at": 1.0,
                                "updated_at": 2.0})
        self.store.add_run("p", "r", "done", 1.0, 2.0, plan_id="p1")
        self.store.add_run("p", "r", "done", 1.0, 2.0, plan_id="p1")
        self.store.add_run("p", "r", "done", 1.0, 2.0)  # unrelated
        runs = self.store.plan_runs("p1")
        self.assertEqual(len(runs), 2)
        self.assertTrue(all(r["plan_id"] == "p1" for r in runs))


class TestToolRunFirstClass(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = ProjectStore(self.tmp)
        self.ctx = SimpleNamespace(store=self.store, message_id="7")

    def _record(self, **kw):
        from backend.agents.tools import _record_tool_run
        return _record_tool_run(self.ctx, prompt="p", reply="r", **kw)

    def test_record_tool_run_is_first_class(self):
        rid = self._record(kind="sweep", label="point-1",
                           metrics={"acc": 0.8},
                           tool_sequence=[{"name": "run_sweep", "ok": True}])
        self.assertIsNotNone(rid)
        row = self.store.get_run(rid)
        self.assertEqual(row["status"], "done")
        self.assertEqual(row["kind"], "sweep")
        self.assertEqual(row["metrics"], {"acc": 0.8})
        self.assertTrue(row["integrity_hash"])
        self.assertTrue(self.store.verify_run_integrity(rid)["ok"])

    def test_record_tool_run_error_capture(self):
        rid = self._record(kind="sweep", status="failed",
                           error="boom: division by zero")
        row = self.store.get_run(rid)
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["error"], "boom: division by zero")
        # error excluded from the hash -> tamper-evidence still holds.
        self.assertTrue(self.store.verify_run_integrity(rid)["ok"])


class TestGracefulDrain(unittest.IsolatedAsyncioTestCase):
    """ProjectRuntime.stop() drains background work before stopping the stack."""

    def _fake(self, campaign_task=None, eval_task=None):
        stopped = {"audit": False, "kernels": False, "monitor": False}
        audit = SimpleNamespace(stopped=stopped)
        kernels = SimpleNamespace(stopped=stopped)
        async def stop_audit():
            stopped["audit"] = True
        async def stop_kernels():
            stopped["kernels"] = True
        audit.stop = stop_audit
        kernels.stop = stop_kernels
        return SimpleNamespace(
            name="p", campaign_stop=False, _campaign_task=campaign_task,
            _eval_task=eval_task, _finetune_monitor=None,
            _plan_tasks={},
            audit_emitter=audit, kernels=kernels,
            stop_finetune_monitor=lambda: stopped.__setitem__("monitor", True),
            drain_plans=lambda: asyncio.sleep(0),
            _stopped=stopped)

    async def test_stop_awaits_cooperative_task(self):
        # A task that obeys campaign_stop should be allowed to finish gracefully.
        finished = {"done": False}
        async def work():
            while not self.fake.campaign_stop:
                await asyncio.sleep(0.01)
            finished["done"] = True
        self.fake = self._fake()
        self.fake._campaign_task = asyncio.create_task(work())
        await ProjectRuntime.stop(self.fake, drain_timeout=5.0)
        self.assertTrue(finished["done"])
        self.assertTrue(self.fake._campaign_task.done())
        self.assertTrue(self.fake._stopped["kernels"])
        self.assertTrue(self.fake._stopped["audit"])
        self.assertTrue(self.fake._stopped["monitor"])

    async def test_stop_cancels_straggler(self):
        # A task that ignores campaign_stop is cancelled after the drain window.
        self.fake = self._fake()
        async def stubborn():
            while True:
                await asyncio.sleep(1.0)
        self.fake._eval_task = asyncio.create_task(stubborn())
        await ProjectRuntime.stop(self.fake, drain_timeout=0.1)
        self.assertTrue(self.fake._eval_task.cancelled() or self.fake._eval_task.done())


class TestMessageIntegrityChain(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = ProjectStore(self.tmp)

    def test_add_message_hashes_and_verifies(self):
        self.store.add_message("user", "hello", {"tags": ["hi"]})
        self.store.add_message("assistant", "world")
        res = self.store.verify_message_chain()
        self.assertTrue(res["ok"])
        self.assertEqual(res["verified"], 2)
        self.assertEqual(res["errors"], [])
        msgs = self.store.list_messages()
        self.assertTrue(msgs[0]["integrity_hash"])
        # Chained: second message's prev_hash == first message's hash.
        self.assertEqual(msgs[1]["prev_hash"], msgs[0]["integrity_hash"])

    def test_chain_detects_edit(self):
        self.store.add_message("user", "original")
        self.store.add_message("assistant", "reply")
        # Tamper: rewrite the content of the first message in place.
        self.store._conn.execute(
            "UPDATE messages SET content=? WHERE role='user'", ("tampered",))
        self.store._conn.commit()
        res = self.store.verify_message_chain()
        self.assertFalse(res["ok"])
        self.assertTrue(any(e["reason"] == "hash mismatch"
                           for e in res["errors"]))

    def test_legacy_rows_are_skipped_not_errors(self):
        # Pre-migration row (no hashes) inserted directly, then a new message.
        self.store._conn.execute(
            "INSERT INTO messages (role, content, created_at, meta)"
            " VALUES ('user','legacy',1.0,'{}')")
        self.store._conn.commit()
        self.store.add_message("assistant", "new")
        res = self.store.verify_message_chain()
        self.assertTrue(res["ok"])
        self.assertEqual(res["verified"], 1)
        self.assertEqual(res["skipped"], 1)


class TestKernelPoolTracking(unittest.IsolatedAsyncioTestCase):
    """Item 4: ToolContext tracks ephemeral kernels so aborts can't leak them."""

    def setUp(self):
        from backend.agents.tools import ToolContext
        from backend.artifacts.store import ArtifactStore
        from backend.permissions import PermissionManager
        self.store = ProjectStore(Path(tempfile.mkdtemp()))
        self.artifacts = ArtifactStore(Path(tempfile.mkdtemp()))
        self.ctx = ToolContext(
            kernels=SimpleNamespace(pool=lambda n: [], stop_pool=self._stop),
            artifacts=self.artifacts, store=self.store,
            permissions=PermissionManager(self.store))
        self.stopped = []

    async def _stop(self, kernels):
        self.stopped.extend(kernels)

    async def test_stop_kernels_stops_registered(self):
        self.ctx.register_kernels(["k1", "k2"])
        await self.ctx.stop_kernels()
        self.assertEqual(sorted(self.stopped), ["k1", "k2"])
        self.assertEqual(self.ctx.active_kernels, [])

    async def test_stop_kernels_idempotent(self):
        self.ctx.register_kernels(["k1"])
        await self.ctx.stop_kernels()
        await self.ctx.stop_kernels()
        self.assertEqual(self.stopped, ["k1"])

    async def test_unregister_removes_without_stopping(self):
        self.ctx.register_kernels(["k1", "k2"])
        self.ctx.unregister_kernels(["k1"])
        self.assertEqual(self.ctx.active_kernels, ["k2"])


class TestLogCorrelation(unittest.TestCase):
    """Item 7: the context formatter appends project/run ids to log records."""

    def setUp(self):
        from backend import logging_config as lc
        self.lc = lc
        lc.clear_log_context()

    def tearDown(self):
        self.lc.clear_log_context()

    def test_context_appears_in_formatted_line(self):
        fmt = self.lc._context_formatter()
        self.lc.set_log_context(project="p1", run="42")
        rec = logging.LogRecord("fox.test", logging.INFO, "", 0,
                                "hello", (), None)
        line = fmt.format(rec)
        self.assertIn("project=p1", line)
        self.assertIn("run=42", line)
        self.assertIn("hello", line)

    def test_clear_removes_key(self):
        fmt = self.lc._context_formatter()
        self.lc.set_log_context(project="p1", run="42")
        self.lc.clear_log_context("run")
        rec = logging.LogRecord("fox.test", logging.INFO, "", 0, "x", (), None)
        line = fmt.format(rec)
        self.assertIn("project=p1", line)
        self.assertNotIn("run=42", line)

    def test_empty_context_no_suffix(self):
        fmt = self.lc._context_formatter()
        rec = logging.LogRecord("fox.test", logging.INFO, "", 0, "x", (), None)
        line = fmt.format(rec)
        self.assertNotIn("project=", line)
        self.assertNotIn("run=", line)


class TestTranscriptFidelity(unittest.IsolatedAsyncioTestCase):
    """Item 6: each turn persists the exact LLM request as a transcript artifact."""

    def setUp(self):
        from backend.agents.coordinator import Coordinator
        from backend.agents.tools import ToolContext
        from backend.artifacts.store import ArtifactStore
        from backend.audit import make_audit
        from backend.permissions import PermissionManager
        self.tmp = Path(tempfile.mkdtemp())
        self.store = ProjectStore(self.tmp)
        self.artifacts = ArtifactStore(self.tmp)
        self.audit_store, self.emitter = make_audit(self.tmp)
        self.emitter.start()

        class FakeKernel:
            async def run_code(self, code, timeout=30.0):
                return {"output": "ok"}

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

        self.ctx = ToolContext(
            kernels=FakeKernels(), artifacts=self.artifacts, store=self.store,
            permissions=PermissionManager(self.store),
            audit=self.emitter, message_id="7")
        self._record_calls = []

    def _record(self, r: dict):
        if r.get("id"):
            self.store.finish_run(
                rid=int(r["id"]), reply=r.get("reply", ""),
                status=r.get("status", "done"), finished_at=r.get("finished_at"),
                tool_sequence=r.get("tool_sequence"),
                artifact_ids=r.get("artifact_ids"), metrics=r.get("metrics"),
                config=r.get("config"), label=r.get("label"),
                code=r.get("code"), env=r.get("env"),
                error=r.get("error") or None, review=r.get("review"))
            return int(r["id"])
        return self.store.add_run(
            prompt=r.get("prompt", ""), reply=r.get("reply", ""),
            status=r.get("status", "done"), started_at=r.get("started_at", 0.0),
            finished_at=r.get("finished_at", 0.0),
            tool_sequence=r.get("tool_sequence"),
            artifact_ids=r.get("artifact_ids"), metrics=r.get("metrics"),
            experiment_id=r.get("experiment_id") or None)

    class ToolLLM:
        def __init__(self):
            self.calls = 0

        async def stream(self, messages, tools=None, temperature=None, on_delta=None):
            self.calls += 1
            if self.calls == 1:
                return {"role": "assistant", "content": "",
                        "tool_calls": [{"id": "c1", "type": "function",
                                        "function": {"name": "run_python",
                                                     "arguments": {"code": "print('hi')"}}}]}
            return {"role": "assistant", "content": "Done."}

    async def test_turn_persists_transcript_artifact(self):
        from backend.agents.coordinator import Coordinator
        coordinator = Coordinator(self.ToolLLM(), self.ctx, emit=self._noop,
                                  persist=lambda r, c, m: None,
                                  record=self._record, max_iters=4,
                                  mcp=None, audit=self.emitter)
        result = await coordinator.run_turn([{"role": "user", "content": "go"}])
        await self.emitter.flush()
        await self.emitter.stop()
        self.assertEqual(result["text"], "Done.")
        arts = self.artifacts.list(limit=100)
        transcripts = [a for a in arts if getattr(a, "kind", "") == "transcript"]
        self.assertEqual(len(transcripts), 1)
        runs = self.store.list_runs()
        self.assertEqual(len(runs), 1)
        # The transcript is linked to the run it belongs to.
        self.assertEqual(transcripts[0].run_id, str(runs[0]["id"]))
        # And the artifact bytes contain the exact request (params + messages).
        path = Path(transcripts[0].data_path)
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["params"]["model"], coordinator.model_name)
        self.assertEqual(payload["messages"][0]["role"], "user")
        self.assertTrue(any(m.get("tool_calls") for m in payload["messages"]))

    async def _noop(self, t, p):
        return None


class TestPlanDedup(unittest.IsolatedAsyncioTestCase):
    """A1: the runtime plan registry dedups across chat and REST executors."""

    def setUp(self):
        import functools
        self.tmp = Path(tempfile.mkdtemp())
        self.store = ProjectStore(self.tmp)
        self.rt = SimpleNamespace(name="p", _plan_tasks={})
        self.rt.plan_running = functools.partial(ProjectRuntime.plan_running, self.rt)
        self.rt.launch_plan = functools.partial(ProjectRuntime.launch_plan, self.rt)
        self.rt.cancel_plan_task = functools.partial(
            ProjectRuntime.cancel_plan_task, self.rt)
        self.rt.drain_plans = functools.partial(ProjectRuntime.drain_plans, self.rt)

    async def test_launch_dedups(self):
        started = {"n": 0}

        async def work():
            started["n"] += 1
            await asyncio.sleep(0.05)

        ok1, _ = self.rt.launch_plan("planA", work())
        await asyncio.sleep(0)  # let the scheduled task actually start
        ok2, _ = self.rt.launch_plan("planA", work())
        self.assertTrue(ok1)
        self.assertFalse(ok2)  # second launch rejected while first is in flight
        self.assertEqual(started["n"], 1)
        await asyncio.sleep(0.1)
        ok3, _ = self.rt.launch_plan("planA", work())
        self.assertTrue(ok3)  # registry cleared after completion
        await self.rt.drain_plans()

    async def test_cancel_and_drain(self):
        async def stubborn():
            await asyncio.sleep(10.0)

        ok, _ = self.rt.launch_plan("planB", stubborn())
        self.assertTrue(ok)
        self.assertTrue(self.rt.plan_running("planB"))
        self.assertTrue(self.rt.cancel_plan_task("planB"))
        await self.rt.drain_plans()
        self.assertFalse(self.rt.plan_running("planB"))


class TestArtifactIntegrity(unittest.TestCase):
    """C: artifact bytes are tamper-evident."""

    def setUp(self):
        from backend.artifacts.store import ArtifactStore
        self.tmp = Path(tempfile.mkdtemp())
        self.store = ArtifactStore(self.tmp)

    def test_verify_roundtrip_and_tamper(self):
        from backend.artifacts.store import Artifact
        art = Artifact(kind="figure", name="f", data_type="png")
        self.store.add_artifact(art, data=b"PNGDATA", data_type="png")
        self.assertTrue(self.store.verify_artifact(art.id)["ok"])
        Path(art.data_path).write_bytes(b"EVIL!")
        res = self.store.verify_artifact(art.id)
        self.assertFalse(res["ok"])
        summary = self.store.verify_artifacts()
        self.assertFalse(summary["ok"])
        self.assertEqual(summary["mismatches"], 1)

    def test_linked_artifact_still_verifies(self):
        from backend.artifacts.store import Artifact
        art = Artifact(kind="report", name="r")
        self.store.add_artifact(art, data=b"report", data_type="text")
        self.store.link_artifacts([art.id], message_id="m", run_id="42")
        self.assertTrue(self.store.verify_artifact(art.id)["ok"])


class TestSweepPointAudit(unittest.IsolatedAsyncioTestCase):
    """B: each sweep point emits an audit event linked to its own run_id."""

    def setUp(self):
        from backend.agents.tools import ToolContext
        from backend.artifacts.store import ArtifactStore
        from backend.audit import make_audit
        from backend.permissions import PermissionManager
        from tests.test_round3 import PoolKernels
        self.tmp = Path(tempfile.mkdtemp())
        self.store = ProjectStore(self.tmp)
        self.eid = self.store.create_experiment("sweep", "", "acc", 0.9, True)
        self.audit_store, self.emitter = make_audit(self.tmp)
        self.emitter.start()
        self.ctx = ToolContext(
            kernels=PoolKernels(), artifacts=ArtifactStore(self.tmp),
            store=self.store, permissions=PermissionManager(self.store),
            audit=self.emitter, message_id="5")
        self.ctx.experiment_id = str(self.eid)

    async def test_sweep_points_emit_audit_events(self):
        from backend.agents.tools import _run_sweep
        await _run_sweep(self.ctx, "report_metric('acc', 0.8)",
                         [{"eps": 1}, {"eps": 2}], label_prefix="eps")
        await self.emitter.flush()
        await self.emitter.stop()
        events = self.audit_store.query()
        points = [e for e in events if e["method"] == "sweep_point"]
        self.assertEqual(len(points), 2)
        runs = [r for r in self.store.experiment_runs(self.eid)
                if r["kind"] == "sweep"]
        self.assertEqual(len(runs), 2)
        # Each point event links to its own run_id.
        point_ids = {e["run_id"] for e in points}
        run_ids = {str(r["id"]) for r in runs}
        self.assertEqual(point_ids, run_ids)


class _AlwaysToolLLM:
    """Keeps requesting tool calls (with a per-iteration delay) so a turn never
    finishes on its own and the wall-clock budget eventually trips."""

    async def stream(self, messages, tools=None, temperature=None, on_delta=None):
        await asyncio.sleep(0.03)
        return {"role": "assistant", "content": "",
                "tool_calls": [{"id": "c1", "type": "function",
                                "function": {"name": "run_python",
                                             "arguments": {"code": "print(1)"}}}]}


class TestTurnBudget(unittest.IsolatedAsyncioTestCase):
    """Item 9: a whole-turn wall-clock budget stops degenerate turns gracefully."""

    def setUp(self):
        from backend.agents.coordinator import Coordinator
        from backend.agents.tools import ToolContext
        from backend.artifacts.store import ArtifactStore
        from backend.audit import make_audit
        from backend.permissions import PermissionManager
        from tests.test_round3 import FakeKernels
        self.tmp = Path(tempfile.mkdtemp())
        self.store = ProjectStore(self.tmp)
        self.artifacts = ArtifactStore(self.tmp)
        self.audit_store, self.emitter = make_audit(self.tmp)
        self.emitter.start()
        self.ctx = ToolContext(
            kernels=FakeKernels(), artifacts=self.artifacts, store=self.store,
            permissions=PermissionManager(self.store), audit=self.emitter,
            message_id="7")
        self.Coordinator = Coordinator

    def _record(self, r: dict):
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

    async def test_budget_stops_turn_and_records_stopped(self):
        coordinator = self.Coordinator(
            _AlwaysToolLLM(), self.ctx, emit=_noop_emit,
            persist=lambda r, c, m: None, record=self._record,
            max_iters=20, mcp=None, audit=self.emitter,
            turn_timeout=0.1)
        result = await coordinator.run_turn(
            [{"role": "user", "content": "go"}])
        await self.emitter.flush()
        await self.emitter.stop()
        self.assertIn("time budget", result["text"])
        runs = self.store.list_runs()
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["status"], "stopped")
        # The stop is graceful: the run still carries a valid integrity hash.
        self.assertTrue(self.store.verify_run_integrity(runs[0]["id"])["ok"])

    async def test_no_budget_runs_to_completion(self):
        class FinishingLLM:
            def __init__(self):
                self.calls = 0

            async def stream(self, messages, tools=None, temperature=None, on_delta=None):
                self.calls += 1
                if self.calls == 1:
                    return {"role": "assistant", "content": "",
                            "tool_calls": [{"id": "c", "type": "function",
                                            "function": {"name": "run_python",
                                                         "arguments": {"code": "print(1)"}}}]}
                return {"role": "assistant", "content": "All done."}

        coordinator = self.Coordinator(
            FinishingLLM(), self.ctx, emit=_noop_emit,
            persist=lambda r, c, m: None, record=self._record,
            max_iters=4, mcp=None, audit=self.emitter)
        result = await coordinator.run_turn([{"role": "user", "content": "go"}])
        self.assertEqual(result["text"], "All done.")


class TestKernelRestartSurfacing(unittest.IsolatedAsyncioTestCase):
    """Item 10: kernel restarts are surfaced into the run's env snapshot."""

    def setUp(self):
        from backend.agents.coordinator import Coordinator
        from backend.agents.tools import ToolContext
        from backend.artifacts.store import ArtifactStore
        from backend.audit import make_audit
        from backend.permissions import PermissionManager
        self.tmp = Path(tempfile.mkdtemp())
        self.store = ProjectStore(self.tmp)
        self.artifacts = ArtifactStore(self.tmp)
        self.audit_store, self.emitter = make_audit(self.tmp)
        self.emitter.start()
        self.Coordinator = Coordinator

        class K:
            restarts = 2  # this kernel has died + restarted twice this session

        class Kernels:
            python = K()
            r = K()

            async def get_env(self):
                return {"python": "3.12"}

        class FakeLLM:
            async def stream(self, messages, tools=None, temperature=None, on_delta=None):
                return {"role": "assistant", "content": "ok"}

        self.ctx = ToolContext(
            kernels=Kernels(), artifacts=self.artifacts, store=self.store,
            permissions=None, audit=self.emitter, message_id="7")
        self.ctx.permissions = PermissionManager(self.store)
        self.llm = FakeLLM()

    def _record(self, r: dict):
        if r.get("id"):
            return self.store.finish_run(
                rid=int(r["id"]), reply=r.get("reply", ""),
                status=r.get("status", "done"), finished_at=r.get("finished_at"),
                tool_sequence=r.get("tool_sequence"),
                artifact_ids=r.get("artifact_ids"), metrics=r.get("metrics"),
                config=r.get("config"), label=r.get("label"),
                code=r.get("code"), env=r.get("env"),
                error=r.get("error") or None, review=r.get("review"))
        return self.store.add_run(prompt=r.get("prompt", ""),
                                  reply=r.get("reply", ""),
                                  status=r.get("status", "done"),
                                  started_at=r.get("started_at", 0.0),
                                  finished_at=r.get("finished_at", 0.0))

    async def test_restart_count_recorded_on_run(self):
        coordinator = self.Coordinator(
            self.llm, self.ctx, emit=_noop_emit,
            persist=lambda r, c, m: None, record=self._record,
            max_iters=2, mcp=None, audit=self.emitter)
        await coordinator.run_turn([{"role": "user", "content": "go"}])
        runs = self.store.list_runs()
        self.assertEqual(len(runs), 1)
        env = runs[0].get("env") or {}
        self.assertEqual(env.get("_kernel_restarts"), {"python": 2, "r": 2})


class TestVerifyEndpoints(unittest.IsolatedAsyncioTestCase):
    """Phase 5: the /verify REST endpoints work against a real runtime."""

    async def asyncSetUp(self):
        import backend.project_runtime as pr
        from backend.state import runtimes
        self.tmp = Path(tempfile.mkdtemp())
        self._orig = pr.PROJECTS_DIR
        pr.PROJECTS_DIR = self.tmp
        self.rt = pr.ProjectRuntime("verifyproj")
        runtimes["verifyproj"] = self.rt

    async def asyncTearDown(self):
        import backend.project_runtime as pr
        from backend.state import runtimes
        runtimes.pop("verifyproj", None)
        pr.PROJECTS_DIR = self._orig
        await self.rt.stop()

    async def test_message_verify_endpoint(self):
        from backend.routers import runs
        self.rt.store.add_message("user", "hi")
        self.rt.store.add_message("assistant", "yo")
        res = await runs.project_message_chain_verify("verifyproj")
        self.assertTrue(res["chain"]["ok"])
        self.assertEqual(res["chain"]["verified"], 2)

    async def test_artifact_verify_endpoints(self):
        from backend.artifacts.store import Artifact
        from backend.routers import artifacts
        art = Artifact(kind="text", name="r", data_type="text")
        self.rt.artifacts.add_artifact(art, data=b"data", data_type="text")
        summary = await artifacts.artifacts_verify("verifyproj")
        self.assertTrue(summary["chain"]["ok"])
        self.assertEqual(summary["chain"]["verified"], 1)
        single = await artifacts.artifact_verify("verifyproj", art.id)
        self.assertTrue(single["ok"])


class TestCompactionAudit(unittest.IsolatedAsyncioTestCase):
    """Phase 5: compaction is itself audited (traceable summarization)."""

    async def asyncSetUp(self):
        import backend.project_runtime as pr
        from backend.state import runtimes
        self.tmp = Path(tempfile.mkdtemp())
        self._orig = pr.PROJECTS_DIR
        pr.PROJECTS_DIR = self.tmp
        self.rt = pr.ProjectRuntime("compactproj")
        runtimes["compactproj"] = self.rt

        class FakeLLM:
            async def complete(self, messages, temperature=None, tools=None):
                return {"content": "compacted summary"}

        self.rt.llm = FakeLLM()
        self.rt.COMPACTION_LIMIT = 5
        self.rt.COMPACTION_KEEP = 2

    async def asyncTearDown(self):
        import backend.project_runtime as pr
        from backend.state import runtimes
        runtimes.pop("compactproj", None)
        pr.PROJECTS_DIR = self._orig
        await self.rt.stop()

    async def test_compaction_emits_audit_event(self):
        for i in range(10):
            self.rt.store.add_message("user" if i % 2 == 0 else "assistant",
                                      f"message {i}")
        await self.rt.maybe_compact()
        await self.rt.audit_emitter.flush()
        events = self.rt.audit_store.query()
        comp = [e for e in events if e["method"] == "compaction"]
        self.assertEqual(len(comp), 1)
        payload = comp[0].get("result_summary") or {}
        self.assertEqual(payload.get("folded"), 8)
        self.assertEqual(payload.get("cutoff"),
                         int(self.rt.store.get_setting("context_cutoff", "0")))
        # The summary + cutoff were actually persisted.
        self.assertTrue(self.rt.store.get_setting("context_summary", ""))

    async def test_compact_guard_prevents_concurrent(self):
        """Item 18: an in-flight compaction short-circuits a second call."""
        for i in range(10):
            self.rt.store.add_message("user" if i % 2 == 0 else "assistant",
                                      f"message {i}")
        self.rt._compacting = True  # simulate a compaction already in flight
        await self.rt.maybe_compact()
        # The guard returned before writing anything.
        self.assertEqual(self.rt.store.get_setting("context_summary", ""), "")
        self.assertEqual(self.rt.store.get_setting("context_cutoff", "0"), "0")
        self.rt._compacting = False


class TestPlanAuditEvents(unittest.IsolatedAsyncioTestCase):
    """Item 16: plan executions are visible in the audit trail."""

    async def test_audit_plan_emits_lifecycle_events(self):
        from backend.audit import make_audit
        from backend.routers.experiment_planner import _audit_plan
        tmp = Path(tempfile.mkdtemp())
        audit_store, emitter = make_audit(tmp)
        emitter.start()
        rt = SimpleNamespace(name="paudit", audit_emitter=emitter)
        plan = {"id": "p1", "experiment_id": "peer", "dataset": "upi.csv",
                "seed": 1, "steps": ["a", "b"]}
        await _audit_plan(rt, "plan_started", plan, run_id=42)
        await _audit_plan(rt, "plan_completed", plan, run_id=42,
                          metrics={"acc": 0.5})
        await _audit_plan(rt, "plan_failed", plan, run_id=43,
                          error="boom")
        await emitter.flush()
        await emitter.stop()
        evs = audit_store.query()
        methods = {e["method"] for e in evs}
        self.assertIn("plan_started", methods)
        self.assertIn("plan_completed", methods)
        self.assertIn("plan_failed", methods)
        started = next(e for e in evs if e["method"] == "plan_started")
        self.assertEqual(started["run_id"], "42")
        self.assertEqual((started["result_summary"] or {}).get("plan_id"), "p1")
        done = next(e for e in evs if e["method"] == "plan_completed")
        self.assertEqual((done["result_summary"] or {}).get("metrics"),
                         {"acc": 0.5})
        failed = next(e for e in evs if e["method"] == "plan_failed")
        self.assertEqual(failed["severity"], "critical")
        self.assertIn("boom", (failed["result_summary"] or {}).get("error", ""))


class TestCampaignKernelIsolation(unittest.IsolatedAsyncioTestCase):
    """Item 12: background campaigns run on their own kernel, not the chat
    kernel, so the two can't clobber each other's state."""

    async def asyncSetUp(self):
        import backend.project_runtime as pr
        from backend.state import runtimes
        self.tmp = Path(tempfile.mkdtemp())
        self._orig = pr.PROJECTS_DIR
        pr.PROJECTS_DIR = self.tmp
        self.rt = pr.ProjectRuntime("isocamp")
        runtimes["isocamp"] = self.rt
        self.cid = self.rt.store.create_campaign("c", "q", "acc", True)

    async def asyncTearDown(self):
        import backend.project_runtime as pr
        from backend.state import runtimes
        runtimes.pop("isocamp", None)
        pr.PROJECTS_DIR = self._orig
        await self.rt.stop()

    async def test_campaign_uses_dedicated_kernel(self):
        import backend.campaign as campaign_mod
        captured = {}

        async def fake_run_campaign(rt, coord, build, cid, **kw):
            captured["dedicated"] = coord.ctx.kernels
            captured["main"] = rt.kernels
            captured["dedicated_python"] = coord.ctx.kernels.python
            return {"campaign": None, "steps": [], "report": "",
                    "stopped_reason": "test"}

        orig = campaign_mod.run_campaign
        campaign_mod.run_campaign = fake_run_campaign
        try:
            ok, msg = self.rt.start_campaign(self.cid)
            self.assertTrue(ok, msg)
            await self.rt._campaign_task
        finally:
            campaign_mod.run_campaign = orig

        self.assertIsNotNone(captured.get("dedicated"))
        # The campaign's kernel manager is distinct from the chat kernel manager.
        self.assertIsNot(captured["dedicated"], captured["main"])
        self.assertIsNot(captured["dedicated_python"], self.rt.kernels.python)
        # The dedicated kernel subprocess was stopped after the task finished.
        self.assertIsNone(captured["dedicated_python"]._proc)


class TestProjectStatus(unittest.IsolatedAsyncioTestCase):
    """Item 13: /status exposes in-flight work + kernel health + audit stats."""

    async def asyncSetUp(self):
        import backend.project_runtime as pr
        from backend.state import runtimes
        self.tmp = Path(tempfile.mkdtemp())
        self._orig = pr.PROJECTS_DIR
        pr.PROJECTS_DIR = self.tmp
        self.rt = pr.ProjectRuntime("statusproj")
        runtimes["statusproj"] = self.rt

    async def asyncTearDown(self):
        import backend.project_runtime as pr
        from backend.state import runtimes
        runtimes.pop("statusproj", None)
        pr.PROJECTS_DIR = self._orig
        await self.rt.stop()

    async def test_status_reports_inflight_and_kernels(self):
        from backend.routers import projects
        res = await projects.project_status("statusproj")
        st = res["status"]
        self.assertEqual(st["name"], "statusproj")
        self.assertFalse(st["campaign_running"])
        self.assertFalse(st["eval_running"])
        self.assertEqual(st["plans_running"], [])
        self.assertIn("python", st["kernels"])
        self.assertIn("restarts", st["kernels"]["python"])
        self.assertIn("workflow", st)
        self.assertIn("audit", st)
        # Launch a plan task and see it reported.
        self.rt.launch_plan("planA", asyncio.sleep(1.0))
        await asyncio.sleep(0)  # let the task start
        st = (await projects.project_status("statusproj"))["status"]
        self.assertEqual(st["plans_running"], ["planA"])
        self.rt._plan_tasks["planA"].cancel()
        await self.rt.drain_plans()


class TestDurableCampaignResume(unittest.IsolatedAsyncioTestCase):
    """Item 15: campaign resume is derived from persisted step statuses, not the
    volatile workflow snapshot."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = ProjectStore(self.tmp)

    def test_resume_step_from_step_statuses(self):
        cid = self.store.create_campaign("c", "q", "acc", True)
        for i in range(1, 4):
            self.store.add_campaign_step(cid, i, f"step {i}", "experiment",
                                         "", "")
        self.assertEqual(self.store.campaign_resume_step(cid), 1)
        # Mark step 1 done -> resume points at step 2.
        steps = self.store.list_campaign_steps(cid)
        self.store.update_campaign_step(steps[0]["id"], status="done",
                                        experiment_id=1, best_run_id=5)
        self.assertEqual(self.store.campaign_resume_step(cid), 2)
        # All done -> None.
        for s in steps:
            self.store.update_campaign_step(s["id"], status="done",
                                            experiment_id=1, best_run_id=5)
        self.assertIsNone(self.store.campaign_resume_step(cid))

    def test_resume_step_persisted_and_migrated(self):
        cid = self.store.create_campaign("c", "q", "acc", True)
        self.store.update_campaign(cid, resume_step=3)
        c = self.store.get_campaign(cid)
        self.assertEqual(c["resume_step"], 3)


class TestSweepPoolCap(unittest.IsolatedAsyncioTestCase):
    """Item 14: a huge grid caps the parallel pool and runs the rest sequentially."""

    def setUp(self):
        from backend.agents.tools import ToolContext
        from backend.artifacts.store import ArtifactStore
        from backend.permissions import PermissionManager
        from tests.test_round3 import PoolKernels
        self.tmp = Path(tempfile.mkdtemp())
        self.store = ProjectStore(self.tmp)
        self.eid = self.store.create_experiment("sweep", "", "acc", 0.9, True)
        self.ctx = ToolContext(kernels=PoolKernels(),
                               artifacts=ArtifactStore(self.tmp),
                               store=self.store,
                               permissions=PermissionManager(self.store))
        self.ctx.experiment_id = str(self.eid)

    async def test_large_grid_is_capped(self):
        from backend.agents.tools import MAX_SWEEP_KERNELS, _run_sweep
        configs = [{"eps": i} for i in range(20)]
        out = await _run_sweep(self.ctx, "report_metric('acc', 0.8)",
                               configs, label_prefix="eps")
        self.assertIn("parallel 8 + sequential 12", out)
        runs = [r for r in self.store.experiment_runs(self.eid)
                if r["kind"] == "sweep"]
        self.assertEqual(len(runs), 20)
        # The pool never exceeded the cap (PoolKernels.pool records _n).
        self.assertLessEqual(self.ctx.kernels._n, MAX_SWEEP_KERNELS)


class TestSeparateStopFlags(unittest.IsolatedAsyncioTestCase):
    """Item 19: campaign and eval have independent stop signals, so stopping
    one never stops the other when both run concurrently."""

    def setUp(self):
        import backend.project_runtime as pr
        self.orig = pr.PROJECTS_DIR
        self.tmp = Path(tempfile.mkdtemp())
        pr.PROJECTS_DIR = self.tmp
        self.rt = pr.ProjectRuntime("stopflag")
        self._pr = pr

    def tearDown(self):
        self._pr.PROJECTS_DIR = self.orig
        # Fake tasks so stop() doesn't await anything real.
        self.rt._campaign_task = None
        self.rt._eval_task = None

    async def test_stop_campaign_does_not_stop_eval(self):
        # Simulate both running concurrently.
        self.rt._campaign_task = asyncio.create_task(asyncio.sleep(30))
        self.rt._eval_task = asyncio.create_task(asyncio.sleep(30))
        self.rt.campaign_stop = False
        self.rt.eval_stop = False
        self.assertTrue(self.rt.stop_campaign())
        # The eval's stop flag is untouched.
        self.assertFalse(self.rt.eval_stop)
        self.assertTrue(self.rt.campaign_stop)
        # Stopping the eval must not clear the campaign's stop signal either.
        self.assertTrue(self.rt.stop_eval())
        self.assertTrue(self.rt.eval_stop)
        self.assertTrue(self.rt.campaign_stop)
        self.rt._eval_task.cancel()
        self.rt._campaign_task.cancel()
        try:
            await self.rt._eval_task
            await self.rt._campaign_task
        except asyncio.CancelledError:
            pass

    async def test_stop_sets_both_flags(self):
        self.rt._campaign_task = asyncio.create_task(asyncio.sleep(30))
        self.rt._eval_task = asyncio.create_task(asyncio.sleep(30))
        await self.rt.stop(drain_timeout=0.05)
        self.assertTrue(self.rt.campaign_stop)
        self.assertTrue(self.rt.eval_stop)


class TestEvalAuditEvents(unittest.IsolatedAsyncioTestCase):
    """Item 20: evals emit eval-level audit events."""

    async def test_audit_eval_emits_events(self):
        from backend.audit import make_audit
        from backend.eval import _audit_eval
        tmp = Path(tempfile.mkdtemp())
        audit_store, emitter = make_audit(tmp)
        emitter.start()
        rt = SimpleNamespace(name="paudit", audit_emitter=emitter)
        ev = {"id": 7, "name": "bench", "goal_metric": "acc"}
        await _audit_eval(rt, "eval_started", ev, models=["m1", "m2"])
        await _audit_eval(rt, "eval_completed", ev, models=["m1", "m2"])
        await _audit_eval(rt, "eval_failed", ev, error="boom")
        await emitter.flush()
        await emitter.stop()
        evs = audit_store.query()
        methods = {e["method"] for e in evs}
        self.assertIn("eval_started", methods)
        self.assertIn("eval_completed", methods)
        self.assertIn("eval_failed", methods)
        started = next(e for e in evs if e["method"] == "eval_started")
        self.assertEqual((started["result_summary"] or {}).get("eval_id"), 7)
        failed = next(e for e in evs if e["method"] == "eval_failed")
        self.assertEqual(failed["severity"], "critical")


class TestStatusResumeEnrichment(unittest.IsolatedAsyncioTestCase):
    """Item 21: /status exposes durable resume points + improve_latest."""

    async def asyncSetUp(self):
        import backend.project_runtime as pr
        from backend.state import runtimes
        self.tmp = Path(tempfile.mkdtemp())
        self._orig = pr.PROJECTS_DIR
        pr.PROJECTS_DIR = self.tmp
        self.rt = pr.ProjectRuntime("statusproj2")
        runtimes["statusproj2"] = self.rt

    async def asyncTearDown(self):
        import backend.project_runtime as pr
        from backend.state import runtimes
        runtimes.pop("statusproj2", None)
        pr.PROJECTS_DIR = self._orig
        await self.rt.stop()

    async def test_status_has_resume_and_improve(self):
        from backend.routers import projects
        self.rt.store.set_setting("improve_latest", json.dumps(
            {"kind": "improve", "experiment_id": 3, "iterations": 4,
             "prompt": "p"}))
        cid = self.rt.store.create_campaign("c", "q", "acc", True)
        self.rt.store.add_campaign_step(cid, 1, "s1", "experiment", "", "")
        self.rt.store.update_campaign(cid, resume_step=1)
        self.rt._campaign_cid = cid
        st = (await projects.project_status("statusproj2"))["status"]
        self.assertEqual(st["campaign_resume_step"], 1)
        self.assertEqual(st["improve_latest"]["experiment_id"], 3)


class TestRuntimeEviction(unittest.IsolatedAsyncioTestCase):
    """Item 22: idle runtimes can be evicted; busy ones can't."""

    async def asyncSetUp(self):
        import backend.project_runtime as pr
        self.tmp = Path(tempfile.mkdtemp())
        self._orig = pr.PROJECTS_DIR
        pr.PROJECTS_DIR = self.tmp
        self.rt = pr.ProjectRuntime("evictproj")

    async def asyncTearDown(self):
        import backend.project_runtime as pr
        pr.PROJECTS_DIR = self._orig
        await self.rt.evict()

    async def test_idle_is_not_busy(self):
        self.assertFalse(self.rt.is_busy())

    async def test_busy_with_subscriber(self):
        self.rt.workflow.subscribe(lambda t, p: None)
        self.assertTrue(self.rt.is_busy())

    async def test_busy_with_plan_task(self):
        self.rt._plan_tasks["p"] = asyncio.create_task(asyncio.sleep(30))
        self.assertTrue(self.rt.is_busy())
        self.rt._plan_tasks["p"].cancel()
        await self.rt.drain_plans()
        self.assertFalse(self.rt.is_busy())

    async def test_evict_stops_kernels(self):
        await self.rt.evict()
        self.assertIsNone(self.rt.kernels.python._proc)


class TestKernelAuditContext(unittest.IsolatedAsyncioTestCase):
    """Item 24: kernel audit events carry the project's session id."""

    async def test_kernel_event_has_session_id(self):
        from backend.audit import make_audit
        from backend.project_runtime import ProjectRuntime
        tmp = Path(tempfile.mkdtemp())
        audit_store, emitter = make_audit(tmp)
        emitter.start()

        class _FakeRT:
            name = "kproj"

            def __init__(self, emitter):
                self.audit_emitter = emitter

            async def _audit_emit(self, ev):
                await self.audit_emitter.emit(ev)

        fake = _FakeRT(emitter)
        ProjectRuntime._on_kernel_event(fake, "busy", {"code": "x"})
        await asyncio.sleep(0.05)
        await emitter.flush()
        await emitter.stop()
        evs = audit_store.query()
        self.assertTrue(any(e["session_id"] == "kproj" for e in evs))


class TestCascadeCleanup(unittest.TestCase):
    """R3: deleting a plan removes its mirror record; orphan artifact files are
    swept."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = ProjectStore(self.tmp)

    def test_delete_plan_removes_mirror(self):
        self.store.upsert_plan({"id": "p1", "experiment_id": "x", "name": "X",
                                "status": "DONE", "created_at": 1.0,
                                "updated_at": 2.0})
        self.assertIsNotNone(self.store.get_plan_record("p1"))
        self.assertTrue(self.store.delete_plan_record("p1"))
        self.assertIsNone(self.store.get_plan_record("p1"))
        # Deleting again is a no-op.
        self.assertFalse(self.store.delete_plan_record("p1"))

    def test_sweep_orphan_artifacts(self):
        from backend.artifacts.store import Artifact, ArtifactStore
        arts = ArtifactStore(self.tmp)
        art = Artifact(kind="text", name="keep", data_type="text")
        arts.add_artifact(art, data=b"data", data_type="text")
        # Drop the row behind the file, simulating a crashed write.
        arts._conn.execute("DELETE FROM artifacts WHERE id=?", (art.id,))
        arts._conn.commit()
        # Also leave a stray file with a name that never had a row.
        (arts.artifacts_dir / "orphan.png").write_bytes(b"x")
        removed = arts.sweep_orphans()
        self.assertEqual(removed, 2)
        self.assertFalse((arts.artifacts_dir / "orphan.png").exists())
        self.assertFalse(Path(art.data_path).exists())


if __name__ == "__main__":
    unittest.main()
