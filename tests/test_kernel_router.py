"""Kernel status router: per-project live status + kernel events in the audit trail."""

from __future__ import annotations

import asyncio
import os
import unittest
import uuid

from fastapi.testclient import TestClient


class TestKernelStatusRouter(unittest.TestCase):
    def test_status_snapshot_and_audit_wiring(self):
        os.environ["FOX_WORKBENCH_DIR"] = f"/tmp/fox-test-{uuid.uuid4().hex[:8]}"
        from backend.main import app
        from backend.state import get_runtime

        name = f"proj-{uuid.uuid4().hex[:8]}"
        with TestClient(app) as client:
            client.post("/api/projects", json={"name": name})
            rt = get_runtime(name)

            r = client.get(f"/api/projects/{name}/kernel/status")
            assert r.status_code == 200
            body = r.json()
            assert body["state"] in ("idle", "busy")
            assert "pid" in body
            assert "uptime" in body
            assert body["remote"] is False

            r = client.post(f"/api/projects/{name}/kernel/execute",
                            json={"code": "x = 1"})
            assert r.status_code == 200
            assert r.json()["ok"]

            async def seed():
                # Busy/idle events should reach the audit trail via the
                # ProjectRuntime subscriber.
                await rt.audit_emitter.flush()

            asyncio.run(seed())
            r = client.get(f"/api/projects/{name}/audit/timeline")
            events = r.json()["events"]
            kernel_events = [e for e in events if e.get("source") == "kernel"]
            assert kernel_events, "no kernel events landed in the audit trail"
            tools = {e.get("tool_name") for e in kernel_events}
            assert "kernel.busy" in tools and "kernel.idle" in tools


if __name__ == "__main__":
    unittest.main()
