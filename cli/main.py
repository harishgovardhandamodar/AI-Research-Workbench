"""Fox CLI — AI Research Workbench command-line front-end.

Entry point: ``python -m cli`` or the ``fox`` console script.
"""

from __future__ import annotations

import argparse
import sys

from . import commands, ui
from .log import log as _log
from .splash import render_splash_panel

PROG = "fox"
VERSION = commands.VERSION


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=ui.accent("fox") + ui.dim(" — AI Research Workbench CLI"),
        epilog=ui.dim("run `fox manual` for the full manual  ·  "
                      "`fox` alone opens the terminal window"))
    parser.add_argument("--version", action="version",
                        version=f"fox {VERSION}")
    parser.add_argument("--url", default=None,
                        help="server base URL (default $FOX_URL or "
                             "http://127.0.0.1:8765)")
    parser.add_argument("--json", action="store_true",
                        help="machine-readable JSON output on stdout")
    parser.add_argument("--quiet", action="store_true",
                        help="suppress spinner/progress output")
    parser.add_argument("--debug", action="store_true",
                        help="debug logging to stderr (also: FOX_DEBUG=1)")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    sub.add_parser("splash", help="render the fox splash panel")
    p_tui = sub.add_parser("tui", help="open the full-screen terminal window")
    p_tui.add_argument("--theme", default=None,
                       help="theme name (opencode-dark|light|midnight|…)")
    p_tui.add_argument("--render-preview", action="store_true",
                       help="print a static frame and exit (docs/tests)")
    p_tui.add_argument("--view", default="output",
                       choices=["output", "logs", "settings", "help", "theme"],
                       help="view for --render-preview")
    p_tui.add_argument("--width", type=int, default=88)
    p_tui.add_argument("--height", type=int, default=26)
    sub.add_parser("version", help="show version")
    sub.add_parser("status", help="workbench + model + research overview")
    sub.add_parser("doctor", help="environment check")

    p_serve = sub.add_parser("serve", help="launch the workbench server")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8765)

    p_graph = sub.add_parser("graph", help="knowledge-graph summary")

    p_papers = sub.add_parser("papers", help="list / search / ingest papers")
    p_papers.add_argument("action", nargs="?", default="list",
                          choices=["list", "search", "add"])
    p_papers.add_argument("query", nargs="?", default=None,
                          help="search term, arXiv id, or URL (search/add)")

    p_projects = sub.add_parser("projects", help="manage projects")
    p_projects.add_argument("action", nargs="?", default="list",
                            choices=["list", "new", "show", "rm", "fork"])
    p_projects.add_argument("project", nargs="?", default=None,
                            help="project name")
    p_projects.add_argument("target", nargs="?", default=None,
                            help="target name for `fork`")
    p_projects.add_argument("-d", "--description", default="")

    p_runs = sub.add_parser("runs", help="list runs of a project")
    p_runs.add_argument("project")

    p_audit = sub.add_parser("audit", help="agent audit trail for a project")
    p_audit.add_argument("project")
    p_audit.add_argument("action", nargs="?", default="overview",
                         choices=["overview", "events", "deviations",
                                  "agents", "verify"])

    p_run = sub.add_parser("run", help="inspect a run / generate report")
    p_run.add_argument("project")
    p_run.add_argument("rid", help="run id")
    p_run.add_argument("action", nargs="?", default="show",
                       choices=["show", "report"])

    p_exp = sub.add_parser("experiments", help="list / start experiments")
    p_exp.add_argument("project")
    p_exp.add_argument("action", nargs="?", default="list",
                       choices=["list", "start", "run-obfuscation"])
    p_exp.add_argument("--name", default=None,
                       help="experiment name (start; default '<project> experiment')")
    p_exp.add_argument("--hypothesis", default="",
                       help="hypothesis (start)")
    p_exp.add_argument("--goal-metric", default="",
                       help="goal metric name (start)")
    p_exp.add_argument("--goal-target", type=float, default=None,
                       help="goal target value (start)")
    p_exp.add_argument("--plan", default="",
                       help="experiment plan (start)")
    p_exp.add_argument("--n-rows", type=int, default=2000,
                       help="synthetic bank transactions (run-obfuscation)")
    p_exp.add_argument("--seed", type=int, default=42,
                       help="RNG seed (run-obfuscation)")

    p_experiment = sub.add_parser("experiment",
                                  help="inspect an experiment / ranking")
    p_experiment.add_argument("project")
    p_experiment.add_argument("eid", help="experiment id")
    p_experiment.add_argument("action", nargs="?", default="show",
                              choices=["show", "ranking"])
    p_experiment.add_argument("--metric", default="",
                              help="ranking metric (default: goal_metric)")

    p_compare = sub.add_parser("compare", help="metric delta between two runs")
    p_compare.add_argument("project")
    p_compare.add_argument("run_a")
    p_compare.add_argument("run_b")

    p_eda = sub.add_parser("eda", help="exploratory data analysis + report")
    p_eda.add_argument("dataset", nargs="?", default=None,
                       help="dataset path or URL (csv/parquet/excel)")
    p_eda.add_argument("--format", default="auto",
                       help="file format (auto|csv|parquet|excel|json)")
    p_eda.add_argument("--llm", action="store_true",
                       help="write narrative sections with a LOCAL model (FOX_MODEL)")
    p_eda.add_argument("--html", action="store_true",
                       help="also export the report as HTML")

    p_res = sub.add_parser("research", help="research scenarios + autoresearch")
    p_res.add_argument("action", nargs="?", default="list",
                       choices=["list", "status", "report",
                                "build", "synthesize", "experiments", "loop"])
    p_res.add_argument("scenario", nargs="?", default=None,
                       help="scenario id")

    p_manage = sub.add_parser("manage", help="experiment management repo")
    p_manage.add_argument("action", nargs="?", default="status",
                          choices=["repos", "status", "link",
                                   "commit", "push", "commit-and-push"])
    p_manage.add_argument("project", nargs="?", default=None,
                          help="project name (commit / push)")
    p_manage.add_argument("github_repo", nargs="?",
                          help="owner/repo (link)")
    p_manage.add_argument("-m", "--message", default="",
                          help="commit message (commit / commit-and-push)")

    p_jobs = sub.add_parser("jobs", help="list / inspect RKG background jobs")
    p_jobs.add_argument("job_id", nargs="?", default=None)

    p_sched = sub.add_parser("scheduler", help="research scheduler status")

    p_pool = sub.add_parser("pool", help="research pool (papers + topics)")
    p_pool.add_argument("action", nargs="?", default="list",
                        choices=["list", "topics", "topics-add", "topics-rm",
                                 "import"])
    p_pool.add_argument("name", nargs="?", default=None,
                        help="topic name (topics-add / topics-rm)")
    p_pool.add_argument("query", nargs="?", default=None,
                        help="arxiv search query (topics-add)")
    p_pool.add_argument("arxiv_id", nargs="?", default=None,
                        help="arxiv id (import)")

    p_man = sub.add_parser("manual", help="print the manual")
    p_man.add_argument("topic", nargs="?", default=None,
                       help="section (quickstart|status|projects|research|graph)")

    return parser


def _ns_url(ns: argparse.Namespace) -> argparse.Namespace:
    ns.url = getattr(ns, "url", None) or None
    if getattr(ns, "debug", False):
        _log.set_level("DEBUG")
    return ns


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        return commands.cmd_interactive(argparse.Namespace(url=None))
    parser = _build_parser()
    ns = parser.parse_args(argv)
    ns = _ns_url(ns)

    cmd = ns.command
    handlers = {
        "splash": commands.cmd_splash,
        "tui": commands.cmd_interactive,
        "version": commands.cmd_version,
        "status": commands.cmd_status,
        "doctor": commands.cmd_doctor,
        "serve": commands.cmd_serve,
        "projects": commands.cmd_projects,
        "runs": commands.cmd_runs,
        "run": commands.cmd_run,
        "audit": commands.cmd_audit,
        "experiments": commands.cmd_experiments,
        "experiment": commands.cmd_experiment,
        "compare": commands.cmd_compare,
        "eda": commands.cmd_eda,
        "research": commands.cmd_research,
        "graph": commands.cmd_graph,
        "papers": commands.cmd_papers,
        "jobs": commands.cmd_jobs,
        "scheduler": commands.cmd_scheduler,
        "pool": commands.cmd_pool,
        "manage": commands.cmd_manage,
        "manual": commands.cmd_manual,
    }
    handler = handlers.get(cmd)
    if handler is None:
        parser.print_help()
        return 2
    return handler(ns)


if __name__ == "__main__":
    raise SystemExit(main())
