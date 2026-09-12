"""Kernel status router: per-project live status + kernel events in the audit trail."""

from __future__ import annotations

import asyncio
import os
import unittest
import uuid

from fastapi.testclient import TestClient


def _delete_test_project(app, name: str) -> None:
    """Best-effort cleanup so tests never leave projects in the workbench dir.

    backend.paths is frozen at import, so the FOX_WORKBENCH_DIR set below only
    affects late importers — the created project lands in the real volume and
    must be removed explicitly (stops kernels, drops the runtime, rmtree).
    """
    try:
        with TestClient(app) as client:
            client.delete(f"/api/projects/{name}")
    except Exception:  # noqa: BLE001 - cleanup must not fail the test
        pass


class TestKernelStatusRouter(unittest.TestCase):
    def test_status_snapshot_and_audit_wiring(self):
        os.environ["FOX_WORKBENCH_DIR"] = f"/tmp/fox-test-{uuid.uuid4().hex[:8]}"
        from backend.main import app
        from backend.state import get_runtime

        name = f"proj-{uuid.uuid4().hex[:8]}"
        self.addCleanup(_delete_test_project, app, name)
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
