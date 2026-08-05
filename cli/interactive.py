"""Interactive shell: `fox` with no arguments.

Shows the animated splash, then drops into a `>` prompt (opencode-style).
Every prompt command reuses the normal subcommand handlers, so behaviour is
identical to the one-shot invocations.
"""

from __future__ import annotations

import shlex
from types import SimpleNamespace

from . import commands, ui
from .ui import accent, bold, c, dim, err, ok

HELP = """\
  Commands
  ────────
  status               workbench / model overview
  doctor               environment check
  projects             list projects          (or: projects new <name> …)
  runs <project>       agent runs
  experiments <proj>   list experiments       (… start)
  research             list scenarios         (or: research status <id> …)
  graph                knowledge graph stats
  papers               latest ingested papers
  serve                launch the server
  manual               open the manual        (or: manual <section>)
  help                 this help
  exit | quit          leave the shell
"""


def _repl_args(argv: list[str]) -> SimpleNamespace:
    """Build an argparse-like namespace from a raw REPL command line."""
    args = SimpleNamespace(url=None, project=None, scenario=None,
                           action=None, target=None, description="",
                           name=None, topic=None, host="127.0.0.1",
                           port=8765)
    tokens = shlex.split(argv)
    if not tokens:
        return args
    cmd = tokens[0]
    rest = tokens[1:]
    if cmd in ("projects", "runs", "experiments", "research"):
        # consume known action words; keep first free word as the entity
        args.action = None
        positional = []
        i = 0
        while i < len(rest):
            t = rest[i]
            if t in ("list", "new", "rm", "show", "fork", "start", "status",
                     "report", "build", "synthesize", "experiments", "loop"):
                if args.action is None:
                    args.action = t
                elif t == "experiments" and args.action in ("synthesize",):
                    pass
                i += 1
                continue
            if t == "--url" and i + 1 < len(rest):
                args.url = rest[i + 1]
                i += 2
                continue
            if t in ("-d", "--desc", "--description") and i + 1 < len(rest):
                args.description = rest[i + 1]
                i += 2
                continue
            positional.append(t)
            i += 1
        if cmd == "runs" and positional:
            args.project = positional[0]
        elif cmd == "experiments" and positional:
            args.project = positional[0]
            if args.action is None:
                args.action = "list"
        elif cmd == "research" and positional:
            args.scenario = positional[0]
        elif cmd == "projects" and positional:
            args.project = positional[0]
            if args.action is None:
                args.action = "show"
        if cmd == "projects" and args.action == "new" and len(positional) > 1:
            args.project = positional[0]
        if cmd == "projects" and args.action == "fork" and len(positional) > 1:
            args.target = positional[1]
    if cmd == "manual" and rest:
        args.topic = rest[0]
    return args


def run_repl(args) -> int:
    try:
        import readline  # noqa: F401  (history / arrow keys when available)
    except ImportError:  # pragma: no cover
        pass

    print()
    from .splash import animated_splash
    from .commands import VERSION

    animated_splash(version=VERSION)
    print(f"\n  {c('type `help` for commands, `exit` to quit', ui.DIM)}")
    print(f"  {c(f'api: {args.url or commands._client(args).url}', ui.FADED)}\n")

    while True:
        try:
            line = input(f"  {accent('fox')} {c('>', ui.DIM)} ").strip()
        except (EOFError, KeyboardInterrupt):
            print(f"\n  {dim('bye')}")
            return 0
        if not line:
            continue
        try:
            code = _dispatch(line)
        except KeyboardInterrupt:
            print(f"\n  {dim('interrupted')}")
            continue
        except SystemExit:
            continue
        if code < 0:
            return 0
        print()


def _dispatch(line: str) -> int:
    tokens = shlex.split(line)
    cmd = tokens[0].lower()
    rest = tokens[1:]
    if cmd in ("exit", "quit", "q"):
        print(f"  {dim('bye')}")
        return -1
    if cmd in ("help", "h", "?"):
        print(HELP)
        return 0
    if cmd == "manual":
        args = _repl_args(line)
        return commands.cmd_manual(args)
    if cmd == "splash":
        return commands.cmd_splash(None)
    if cmd == "version":
        return commands.cmd_version(None)
    if cmd in ("status", "doctor"):
        args = _repl_args(line)
        return commands.cmd_status(args) if cmd == "status" else commands.cmd_doctor(args)
    if cmd == "serve":
        args = _repl_args(line)
        return commands.cmd_serve(args)
    if cmd == "projects":
        args = _repl_args(line)
        args.action = args.action or "list"
        return commands.cmd_projects(args)
    if cmd == "runs":
        args = _repl_args(line)
        return commands.cmd_runs(args)
    if cmd == "experiments":
        args = _repl_args(line)
        return commands.cmd_experiments(args)
    if cmd == "research":
        args = _repl_args(line)
        args.action = args.action or "list"
        return commands.cmd_research(args)
    if cmd == "graph":
        return commands.cmd_graph(SimpleNamespace(url=None))
    if cmd == "papers":
        return commands.cmd_papers(SimpleNamespace(url=None))
    print(f"  {err(f'unknown command `{cmd}`')}  "
          + dim("(try `help`)"))
    return 0
