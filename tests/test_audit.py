"""Audit trail system: redaction, hash-chained store, deviations, policy,
middleware, and the workbench integration (router + coordinator wiring)."""

from __future__ import annotations

import asyncio
import json
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from audit import (AuditEmitter, AuditEvent, DeviationDetector, LocalAuditStore,
                   PermissionTracker, PolicyEngine, audit_tool, redact,
                   redact_string, risk_tier_for, severity_for_tier)
from audit.middleware import AuditedSession
from audit.models import PolicyRule, ulid_now

# --------------------------------------------------------------- redaction ---


def test_redaction_masks_keys_and_tokens():
    obj = {
        "command": "curl -H 'Authorization: Bearer abc123def' --token=secret1 https://host",
        "api_key": "sk-0123456789abcdef",
        "nested": {"password": "hunter2", "safe": "keep-me", "list": ["token=x", "fine"]},
    }
    out = redact(obj)
    assert out["api_key"] == "***REDACTED***"
    assert out["nested"]["password"] == "***REDACTED***"
    assert out["nested"]["safe"] == "keep-me"
    assert "secret1" not in json.dumps(out)
    assert "sk-0123456789abcdef" not in json.dumps(out)


def test_redact_string_url_credentials():
    masked = redact_string("git clone https://user:supersecret@github.com/x/y.git")
    assert "supersecret" not in masked
    assert "user:***REDACTED***@" in masked


# ------------------------------------------------------------------- store ---


def _store(tmp_path: Path) -> LocalAuditStore:
    return LocalAuditStore(tmp_path / "audit")


def test_store_append_query_and_chain(tmp_path):
    store = _store(tmp_path)
    a = AuditEvent(agent_id="a1", tool_name="run_python", tags=["x"])
    b = AuditEvent(agent_id="a1", tool_name="run_shell",
                   network={"destination": "example.com", "method": "GET"},
                   policy_decision={"outcome": "OVERRIDE", "risk_tier": "critical"})
    store.append(a)
    store.append(b)
    assert store.count() == 2
    assert store.get(a.event_id).event_id == a.event_id
    got = store.query(agent_id="a1")
    assert len(got) == 2
    assert got[0]["event_hash"] == b.event_hash
    # chained: a.event_hash == b.prev_hash
    assert b.prev_hash == a.event_hash
    result = store.verify_chain()
    assert result["ok"] and result["events"] == 2
    summ = store.summary()
    assert summ["total"] == 2
    assert summ["overrides"] == 1
    assert summ["network"] == 1


def test_store_tamper_detection(tmp_path):
    store = _store(tmp_path)
    store.append(AuditEvent(agent_id="a", tool_name="t1"))
    store.append(AuditEvent(agent_id="a", tool_name="t2"))
    store.append(AuditEvent(agent_id="a", tool_name="t3"))
    path = store._jsonl_path
    lines = path.read_text().splitlines()
    # tamper: rewrite the first event's tool_name
    d = json.loads(lines[0])
    d["tool_name"] = "tampered"
    path.write_text("\n".join([json.dumps(d)] + lines[1:]) + "\n")
    result = store.verify_chain()
    assert not result["ok"]
    assert result["broken_at"] == 0


def test_store_deviation_records(tmp_path):
    store = _store(tmp_path)
    from audit import DeviationRecord

    store.record_deviation(DeviationRecord(agent_id="a", rule="novel_tool",
                                           severity="warning",
                                           explanation="x", event_ids=["e1"]))
    devs = store.list_deviations()
    assert len(devs) == 1 and devs[0]["rule"] == "novel_tool"
    store.mark_deviation_reviewed(devs[0]["deviation_id"], reviewed=True,
                                  reviewed_by="tester")
    devs = store.list_deviations(reviewed=True)
    assert devs[0]["reviewed"] and devs[0]["reviewed_by"] == "tester"


def test_agent_history_and_tool_usage(tmp_path):
    store = _store(tmp_path)
    for i in range(5):
        store.append(AuditEvent(agent_id="agent-x", tool_name="run_python"))
    assert len(store.get_agent_history("agent-x")) == 5
    usage = store.tool_usage("agent-x")
    assert usage[0]["tool"] == "run_python" and usage[0]["count"] == 5


# ------------------------------------------------------------ deviations ----


def _seed_baseline(store: LocalAuditStore, agent: str, n: int = 30):
    prev = None
    for i in range(n):
        e = AuditEvent(agent_id=agent, tool_name="run_python", tags=["base"],
                       result_summary=AuditEvent.result_summary_for(
                           status="ok", data_classes=["csv"], size=10),
                       filesystem={"path": "/workbench/data", "operation": "read"},
                       timestamp=datetime.now(timezone.utc)
                       - timedelta(seconds=(n - i) * 5))
        store.append(e)
        prev = e


def test_deviation_detector_flags_novel_tool(tmp_path):
    store = _store(tmp_path)
    _seed_baseline(store, "a1")
    det = DeviationDetector()
    det.compute_baseline(store)
    # novel tool call now
    ev = AuditEvent(agent_id="a1", tool_name="run_shell",
                    arguments_redacted={"command": "rm -rf /tmp"},
                    timestamp=datetime.now(timezone.utc))
    store.append(ev)
    findings = det.check_event(store, store.get(ev.event_id).model_dump(mode="json"))
    rules = {f.rule for f in findings}
    assert "novel_tool" in rules


def test_deviation_detector_flags_novel_sequence(tmp_path):
    store = _store(tmp_path)
    for i in range(30):
        store.append(AuditEvent(
            agent_id="seq", tool_name="run_python" if i % 2 else "run_notebook",
            timestamp=datetime.now(timezone.utc) - timedelta(seconds=(30 - i))))
    det = DeviationDetector()
    det.compute_baseline(store)
    # learn a new previous-tool first
    e1 = AuditEvent(agent_id="seq", tool_name="save_artifact",
                    timestamp=datetime.now(timezone.utc))
    store.append(e1)
    det.check_event(store, store.get(e1.event_id).model_dump(mode="json"))
    # then the novel bigram save_artifact -> run_shell
    e2 = AuditEvent(agent_id="seq", tool_name="run_shell",
                    timestamp=datetime.now(timezone.utc))
    store.append(e2)
    findings = det.check_event(store, store.get(e2.event_id).model_dump(mode="json"))
    assert any(f.rule == "novel_sequence" for f in findings)


def test_deviation_detector_flags_unseen_path(tmp_path):
    store = _store(tmp_path)
    _seed_baseline(store, "a2")
    det = DeviationDetector()
    det.compute_baseline(store)
    ev = AuditEvent(agent_id="a2", tool_name="run_python",
                    filesystem={"path": "/etc/passwd", "operation": "read"},
                    timestamp=datetime.now(timezone.utc))
    store.append(ev)
    findings = det.check_event(store, store.get(ev.event_id).model_dump(mode="json"))
    assert any(f.rule == "unseen_path" for f in findings)


def test_deviation_detector_requires_history(tmp_path):
    store = _store(tmp_path)
    store.append(AuditEvent(agent_id="a3", tool_name="run_python"))
    det = DeviationDetector()
    findings = det.check_event(store, store.query()[0])
    assert findings == []


# ------------------------------------------------------------------- policy ---


def test_risk_tiers():
    assert risk_tier_for("run_python") == "low"
    assert risk_tier_for("run_shell") == "critical"
    assert risk_tier_for("privacy__apply_laplace_dp") == "high"
    assert risk_tier_for("privacy__reidentification_scenario") == "high"
    assert risk_tier_for("github__commit") == "high"
    assert severity_for_tier("critical") == "critical"


def test_policy_engine_and_permission_tracker(tmp_path):
    class FakeRuleStore:
        def __init__(self): self.rules = {}
        def get_rule(self, key, pattern):
            r = self.rules.get((key, pattern))
            return r.model_dump(mode="json") if r else None

    f = FakeRuleStore()
    f.rules[("mcp_tool", "github__push")] = PolicyRule(
        key="mcp_tool", pattern="github__push", decision="DENY", reason="no push")
    engine = PolicyEngine(f)
    decision = engine.decide("mcp_tool", "github__push")
    assert decision["outcome"] == "DENY" and decision["reason"] == "no push"

    tracker = PermissionTracker()
    tracker.observe("mcp_tool", "github__push", "OVERRIDE", "high")
    tracker.observe("mcp_tool", "github__push", "OVERRIDE", "high")
    perms = tracker.list()
    assert perms[0]["overrides"] == 2 and perms[0]["usage_count"] == 2


# ------------------------------------------------------------- middleware ----


def test_audit_tool_decorator(tmp_path):
    async def main():
        store = _store(tmp_path)
        emitter = AuditEmitter(store)
        emitter.start()

        @audit_tool(emitter, agent_id="researcher", tool_name="lookup_pdb")
        async def lookup_pdb(pdb_id: str, token: str):
            await asyncio.sleep(0.001)
            return f"PDB {pdb_id}: 123 atoms"

        result = await lookup_pdb("1ABC", token="super-secret-token-12345")
        await emitter.flush()
        await emitter.stop()
        events = store.query(agent_id="researcher")
        assert result.startswith("PDB")
        assert len(events) == 1
        assert events[0]["tool_name"] == "lookup_pdb"
        assert "super-secret-token-12345" not in json.dumps(events[0])
        assert events[0]["duration_ms"] is not None
        assert events[0]["result_summary"]["status"] == "ok"

    asyncio.run(main())


def test_audited_session_wraps_calls(tmp_path):
    class FakeTool:
        name = "do_thing"
        description = "fake"
        input_schema = {"type": "object", "properties": {}}
        annotations = None

    class FakeSession:
        def __init__(self, tools): self.tools = tools
        async def initialize(self): pass
        async def list_tools(self): return type("R", (), {"tools": self.tools})()
        async def call_tool(self, name, arguments=None):
            text = type("T", (), {"type": "text", "text": "ok"})()
            return type("R", (), {"isError": False, "content": [text]})()

    async def main():
        store = _store(tmp_path)
        emitter = AuditEmitter(store)
        emitter.start()
        sess = AuditedSession(FakeSession([FakeTool()]), emitter,
                              agent_id="mcp-agent", mcp_server="science")
        res = await sess.call_tool("do_thing", {"a": 1, "api_key": "sk-secret-999"})
        await emitter.flush()
        await emitter.stop()
        events = store.query(agent_id="mcp-agent")
        assert res is not None
        assert len(events) == 1
        assert events[0]["source"] == "mcp_proxy"
        assert events[0]["mcp_server"] == "science"
        assert "sk-secret-999" not in json.dumps(events[0])

    asyncio.run(main())


# ------------------------------------------------------------ workbench API ---


def test_backend_emit_tool_audit(tmp_path):
    from backend.audit import make_audit, public_event

    store, emitter = make_audit(tmp_path)

    async def main():
        from backend.audit import emit_policy_event, emit_tool_audit

        await emit_tool_audit(
            emitter, agent_id="Fox", session_id="proj", trace_id="t1",
            tool_name="run_shell", method="run_shell",
            args={"command": "curl -H 'Authorization: Bearer xyz' https://api.x.com/data"},
            result="[error] 403", ok=False, duration_ms=123.4,
            source="coordinator")
        await emit_policy_event(
            emitter, agent_id="Fox", session_id="proj", trace_id="t1",
            kind="run_shell", command="curl https://x", decision="allow",
            temporary=True)
        await emitter.flush()
        await emitter.stop()

    asyncio.run(main())
    events = store.query()
    assert len(events) == 2
    shell = next(e for e in events if e["source"] == "coordinator")
    assert shell["network"]["destination"] == "api.x.com"
    assert shell["result_summary"]["status"] == "error"
    assert shell["severity"] == "critical"
    # secret redacted from stored args
    assert "Bearer xyz" not in json.dumps(shell)
    override = next(e for e in events if e["source"] == "approval")
    assert override["policy_decision"]["outcome"] == "OVERRIDE"


def test_audit_router_endpoints(tmp_path):
    import os
    import uuid

    os.environ["FOX_WORKBENCH_DIR"] = str(tmp_path / "wb")
    from fastapi.testclient import TestClient

    from backend.main import app
    from backend.state import get_runtime

    name = f"proj-{uuid.uuid4().hex[:8]}"
    with TestClient(app) as client:
        client.post("/api/projects", json={"name": name})
        rt = get_runtime(name)

        async def seed():
            from backend.audit import emit_tool_audit

            await emit_tool_audit(
                rt.audit_emitter, agent_id="Fox", session_id=name,
                trace_id="r1", tool_name="run_python", method="run_python",
                args={"code": "df = pd.read_csv('clinical.csv'); print(df.shape)"},
                result="(1000, 12)", ok=True, duration_ms=50.0,
                source="coordinator")
            await rt.audit_emitter.flush()

        asyncio.run(seed())

        r = client.get(f"/api/projects/{name}/audit/summary")
        assert r.json()["summary"]["total"] >= 1
        r = client.get(f"/api/projects/{name}/audit/timeline")
        assert len(r.json()["events"]) >= 1
        r = client.get(f"/api/projects/{name}/audit/agents")
        assert any(a["agent_id"] == "Fox" for a in r.json()["agents"])
        r = client.get(f"/api/projects/{name}/audit/events?agent=Fox")
        assert len(r.json()["events"]) >= 1
        eid = r.json()["events"][0]["event_id"]
        r = client.get(f"/api/projects/{name}/audit/event/{eid}")
        assert r.json()["event"]["tool_name"] == "run_python"
        r = client.get(f"/api/projects/{name}/audit/verify")
        assert r.json()["chain"]["ok"]
        r = client.post(f"/api/projects/{name}/audit/scan", json={})
        assert "recorded" in r.json()
        r = client.get(f"/api/projects/{name}/audit/deviations")
        assert isinstance(r.json()["deviations"], list)
        r = client.get(f"/api/projects/{name}/audit/export?fmt=csv")
        assert r.headers["content-type"].startswith("text/csv")


def test_deviation_scan_is_incremental(tmp_path):
    """The scanner seeds baselines on the first scan, then flags genuinely new
    tools/paths seen after the previous scan."""
    from backend.audit import ProjectDeviationScanner

    store = _store(tmp_path)
    from datetime import datetime, timedelta, timezone as tz

    now = time.time()
    for i in range(4):
        store.append(AuditEvent(
            agent_id="scan-agent", tool_name="run_python",
            result_summary=AuditEvent.result_summary_for(status="ok",
                                                         data_classes=["csv"]),
            timestamp=datetime.fromtimestamp(now - (4 - i) * 10, tz=tz.utc)))
    scanner = ProjectDeviationScanner(store)
    assert scanner.scan() == 0  # seeds baseline, nothing to flag

    time.sleep(0.02)
    store.append(AuditEvent(
        agent_id="scan-agent", tool_name="run_shell",
        arguments_redacted={"command": "rm -rf /tmp"},
        timestamp=datetime.now(tz.utc)))
    assert scanner.scan() >= 1
    rules = {d["rule"] for d in store.list_deviations()}
    assert "novel_tool" in rules


def test_ulid_monotonic_and_sorted():
    import time

    ids = [ulid_now() for _ in range(50)]
    assert len(set(ids)) == 50
    # ULIDs sort by their timestamp prefix when generated in distinct moments.
    time.sleep(0.002)
    later = ulid_now()
    assert ids[-1] < later
    assert later > ids[0]


# ----------------------------------------------------- workbench coordinator ---


def test_coordinator_turn_emits_audit_events(tmp_path):
    """A full coordinator turn with audit wiring records tool events + turn
    events into the project's audit store."""

    async def run():
        from backend.agents.coordinator import Coordinator
        from backend.agents.tools import ToolContext
        from backend.artifacts.store import ArtifactStore
        from backend.audit import make_audit
        from backend.permissions import PermissionManager
        from backend.store import ProjectStore

        store = ProjectStore(tmp_path)
        artifacts = ArtifactStore(tmp_path)
        audit_store, emitter = make_audit(tmp_path / "audit")
        emitter.start()

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

        class FakeLLM:
            def __init__(self):
                self.calls = 0
                self.tool_calls = [{
                    "id": "call_1", "type": "function",
                    "function": {"name": "run_python",
                                 "arguments": {"code":
                                               "import pandas as pd; print('ok')"}},
                }]

            async def stream(self, messages, tools=None, temperature=None, on_delta=None):
                self.calls += 1
                if self.calls == 1:
                    return {"role": "assistant", "content": "",
                            "tool_calls": self.tool_calls}
                return {"role": "assistant", "content": "Done."}

        ctx = ToolContext(kernels=FakeKernels(), artifacts=artifacts,
                          store=store, permissions=PermissionManager(store),
                          audit=emitter, message_id="42")

        async def noop(t, p):
            pass

        coordinator = Coordinator(FakeLLM(), ctx, emit=noop,
                                  persist=lambda r, c, m: None,
                                  record=lambda r: 1, max_iters=4,
                                  mcp=None, audit=emitter)
        result = await coordinator.run_turn(
            [{"role": "user", "content": "run it"}])
        await emitter.flush()
        await emitter.stop()
        assert result["text"] == "Done."
        events = audit_store.query()
        tools = [e for e in events if e["tool_name"] == "run_python"]
        assert len(tools) == 1
        assert tools[0]["session_id"] == str(tmp_path.name)
        assert tools[0]["trace_id"] == "42"
        assert tools[0]["result_summary"]["status"] == "ok"
        # turn boundary events recorded too
        methods = {e["method"] for e in events}
        assert "turn_start" in methods and "turn_end" in methods
        assert audit_store.verify_chain()["ok"]

    asyncio.run(run())
