"""Fox CLI — AI Research Workbench command-line front-end.

Entry point: ``python -m cli`` or the ``fox`` console script.
"""

from __future__ import annotations

import argparse
import sys

from . import commands, ui
from .splash import render_splash_panel

PROG = "fox"
VERSION = commands.VERSION


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=ui.accent("fox") + ui.dim(" — AI Research Workbench CLI"),
        epilog=ui.dim("run `fox manual` for the full manual  ·  "
                      "`fox` alone opens the interactive shell"))
    parser.add_argument("--version", action="version",
                        version=f"fox {VERSION}")
    parser.add_argument("--url", default=None,
                        help="server base URL (default $FOX_URL or "
                             "http://127.0.0.1:8765)")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    sub.add_parser("splash", help="render the fox splash panel")
    sub.add_parser("version", help="show version")
    sub.add_parser("status", help="workbench + model + research overview")
    sub.add_parser("doctor", help="environment check")

    p_serve = sub.add_parser("serve", help="launch the workbench server")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8765)

    p_graph = sub.add_parser("graph", help="knowledge-graph summary")

    p_papers = sub.add_parser("papers", help="list ingested papers")

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

    p_exp = sub.add_parser("experiments", help="list / start experiments")
    p_exp.add_argument("project")
    p_exp.add_argument("action", nargs="?", default="list",
                       choices=["list", "start", "run-obfuscation"])
    p_exp.add_argument("--n-rows", type=int, default=2000,
                       help="synthetic bank transactions (run-obfuscation)")
    p_exp.add_argument("--seed", type=int, default=42,
                       help="RNG seed (run-obfuscation)")

    p_res = sub.add_parser("research", help="research scenarios + autoresearch")
    p_res.add_argument("action", nargs="?", default="list",
                       choices=["list", "status", "report",
                                "build", "synthesize", "experiments", "loop"])
    p_res.add_argument("scenario", nargs="?", default=None,
                       help="scenario id")

    p_man = sub.add_parser("manual", help="print the manual")
    p_man.add_argument("topic", nargs="?", default=None,
                       help="section (quickstart|status|projects|research|graph)")

    return parser


def _ns_url(ns: argparse.Namespace) -> argparse.Namespace:
    ns.url = getattr(ns, "url", None) or None
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
        "version": commands.cmd_version,
        "status": commands.cmd_status,
        "doctor": commands.cmd_doctor,
        "serve": commands.cmd_serve,
        "projects": commands.cmd_projects,
        "runs": commands.cmd_runs,
        "experiments": commands.cmd_experiments,
        "research": commands.cmd_research,
        "graph": commands.cmd_graph,
        "papers": commands.cmd_papers,
        "manual": commands.cmd_manual,
    }
    handler = handlers.get(cmd)
    if handler is None:
        parser.print_help()
        return 2
    return handler(ns)


if __name__ == "__main__":
    raise SystemExit(main())
