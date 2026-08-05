"""Tests for the `fox` CLI: entry points, splash, manual, UI toolkit, and
command handlers. Network-dependent commands point at an unreachable port so
the suite runs anywhere without a live workbench server."""

from __future__ import annotations

import io
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


if __name__ == "__main__":
    unittest.main()
