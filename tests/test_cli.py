"""Tests for the `fox` CLI: entry points, splash, manual, UI toolkit, and
command handlers. Network-dependent commands point at an unreachable port so
the suite runs anywhere without a live workbench server."""

from __future__ import annotations

import io
import json
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import cli.commands as commands
import cli.main as cli_main
import cli.ui as ui
from cli.client import FoxClient, FoxClientError
from cli.interactive import _repl_args
from cli.manual import MANUAL

ROOT = Path(__file__).resolve().parent.parent
OFFLINE = "--url=http://127.0.0.1:9"


def run_cli(argv):
    """Run main() capturing stdout; returns (exit_code, out)."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        try:
            code = cli_main.main(argv)
        except SystemExit as e:
            code = e.code or 0
    return code, buf.getvalue()


class EntryPointTests(unittest.TestCase):

    def test_version(self):
        with patch("sys.argv", ["fox", "--version"]):
            with self.assertRaises(SystemExit) as ctx:
                cli_main.main(["--version"])
        self.assertIn(ctx.exception.code, (0, None))

    def test_help_lists_subcommands(self):
        code, out = run_cli(["--help"])
        self.assertEqual(code, 0)
        for sub in ("status", "projects", "research", "graph", "manual", "serve"):
            self.assertIn(sub, out)

    def test_no_args_opens_interactive(self):
        with patch("cli.commands.cmd_interactive", return_value=0) as m:
            code = cli_main.main([])
        self.assertEqual(code, 0)
        m.assert_called_once()

    def test_unknown_command_prints_help(self):
        code, out = run_cli(["frobnicate"])
        self.assertEqual(code, 2)

    def test_python_dash_m_entry(self):
        res = subprocess.run(
            [sys.executable, "-m", "cli", "--version"],
            cwd=ROOT, capture_output=True, text=True, timeout=30)
        self.assertEqual(res.returncode, 0)
        self.assertIn("fox 0.1.0", res.stdout)


class SplashTests(unittest.TestCase):

    def test_static_splash_panel(self):
        from cli.splash import render_splash_panel

        text = render_splash_panel("0.1.0")
        self.assertIn("fox", text)
        self.assertIn("0.1.0", text)
        self.assertIn("AI Research Workbench", text)

    def test_animate_does_not_crash(self):
        from cli.splash import animate, fox_frame

        frame = fox_frame("o o", "|")
        self.assertIn("o o", frame)
        buf = io.StringIO()
        animate(steps=2, delay=0.001, stream=buf)
        self.assertGreater(len(buf.getvalue()), 0)


class ManualTests(unittest.TestCase):

    def test_full_manual(self):
        self.assertIn("## Quick start", MANUAL.text)
        self.assertIn("### `fox research`", MANUAL.text)

    def test_section(self):
        text = MANUAL.section("research")
        self.assertIn("fox research", text)
        self.assertIn("autoresearch", text.lower())

    def test_bad_section(self):
        with self.assertRaises(SystemExit):
            MANUAL.section("nope")


class UiToolkitTests(unittest.TestCase):

    def test_panel_box(self):
        text = ui.panel("status", "body")
        self.assertIn("╭", text)
        self.assertIn("╰", text)
        self.assertIn("body", text)

    def test_table_aligns(self):
        text = ui.table(["a", "b"], [["1", "2"], ["longer", "4"]])
        self.assertIn("longer", text)

    def test_ansi_strip_roundtrip(self):
        colored = ui.accent("hello")
        self.assertEqual(ui.strip_ansi(colored), "hello")

    def test_spinner_lifecycle(self):
        sp = ui.Spinner("working", stream=io.StringIO())
        sp.start()
        sp.stop()
        self.assertTrue(sp._stop.is_set())

    def test_run_with_spinner_passes_args(self):
        captured = []

        def target(*args, **kwargs):
            captured.append((args, kwargs))
            return "ok"

        out = ui.run_with_spinner("x", target, 1, two=2)
        self.assertEqual(out, "ok")
        self.assertEqual(captured, [((1,), {"two": 2})])

    def test_progress_bar(self):
        self.assertIn("100%", ui.progress(1.0))
        self.assertIn("0%", ui.progress(0.0))


class ClientTests(unittest.TestCase):

    def test_offline_raises_structured_error(self):
        c = FoxClient("http://127.0.0.1:9", timeout=1)
        with self.assertRaises(FoxClientError) as ctx:
            c.health()
        self.assertEqual(ctx.exception.status, 0)

    def test_base_url_env(self):
        with patch.dict("os.environ", {"FOX_URL": "http://example:9999"}, clear=False):
            self.assertEqual(commands._client(SimpleNamespace(url=None)).url,
                             "http://example:9999")


class CommandTests(unittest.TestCase):

    def _ns(self, **kw):
        return SimpleNamespace(url="http://127.0.0.1:9", action="list",
                               project=None, scenario=None, target=None,
                               description="", topic=None, **kw)

    def test_status_offline_exit_1(self):
        code, _ = run_cli([OFFLINE, "status"])
        self.assertEqual(code, 1)

    def test_doctor_offline(self):
        code, out = run_cli([OFFLINE, "doctor"])
        self.assertIn("doctor", out)

    def test_projects_list_offline(self):
        code, _ = run_cli([OFFLINE, "projects"])
        self.assertEqual(code, 1)

    def test_projects_requires_name_for_new(self):
        # offline: server contact fails -> exit 1 (not a crash)
        code, _ = run_cli([OFFLINE, "projects", "new", "proj"])
        self.assertEqual(code, 1)

    def test_runs_offline(self):
        code, _ = run_cli([OFFLINE, "runs", "proj"])
        self.assertEqual(code, 1)

    def test_experiments_offline(self):
        code, _ = run_cli([OFFLINE, "experiments", "proj"])
        self.assertEqual(code, 1)

    def test_experiments_run_obfuscation_offline(self):
        # offline: server contact fails -> exit 1 (not a crash)
        code, out = run_cli([OFFLINE, "experiments", "proj", "run-obfuscation"])
        self.assertEqual(code, 1)
        self.assertIn("cannot reach", out)

    def test_experiments_run_obfuscation_accepts_flags(self):
        code, out = run_cli([OFFLINE, "experiments", "proj", "run-obfuscation",
                             "--n-rows", "300", "--seed", "7"])
        self.assertEqual(code, 1)

    def test_research_list_offline(self):
        code, _ = run_cli([OFFLINE, "research"])
        self.assertEqual(code, 1)

    def test_research_status_needs_scenario(self):
        code, _ = run_cli([OFFLINE, "research", "status"])
        self.assertEqual(code, 2)

    def test_graph_offline(self):
        code, _ = run_cli([OFFLINE, "graph"])
        self.assertEqual(code, 1)

    def test_papers_offline(self):
        code, _ = run_cli([OFFLINE, "papers"])
        self.assertEqual(code, 1)

    def test_run_offline(self):
        code, _ = run_cli([OFFLINE, "run", "proj", "1"])
        self.assertEqual(code, 1)

    def test_run_report_offline(self):
        code, _ = run_cli([OFFLINE, "run", "proj", "1", "report"])
        self.assertEqual(code, 1)

    def test_run_requires_rid(self):
        code, _ = run_cli([OFFLINE, "run", "proj"])
        self.assertEqual(code, 2)

    def test_experiment_offline(self):
        code, _ = run_cli([OFFLINE, "experiment", "proj", "1"])
        self.assertEqual(code, 1)

    def test_experiment_ranking_offline(self):
        code, _ = run_cli([OFFLINE, "experiment", "proj", "1", "ranking"])
        self.assertEqual(code, 1)

    def test_compare_offline(self):
        code, _ = run_cli([OFFLINE, "compare", "proj", "1", "2"])
        self.assertEqual(code, 1)

    def test_compare_requires_both_runs(self):
        code, _ = run_cli([OFFLINE, "compare", "proj", "1"])
        self.assertEqual(code, 2)

    def test_jobs_offline(self):
        code, _ = run_cli([OFFLINE, "jobs"])
        self.assertEqual(code, 1)

    def test_scheduler_offline(self):
        code, _ = run_cli([OFFLINE, "scheduler"])
        self.assertEqual(code, 1)

    def test_pool_offline(self):
        code, _ = run_cli([OFFLINE, "pool"])
        self.assertEqual(code, 1)

    def test_pool_topics_add_requires_name_and_query(self):
        code, _ = run_cli([OFFLINE, "pool", "topics-add", "t"])
        self.assertEqual(code, 2)

    def test_manage_status_offline(self):
        code, _ = run_cli([OFFLINE, "manage"])
        self.assertEqual(code, 1)

    def test_manage_commit_requires_project(self):
        code, _ = run_cli([OFFLINE, "manage", "commit"])
        self.assertEqual(code, 2)

    def test_json_projects_offline_emits_json(self):
        code, out = run_cli([OFFLINE, "--json", "projects"])
        self.assertEqual(code, 1)
        self.assertIsInstance(json.loads(out), dict)

    def test_json_scheduler_offline_emits_json(self):
        code, out = run_cli([OFFLINE, "--json", "scheduler"])
        self.assertEqual(code, 1)
        self.assertIsInstance(json.loads(out), dict)

    def test_papers_add_offline_dispatch(self):
        # offline: dispatch runs then server contact fails -> exit 1
        code, _ = run_cli([OFFLINE, "papers", "add", "1706.03762"])
        self.assertEqual(code, 1)
        code, _ = run_cli([OFFLINE, "papers", "add", "https://example.com/x"])
        self.assertEqual(code, 1)

    def test_manual_exit_0(self):
        code, out = run_cli(["manual", "research"])
        self.assertEqual(code, 0)
        self.assertIn("fox research", out)


class ReplParsingTests(unittest.TestCase):

    def test_projects_list(self):
        args = _repl_args("projects list")
        self.assertEqual(args.action, "list")

    def test_projects_new(self):
        args = _repl_args("projects new alpha -d 'my project'")
        self.assertEqual(args.action, "new")
        self.assertEqual(args.project, "alpha")
        self.assertEqual(args.description, "my project")

    def test_research_status(self):
        args = _repl_args("research status autonomous-agents-security")
        self.assertEqual(args.action, "status")
        self.assertEqual(args.scenario, "autonomous-agents-security")

    def test_runs(self):
        args = _repl_args("runs fraud-demo")
        self.assertEqual(args.project, "fraud-demo")

    def test_experiments_start_name(self):
        args = _repl_args("experiments obf-bank-demo start --name 'eps sweep'")
        self.assertEqual(args.project, "obf-bank-demo")
        self.assertEqual(args.action, "start")
        self.assertEqual(args.exp_name, "eps sweep")

    def test_experiments_start_goal_flags(self):
        args = _repl_args("experiments proj start --goal-metric accuracy "
                          "--goal-target 0.9 --hypothesis h --plan p")
        self.assertEqual(args.action, "start")
        self.assertEqual(args.goal_metric, "accuracy")
        self.assertEqual(args.goal_target, 0.9)
        self.assertEqual(args.hypothesis, "h")
        self.assertEqual(args.plan, "p")

    def test_run_repl(self):
        args = _repl_args("run proj 8 report")
        self.assertEqual(args.project, "proj")
        self.assertEqual(args.rid, "8")
        self.assertEqual(args.action, "report")

    def test_experiment_repl(self):
        args = _repl_args("experiment proj 3 ranking --metric acc")
        self.assertEqual(args.project, "proj")
        self.assertEqual(args.eid, "3")
        self.assertEqual(args.action, "ranking")
        self.assertEqual(args.metric, "acc")

    def test_compare_repl(self):
        args = _repl_args("compare proj 1 2")
        self.assertEqual(args.project, "proj")
        self.assertEqual(args.run_a, "1")
        self.assertEqual(args.run_b, "2")

    def test_manage_commit_repl(self):
        args = _repl_args("manage commit proj -m msg")
        self.assertEqual(args.action, "commit")
        self.assertEqual(args.project, "proj")
        self.assertEqual(args.message, "msg")

    def test_manage_link_repl(self):
        args = _repl_args("manage link owner/repo")
        self.assertEqual(args.action, "link")
        self.assertEqual(args.github_repo, "owner/repo")

    def test_papers_add_repl(self):
        args = _repl_args("papers add 1706.03762 --json")
        self.assertEqual(args.action, "add")
        self.assertEqual(args.query, "1706.03762")
        self.assertTrue(args.json)

    def test_pool_repl(self):
        args = _repl_args("pool topics-add t q")
        self.assertEqual(args.action, "topics-add")
        self.assertEqual(args.name, "t")
        self.assertEqual(args.query, "q")
        args = _repl_args("pool import 1706.03762")
        self.assertEqual(args.action, "import")
        self.assertEqual(args.arxiv_id, "1706.03762")


class PaperAddDispatchTests(unittest.TestCase):

    def _fake_client(self, hits):
        cli = object.__new__(FoxClient)
        job = {"id": "j1", "status": "done"}

        def web(url, model=""):
            hits.append(("web", url))
            return job

        def pool(aid):
            hits.append(("pool", aid))
            return job

        def search(q, model=""):
            hits.append(("search", q))
            return job

        cli.rkg_web_add = web
        cli.rkg_pool_import = pool
        cli.rkg_import = search
        cli.wait_job = lambda jid: job
        return cli, hits

    def test_arxiv_id_routes_to_pool(self):
        for ref in ("1706.03762", "arXiv:1706.03762", "2512.21799v1"):
            hits = []
            cli, hits = self._fake_client(hits)
            commands._dispatch_paper_add(
                SimpleNamespace(json=False, quiet=True), cli, ref)
            self.assertEqual(hits[0][0], "pool")

    def test_url_routes_to_web(self):
        hits = []
        cli, hits = self._fake_client(hits)
        commands._dispatch_paper_add(
            SimpleNamespace(json=False, quiet=True), cli, "https://arxiv.org/abs/1706.03762")
        self.assertEqual(hits[0][0], "web")

    def test_query_routes_to_search(self):
        hits = []
        cli, hits = self._fake_client(hits)
        commands._dispatch_paper_add(
            SimpleNamespace(json=False, quiet=True), cli, "graph neural networks")
        self.assertEqual(hits[0][0], "search")


class NewClientMethodTests(unittest.TestCase):

    def _recording(self, path_out):
        def record(path, body=None, **kw):
            return path_out
        return record

    def test_run_wraps(self):
        c = FoxClient("http://x")
        c.get = self._recording({"run": {"id": 1}})
        self.assertEqual(c.run("p", "2"), {"id": 1})

    def test_compare_query_params(self):
        c = FoxClient("http://x")
        c.get = lambda path: {"comparison": path}
        out = c.compare("p", "1 2", "3")
        self.assertIn("run_a=1%202", out)
        self.assertIn("run_b=3", out)

    def test_experiment_ranking_metric_param(self):
        c = FoxClient("http://x")
        c.get = lambda path: path
        out = c.experiment_ranking("p", "4", metric="acc")
        self.assertIn("/api/projects/p/experiments/4/ranking", out)
        self.assertIn("metric=acc", out)

    def test_management_endpoints(self):
        c = FoxClient("http://x")
        calls = []

        def get(path):
            calls.append(("GET", path))
            return {"repos": []}

        def post(path, body=None, **kw):
            calls.append(("POST", path))
            return {"ok": True}

        c.get, c.post = get, post
        c.mgmt_status()
        c.mgmt_repos()
        c.mgmt_commit("p", "m")
        c.mgmt_push("p")
        self.assertEqual(calls[0][1], "/api/management/status")
        self.assertIn("management/commit", calls[2][1])
        self.assertIn("management/push", calls[3][1])

    def test_papers_search_bare_array(self):
        c = FoxClient("http://x")
        c.get = lambda path: [{"id": "1", "title": "t"}]
        out = c.rkg_papers_search("graph")
        self.assertIsInstance(out, list)

    def test_scheduler_and_jobs(self):
        c = FoxClient("http://x")
        c.get = lambda path: {"enabled": True} if "scheduler" in path \
            else [{"id": "j1"}]
        self.assertEqual(c.rkg_scheduler_status()["enabled"], True)
        self.assertIsInstance(c.rkg_jobs(), list)

    def test_pool_endpoints(self):
        c = FoxClient("http://x")
        calls = []

        def get(path):
            calls.append(("GET", path))
            return {"topics": [{"name": "t", "query": "q"}]}

        def post(path, body=None, **kw):
            calls.append(("POST", path))
            return {"status": "ok"}

        c.get, c.post = get, post
        c.rkg_pool_topics()
        c.rkg_pool_topic_add("t", "q")
        c.rkg_pool_topic_remove("t")
        self.assertIn("topics/add", calls[1][1])
        self.assertIn("topics/remove", calls[2][1])


class StartExperimentBodyTests(unittest.TestCase):

    def test_start_sends_default_name(self):
        captured = {}

        def record(path, body, **kw):
            captured["body"] = body
            return {"experiment": {"id": 1}}

        c = FoxClient("http://127.0.0.1:9")
        c.post = record
        c.start_experiment("proj")
        self.assertEqual(captured["body"]["name"], "proj experiment")
        self.assertEqual(captured["body"]["hypothesis"], "")

    def test_start_sends_custom_name_and_goal(self):
        captured = {}

        def record(path, body, **kw):
            captured["body"] = body
            return {"experiment": {"id": 1}}

        c = FoxClient("http://127.0.0.1:9")
        c.post = record
        c.start_experiment("proj", exp_name="eps sweep", hypothesis="h",
                           goal_metric="accuracy", goal_target=0.9, plan="p")
        self.assertEqual(captured["body"]["name"], "eps sweep")
        self.assertEqual(captured["body"]["goal_metric"], "accuracy")
        self.assertEqual(captured["body"]["goal_target"], 0.9)
        self.assertEqual(captured["body"]["hypothesis"], "h")
        self.assertEqual(captured["body"]["plan"], "p")

    def test_start_omits_goal_target_when_none(self):
        captured = {}

        def record(path, body, **kw):
            captured["body"] = body
            return {"experiment": {"id": 1}}

        c = FoxClient("http://127.0.0.1:9")
        c.post = record
        c.start_experiment("proj", goal_metric="accuracy")
        self.assertNotIn("goal_target", captured["body"])

    def test_start_command_offline_does_not_crash(self):
        code, _ = run_cli([OFFLINE, "experiments", "proj", "start"])
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
