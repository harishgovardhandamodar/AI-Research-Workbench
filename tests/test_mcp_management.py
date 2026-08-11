"""MCP management & orchestration tests: registry enable/disable, tool
catalog, the standalone permission-aware call_mcp_tool, and the REST endpoint."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend.mcp import MCPRegistry, call_mcp_tool


class _FakeConn:
    def __init__(self, tools, replies=None):
        self._tools = tools
        self._replies = replies or {}
        self.last_args = None

    async def list_tools(self):
        return self._tools

    async def call_tool(self, name, args):
        self.last_args = (name, args)
        if name in self._replies:
            return (self._replies[name], False, [])
        return (f"ran {name} {args}", False, [])

    async def close(self):
        pass


def _tool(name, ro, schema=None):
    return type("T", (), {"name": name, "description": "desc",
                          "annotations": type("A", (), {"read_only_hint": ro})(),
                          "input_schema": schema or {"type": "object",
                                                     "properties": {}}})()


def _registry(enabled=True, wr_reply=None):
    r = MCPRegistry([{"name": "s", "transport": "stdio", "enabled": enabled}])
    r._available = True
    r._conns["s"] = _FakeConn([_tool("ro", True), _tool("wr", False)],
                              replies={"wr": wr_reply} if wr_reply else None)
    return r


class _Perms:
    def __init__(self, grant):
        self.grant = grant
        self.records = []

    def check(self, kind, cmd):
        return self.grant

    def record(self, kind, cmd, val):
        self.records.append((cmd, val))


class _Broker:
    def __init__(self, approve=True):
        self.approve = approve
        self.asked = 0

    async def request(self, kind, cmd, reason):
        self.asked += 1
        return (self.approve, False)


class TestMCPRegistry(unittest.TestCase):
    def test_disabled_server_not_probed_or_exposed(self):
        r = _registry(enabled=False)

        async def run():
            st = await r.statuses()
            self.assertEqual(st[0]["enabled"], False)
            self.assertEqual(st[0]["error"], "disabled")
            self.assertFalse(st[0]["ok"])
            self.assertEqual(r.enabled_servers(), [])
            schemas, fns = await r.build_tools(None)
            self.assertEqual(schemas, [])
            self.assertEqual(fns, {})
        asyncio.run(run())

    def test_enabled_server_exposes_catalog(self):
        r = _registry(enabled=True)

        async def run():
            st = await r.statuses()
            self.assertTrue(st[0]["ok"])
            self.assertEqual(st[0]["tools"], ["ro", "wr"])
            ro = next(t for t in st[0]["tool_catalog"] if t["name"] == "ro")
            self.assertTrue(ro["read_only"])
            wr = next(t for t in st[0]["tool_catalog"] if t["name"] == "wr")
            self.assertFalse(wr["read_only"])
            schemas, fns = await r.build_tools(None)
            self.assertEqual(sorted(fns), ["s__ro", "s__wr"])
        asyncio.run(run())

    def test_clear_status_cache(self):
        r = _registry(enabled=True)

        async def run():
            await r.statuses()
            r.clear_status_cache()
            self.assertIsNone(r._status_cache)
        asyncio.run(run())


class TestCallMcpTool(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.r = _registry(enabled=True)

    async def test_read_only_runs_without_permissions(self):
        text, err, _imgs = await call_mcp_tool(self.r, "s", "ro", {"x": 1})
        self.assertIn("ran ro", text)
        self.assertFalse(err)

    async def test_writable_permission_gates(self):
        # no permissions layer -> runs
        text, err, _imgs = await call_mcp_tool(self.r, "s", "wr", {})
        self.assertFalse(err)
        # ask + no broker -> denied with a clear message
        text, err, _imgs = await call_mcp_tool(self.r, "s", "wr", {},
                                        permissions=_Perms("ask"))
        self.assertTrue(err)
        self.assertIn("denied", text)
        # pre-granted allow -> runs
        text, err, _imgs = await call_mcp_tool(self.r, "s", "wr", {},
                                        permissions=_Perms("allow"))
        self.assertFalse(err)
        # policy deny -> blocked
        text, err, _imgs = await call_mcp_tool(self.r, "s", "wr", {},
                                        permissions=_Perms("deny"))
        self.assertTrue(err)
        self.assertIn("blocked", text)

    async def test_broker_approval_flow(self):
        perms = _Perms("ask")
        broker = _Broker(approve=True)
        text, err, _imgs = await call_mcp_tool(self.r, "s", "wr", {},
                                        permissions=perms, broker=broker)
        self.assertEqual(broker.asked, 1)
        self.assertFalse(err)
        self.assertIn("ran wr", text)
        self.assertIn(("s__wr", "allow"), perms.records)

        perms2 = _Perms("ask")
        broker2 = _Broker(approve=False)
        text, err, _imgs = await call_mcp_tool(self.r, "s", "wr", {},
                                        permissions=perms2, broker=broker2)
        self.assertTrue(err)
        self.assertIn("denied by user", text)

    async def test_missing_tool_and_server(self):
        text, err, _imgs = await call_mcp_tool(self.r, "s", "nope", {})
        self.assertTrue(err)
        self.assertIn("not found", text)
        text, err, _imgs = await call_mcp_tool(self.r, "ghost", "ro", {})
        self.assertTrue(err)

    async def test_catalog_includes_params(self):
        schema = {"type": "object",
                  "properties": {"dataset": {"type": "string"},
                                 "k": {"type": "integer"}},
                  "required": ["dataset"]}
        r = MCPRegistry([{"name": "s", "enabled": True}])
        r._available = True
        r._conns["s"] = _FakeConn([_tool("prof", True, schema=schema)])
        st = await r.statuses()
        params = st[0]["tool_catalog"][0]["params"]
        by_name = {p["name"]: p for p in params}
        self.assertTrue(by_name["dataset"]["required"])
        self.assertFalse(by_name["k"]["required"])
        self.assertEqual(by_name["k"]["type"], "integer")

    async def test_required_args_validated(self):
        # a tool that requires {"dataset", "column"}
        schema = {"type": "object", "properties": {"dataset": {"type": "string"},
                                                   "column": {"type": "string"}},
                  "required": ["dataset", "column"]}
        r = MCPRegistry([{"name": "s", "enabled": True}])
        r._available = True
        r._conns["s"] = _FakeConn([_tool("prof", True, schema=schema)])
        text, err, _imgs = await call_mcp_tool(r, "s", "prof", {"dataset": "d.csv"})
        self.assertTrue(err)
        self.assertIn("column", text)
        self.assertIn("required", text)
        # providing all required args runs
        text, err, _imgs = await call_mcp_tool(r, "s", "prof",
                                        {"dataset": "d.csv", "column": "a"})
        self.assertFalse(err)
        self.assertIn("ran prof", text)


class TestMcpRoutes(unittest.IsolatedAsyncioTestCase):
    async def test_call_tool_route(self):
        from backend.routers import system as sysmod
        with mock.patch.object(sysmod, "mcp_registry", _registry(enabled=True)):
            res = await sysmod.call_tool("s", "ro", {"args": {"x": 1}})
            self.assertTrue(res["ok"])
            self.assertIn("ran ro", res["text"])
            # missing tool -> 502
            res = await sysmod.call_tool("s", "nope", {"args": {}})
            self.assertEqual(res.status_code, 502)

    async def test_enabled_toggle_route(self):
        from backend.routers import system as sysmod
        import tempfile
        from pathlib import Path
        tmp = Path(tempfile.mkdtemp())
        # point CONFIG_PATH away so save_config writes to a scratch dir
        with mock.patch.object(sysmod, "CONFIG",
                               {"mcp": {"servers": [{"name": "s", "enabled": True}]}}):
            with mock.patch("backend.state.CONFIG_PATH", tmp / "config.json"):
                with mock.patch.object(sysmod, "rebuild_mcp", new=mock.AsyncMock()) as rb:
                    res = await sysmod.set_mcp_server_enabled("s", {"enabled": False})
                    self.assertTrue(res["ok"])
                    self.assertFalse(res["enabled"])
                    rb.assert_awaited_once()
        # unknown server -> 404
        with mock.patch.object(sysmod, "CONFIG", {"mcp": {"servers": []}}):
            res = await sysmod.set_mcp_server_enabled("nope", {"enabled": True})
            self.assertEqual(res.status_code, 404)

    async def test_edit_server_route(self):
        from backend.routers import system as sysmod
        import tempfile
        from pathlib import Path
        tmp = Path(tempfile.mkdtemp())
        cfg = {"mcp": {"servers": [{"name": "s", "command": "old",
                                    "args": [], "trusted": False}]}}
        with mock.patch.object(sysmod, "CONFIG", cfg):
            with mock.patch("backend.state.CONFIG_PATH", tmp / "config.json"):
                with mock.patch.object(sysmod, "rebuild_mcp", new=mock.AsyncMock()) as rb:
                    res = await sysmod.edit_mcp_server("s", {"command": "new", "trusted": True})
                    self.assertTrue(res["ok"])
                    self.assertEqual(res["server"]["command"], "new")
                    self.assertTrue(res["server"]["trusted"])
                    rb.assert_awaited_once()
        with mock.patch.object(sysmod, "CONFIG", {"mcp": {"servers": []}}):
            res = await sysmod.edit_mcp_server("nope", {"command": "x"})
            self.assertEqual(res.status_code, 404)


class TestConfigRedaction(unittest.TestCase):
    def test_redacts_mcp_secrets(self):
        from backend.routers import system as sysmod
        cfg = {"mcp": {"servers": [
            {"name": "s", "env": {"PYTHONPATH": ".", "API_TOKEN": "sk-abc"},
             "headers": {"Authorization": "Bearer x", "Content-Type": "application/json"}}]},
            "kaggle": {"username": "u", "key": "secret123"}}
        out = sysmod._redact_config(cfg)
        s = out["mcp"]["servers"][0]
        self.assertEqual(s["env"]["PYTHONPATH"], ".")
        self.assertEqual(s["env"]["API_TOKEN"], sysmod._MCP_MASK)
        self.assertEqual(s["headers"]["Authorization"], sysmod._MCP_MASK)
        self.assertEqual(s["headers"]["Content-Type"], "application/json")
        self.assertEqual(out["kaggle"]["key"], sysmod._MCP_MASK)

    def test_merge_preserves_real_secrets(self):
        from backend.routers import system as sysmod
        orig = {"name": "s", "env": {"PYTHONPATH": ".", "API_TOKEN": "sk-realsecret"},
                "headers": {"Authorization": "Bearer real"}}
        new = {"name": "s", "env": {"PYTHONPATH": ".", "API_TOKEN": sysmod._MCP_MASK},
               "headers": {"Authorization": sysmod._MCP_MASK}}
        merged = sysmod._merge_mcp_server(orig, new)
        self.assertEqual(merged["env"]["API_TOKEN"], "sk-realsecret")
        self.assertEqual(merged["headers"]["Authorization"], "Bearer real")
        # non-masked values still update
        new2 = {"name": "s", "env": {"PYTHONPATH": "/tmp/x", "API_TOKEN": sysmod._MCP_MASK}}
        merged2 = sysmod._merge_mcp_server(orig, new2)
        self.assertEqual(merged2["env"]["PYTHONPATH"], "/tmp/x")
        self.assertEqual(merged2["env"]["API_TOKEN"], "sk-realsecret")

    def test_saving_masked_kaggle_key_preserves_secret(self):
        from backend.routers import system as sysmod
        cfg = {"llm": {"model": "m"}, "mcp": {"servers": []},
               "kaggle": {"username": "u", "key": "livekey"}}

        async def run():
            with mock.patch.object(sysmod, "CONFIG", cfg):
                with mock.patch.object(sysmod, "runtimes", {}):
                    with mock.patch.object(sysmod, "rebuild_mcp", new=mock.AsyncMock()):
                        res = await sysmod.set_config(
                            {"config": {"kaggle": {"username": "u", "key": sysmod._MCP_MASK}}})
                        self.assertEqual(res["config"]["kaggle"]["key"], "livekey")
                        # an actual new key is written through
                        res2 = await sysmod.set_config(
                            {"config": {"kaggle": {"username": "u", "key": "newkey"}}})
                        self.assertEqual(res2["config"]["kaggle"]["key"], "newkey")
        asyncio.run(run())


class TestMcpChatCommand(unittest.IsolatedAsyncioTestCase):
    """The @mcp chat command (background + sync) against a real runtime."""

    async def asyncSetUp(self):
        import backend.project_runtime as pr
        from backend.state import runtimes
        self.tmp = Path(tempfile.mkdtemp())
        self._orig = pr.PROJECTS_DIR
        pr.PROJECTS_DIR = self.tmp
        self.rt = pr.ProjectRuntime("mcpchat")
        runtimes["mcpchat"] = self.rt
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
        runtimes.pop("mcpchat", None)
        await self.rt.stop()

    async def test_background_command(self):
        from backend.agents.approval import ApprovalBroker
        broker = ApprovalBroker(self.emit)
        reg = _registry(enabled=True, wr_reply='{"ok": 1}')
        self.rt.permissions.record("mcp_tool", "s__wr", "allow")
        # keep the registry patch alive while the background task runs
        with mock.patch.object(self.mainmod, "mcp_registry", reg):
            await self.mainmod._handle_mcp_command(
                self.rt, self.emit, broker, "@mcp bg s__wr {}")
            msgs = [p for t, p in self.events if t == "assistant_message"]
            self.assertTrue(any("started in the background" in p["content"] for p in msgs))
            self.assertTrue(any(t == "done" for t, _ in self.events))
            runs = self.rt.store.list_runs()
            self.assertEqual(len(runs), 1)
            self.assertEqual(runs[0]["status"], "running")
            # background task completes and updates the run
            for _ in range(100):
                run = self.rt.store.get_run(runs[0]["id"])
                if run["status"] != "running":
                    break
                await asyncio.sleep(0.05)
        self.assertEqual(run["status"], "done")
        self.assertIn("ok", run["reply"])

    async def test_discovery_lists_tools(self):
        from backend.agents.approval import ApprovalBroker
        broker = ApprovalBroker(self.emit)
        reg = _registry(enabled=True)
        with mock.patch.object(self.mainmod, "mcp_registry", reg):
            await self.mainmod._handle_mcp_command(
                self.rt, self.emit, broker, "@mcp")
        msgs = [p for t, p in self.events if t == "assistant_message"]
        self.assertTrue(any("s__ro" in p["content"] and "s__wr" in p["content"]
                            for p in msgs))


class TestMcpProjectIntegration(unittest.IsolatedAsyncioTestCase):
    """Tool calls + grants against a real project runtime (records runs)."""

    async def asyncSetUp(self):
        import backend.project_runtime as pr
        from backend.state import runtimes
        self.tmp = Path(tempfile.mkdtemp())
        self._orig = pr.PROJECTS_DIR
        pr.PROJECTS_DIR = self.tmp
        self.rt = pr.ProjectRuntime("mpcproj")
        runtimes["mpcproj"] = self.rt
        from backend.routers import system as sysmod
        self.sysmod = sysmod

    async def asyncTearDown(self):
        import backend.project_runtime as pr
        from backend.state import runtimes
        pr.PROJECTS_DIR = self._orig
        runtimes.pop("mpcproj", None)
        await self.rt.stop()

    async def test_call_records_run(self):
        with mock.patch.object(self.sysmod, "mcp_registry", _registry(enabled=True)):
            res = await self.sysmod.call_tool("s", "ro", {"args": {"x": 1},
                                                          "project": "mpcproj"})
            self.assertTrue(res["ok"])
        runs = self.rt.store.list_runs()
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[-1]["kind"], "mcp_tool")
        self.assertEqual(runs[-1]["label"], "s__ro")
        self.assertIn("ran ro", runs[-1]["reply"])

    async def test_grants_roundtrip(self):
        import json as _json
        g = await self.sysmod.mcp_tool_grants("mpcproj")
        self.assertEqual(g["grants"], {})
        set_res = await self.sysmod.set_mcp_tool_grant(
            "mpcproj", {"key": "s__wr", "decision": "allow"})
        self.assertTrue(set_res["ok"])
        self.assertEqual(set_res["grants"].get("s__wr"), "allow")
        g2 = await self.sysmod.mcp_tool_grants("mpcproj")
        self.assertEqual(g2["grants"].get("s__wr"), "allow")
        # invalid decision -> 400
        bad = await self.sysmod.set_mcp_tool_grant("mpcproj", {"key": "s__wr", "decision": "maybe"})
        self.assertEqual(bad.status_code, 400)
        # reset to ask
        await self.sysmod.set_mcp_tool_grant("mpcproj", {"key": "s__wr", "decision": "ask"})
        self.assertEqual((await self.sysmod.mcp_tool_grants("mpcproj"))["grants"].get("s__wr"), "ask")

    async def test_grant_allows_writable_call(self):
        perms = self.rt.permissions
        perms.record("mcp_tool", "s__wr", "allow")
        with mock.patch.object(self.sysmod, "mcp_registry", _registry(enabled=True)):
            res = await self.sysmod.call_tool("s", "wr", {"args": {}, "project": "mpcproj"})
            self.assertTrue(res["ok"])

    async def test_writable_unapproved_denied(self):
        import json as _json
        with mock.patch.object(self.sysmod, "mcp_registry", _registry(enabled=True)):
            res = await self.sysmod.call_tool("s", "wr", {"args": {}, "project": "mpcproj"})
            self.assertEqual(res.status_code, 403)
            self.assertEqual(_json.loads(res.body)["permission_key"], "s__wr")
        # denied calls are NOT recorded as runs
        self.assertEqual(len(self.rt.store.list_runs()), 0)

    async def test_save_result_artifact(self):
        res = await self.sysmod.save_mcp_result_artifact(
            "mpcproj", {"name": "probe-output", "text": "hello world"})
        self.assertTrue(res["ok"])
        art = self.rt.artifacts.get(res["artifact_id"])
        self.assertIsNotNone(art)
        self.assertEqual(art.name, "probe-output")
        # empty text -> 400
        bad = await self.sysmod.save_mcp_result_artifact("mpcproj", {"name": "x", "text": ""})
        self.assertEqual(bad.status_code, 400)

    async def test_track_as_experiment(self):
        json_reply = '{"accuracy": 0.91, "rows": 100}'
        reg = _registry(enabled=True, wr_reply=json_reply)
        perms = self.rt.permissions
        perms.record("mcp_tool", "s__wr", "allow")
        with mock.patch.object(self.sysmod, "mcp_registry", reg):
            res = await self.sysmod.call_tool("s", "wr", {"args": {}, "project": "mpcproj",
                                                          "experiment": True})
            self.assertTrue(res["ok"])
            self.assertIsNotNone(res["run_id"])
            self.assertIsNotNone(res["experiment_id"])
        runs = self.rt.store.list_runs()
        self.assertEqual(runs[-1]["kind"], "mcp_tool")
        self.assertEqual(runs[-1]["experiment_id"], res["experiment_id"])
        self.assertEqual(runs[-1]["metrics"].get("accuracy"), 0.91)
        exp = next(e for e in self.rt.store.list_experiments()
                   if e["id"] == res["experiment_id"])
        self.assertTrue(exp["name"].startswith("🧪 s__wr"))
        # without the track flag, no experiment is created
        with mock.patch.object(self.sysmod, "mcp_registry", reg):
            await self.sysmod.call_tool("s", "wr", {"args": {}, "project": "mpcproj"})
        self.assertEqual(len(self.rt.store.list_experiments()), 1)

    async def test_update_run(self):
        rid = self.rt.store.add_run("p", "r", "done", 0.0, 1.0)
        self.assertTrue(self.rt.store.update_run(
            rid, status="error", reply="boom", metrics={"a": 1.0},
            artifact_ids=["fig1"]))
        run = self.rt.store.get_run(rid)
        self.assertEqual(run["status"], "error")
        self.assertEqual(run["reply"], "boom")
        self.assertEqual(run["metrics"], {"a": 1.0})
        self.assertEqual(run["artifact_ids"], ["fig1"])
        # unknown fields are ignored (no update attempted)
        self.assertFalse(self.rt.store.update_run(rid, bogus_field=1))

    async def test_background_call(self):
        reg = _registry(enabled=True, wr_reply='{"ok": 1}')
        perms = self.rt.permissions
        perms.record("mcp_tool", "s__wr", "allow")
        with mock.patch.object(self.sysmod, "mcp_registry", reg):
            res = await self.sysmod.call_tool(
                "s", "wr", {"args": {}, "project": "mpcproj", "background": True})
            self.assertTrue(res["ok"])
            self.assertTrue(res["background"])
            self.assertIsNotNone(res["run_id"])
            run = self.rt.store.get_run(res["run_id"])
            self.assertEqual(run["status"], "running")
            # wait for the background task to complete
            for _ in range(100):
                run = self.rt.store.get_run(res["run_id"])
                if run["status"] != "running":
                    break
                await asyncio.sleep(0.05)
            self.assertEqual(run["status"], "done")
            self.assertIn("ok", run["reply"])
        # background without a project -> 400
        bad = await self.sysmod.call_tool("s", "wr", {"args": {}, "background": True})
        self.assertEqual(bad.status_code, 400)

    async def test_activity_endpoint(self):
        # two mcp_tool runs + one non-mcp run; activity lists only mcp_tool.
        r1 = self.rt.store.add_run("p1", "r1", "done", 0.0, 1.0, kind="mcp_tool", label="a__t1")
        r2 = self.rt.store.add_run("p2", "r2", "done", 0.0, 1.0, kind="mcp_tool", label="a__t2")
        self.rt.store.add_run("p3", "r3", "done", 0.0, 1.0, kind="agent_run")
        act = await self.sysmod.mcp_activity("mpcproj")
        calls = act["calls"]
        self.assertEqual(len(calls), 2)
        # newest first
        self.assertEqual(calls[0]["label"], "a__t2")
        self.assertEqual(calls[0]["status"], "done")
        self.assertIn("reply", calls[0])


if __name__ == "__main__":
    unittest.main()
