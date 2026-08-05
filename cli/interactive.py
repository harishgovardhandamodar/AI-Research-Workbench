"""Interactive shell: `fox` with no arguments.

Shows the animated splash, then drops into a `>` prompt (opencode-style).
Every prompt command reuses the normal subcommand handlers, so behaviour is
identical to the one-shot invocations. Tab completes commands and their
actions; global flags (``--json``/``--debug``/``--quiet``/``--url``) work here
too.
"""

from __future__ import annotations

import shlex
from types import SimpleNamespace

from . import commands, ui
from .log import log as _log
from .ui import accent, bold, c, dim, err, ok

HELP = """\
  Commands
  ────────
  status               workbench / model overview
  doctor               environment check
  projects             list projects          (or: projects new <name> …)
  runs <project>       agent runs
  run <p> <id>         run detail              (… report)
  experiments <proj>   list experiments       (… start, run-obfuscation)
  experiment <p> <id>  experiment detail       (… ranking)
  compare <p> <a> <b>  metric delta two runs
  research             list scenarios         (or: research status <id> …)
  graph                knowledge graph stats
  papers               latest papers          (or: papers search <q> | add <ref>)
  jobs                 background jobs        (or: jobs <id>)
  scheduler            research scheduler status
  pool                 research pool          (or: pool topics, pool import <id>)
  manage               mgmt repo              (status | repos | link | commit …)
  serve                launch the server
  tui                  open the full-screen terminal window
  manual               open the manual        (or: manual <section>)
  help                 this help
  exit | quit          leave the shell

  flags: --json (machine output) · --debug (stderr tracing) · --quiet
"""

# --------------------------------------------------------------- completion --

_COMMANDS = sorted({
    "status", "doctor", "projects", "runs", "run", "experiments",
    "experiment", "compare", "research", "graph", "papers", "jobs",
    "scheduler", "pool", "manage", "serve", "tui", "manual", "splash",
    "version", "help", "exit",
})
_ACTIONS = {
    "projects": ["list", "new", "show", "rm", "fork"],
    "run": ["show", "report"],
    "experiments": ["list", "start", "run-obfuscation"],
    "experiment": ["show", "ranking"],
    "research": ["list", "status", "report", "build", "synthesize",
                 "experiments", "loop"],
    "manage": ["repos", "status", "link", "commit", "push", "commit-and-push"],
    "papers": ["list", "search", "add"],
    "pool": ["list", "topics", "topics-add", "topics-rm", "import"],
}
_FLAGS = ["--json", "--quiet", "--debug", "--url", "--name", "--hypothesis",
          "--goal-metric", "--goal-target", "--plan", "--metric",
          "--n-rows", "--seed", "--message"]


def _install_completion() -> None:
    import readline  # noqa: F401  (history / arrow keys when available)

    def completer(text: str, state: int) -> str | None:
        try:
            line = readline.get_line_buffer()
            tokens = shlex.split(line)
            head = tokens[0].lower() if tokens else ""
            prefix = line[:readline.get_begidx()]
            candidates: list[str] = []
            if not tokens:
                candidates = _COMMANDS
            elif line.endswith(" ") or not line.strip():
                candidates = _COMMANDS
            elif len(tokens) == 1 and not line.endswith(" "):
                candidates = [w for w in _COMMANDS if w.startswith(text)]
            elif head in _ACTIONS:
                candidates = [a for a in _ACTIONS[head] if a.startswith(text)]
                candidates += [f for f in _FLAGS if f.startswith(text)]
            else:
                candidates = [f for f in _FLAGS if f.startswith(text)]
            hits = [w for w in candidates if w.startswith(text)]
            return hits[state] if state < len(hits) else None
        except Exception:  # noqa: BLE001
            return None

    try:
        readline.set_completer(completer)
        readline.parse_and_bind("tab: complete")
        readline.set_completer_delims(" \t\n")
    except Exception:  # noqa: BLE001
        pass


# ------------------------------------------------------------------- parser --

_BOOL_FLAGS = ("--json", "--quiet", "--debug")


def _repl_args(argv: str) -> SimpleNamespace:
    """Build an argparse-like namespace from a raw REPL command line."""
    args = SimpleNamespace(
        url=None, project=None, scenario=None, action=None, target=None,
        description="", name=None, topic=None, host="127.0.0.1", port=8765,
        exp_name=None, hypothesis="", goal_metric="", goal_target=None,
        plan="", rid=None, eid=None, run_a=None, run_b=None, metric="",
        query=None, message="", github_repo=None, arxiv_id=None,
        job_id=None, json=False, quiet=False, debug=False)
    tokens = shlex.split(argv)
    if not tokens:
        return args
    cmd = tokens[0]
    rest = tokens[1:]

    # consume global/known flags into the namespace
    positional: list[str] = []
    i = 0
    while i < len(rest):
        t = rest[i]
        if t in _BOOL_FLAGS:
            setattr(args, t.lstrip("-").replace("-", "_"), True)
            i += 1
            continue
        value_flags = {
            "--url": "url", "--name": "exp_name", "-n": "exp_name",
            "--hypothesis": "hypothesis", "--goal-metric": "goal_metric",
            "--goal-target": "goal_target", "--plan": "plan",
            "--metric": "metric", "--message": "message",
            "-m": "message", "--n-rows": "n_rows", "--seed": "seed",
            "-d": "description", "--description": "description",
            "--desc": "description",
        }
        if t in value_flags and i + 1 < len(rest):
            attr = value_flags[t]
            val = rest[i + 1]
            if attr == "goal_target":
                try:
                    val = float(val)
                except ValueError:
                    val = None
            elif attr in ("n_rows", "seed"):
                try:
                    val = int(val)
                except ValueError:
                    val = None
            if val is not None:
                setattr(args, attr, val)
            i += 2
            continue
        positional.append(t)
        i += 1

    acts = _ACTIONS.get(cmd)
    if acts is not None:
        for t in positional:
            if t in acts and args.action is None:
                args.action = t
                positional.remove(t)
                break

    if cmd == "projects":
        args.project = positional[0] if positional else None
        args.action = args.action or "list"
        if args.action == "new" and len(positional) > 1:
            args.project = positional[0]
        if args.action == "fork" and len(positional) > 1:
            args.target = positional[1]
    elif cmd == "runs":
        args.project = positional[0] if positional else None
    elif cmd == "run":
        args.project = positional[0] if len(positional) > 0 else None
        args.rid = positional[1] if len(positional) > 1 else None
        args.action = args.action or "show"
    elif cmd == "experiments":
        args.project = positional[0] if positional else None
        args.action = args.action or "list"
    elif cmd == "experiment":
        args.project = positional[0] if len(positional) > 0 else None
        args.eid = positional[1] if len(positional) > 1 else None
        args.action = args.action or "show"
    elif cmd == "compare":
        args.project = positional[0] if len(positional) > 0 else None
        args.run_a = positional[1] if len(positional) > 1 else None
        args.run_b = positional[2] if len(positional) > 2 else None
    elif cmd == "research":
        args.scenario = positional[0] if positional else None
        args.action = args.action or "list"
    elif cmd == "manage":
        args.action = args.action or "status"
        if args.action in ("commit", "push", "commit-and-push"):
            args.project = positional[0] if positional else None
        elif args.action == "link":
            args.github_repo = positional[0] if positional else None
    elif cmd == "papers":
        args.query = positional[0] if positional else None
        args.action = args.action or "list"
    elif cmd == "pool":
        args.action = args.action or "list"
        if args.action in ("topics-add",):
            args.name = positional[0] if len(positional) > 0 else None
            args.query = positional[1] if len(positional) > 1 else None
        elif args.action == "topics-rm":
            args.name = positional[0] if positional else None
        elif args.action == "import":
            args.arxiv_id = positional[0] if positional else None
    elif cmd == "jobs":
        args.job_id = positional[0] if positional else None
    if cmd == "manual" and rest:
        args.topic = rest[0]
    if cmd == "scheduler":
        args.action = "status"
    if getattr(args, "debug", False):
        _log.set_level("DEBUG")
    return args


# ----------------------------------------------------------------------- run --

def run_repl(args) -> int:
    try:
        import readline  # noqa: F401  (history / arrow keys when available)
        _install_completion()
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
    if cmd in ("exit", "quit", "q"):
        print(f"  {dim('bye')}")
        return -1
    if cmd in ("help", "h", "?"):
        print(HELP)
        return 0
    if cmd == "manual":
        return commands.cmd_manual(_repl_args(line))
    if cmd == "splash":
        return commands.cmd_splash(SimpleNamespace(url=None))
    if cmd == "version":
        return commands.cmd_version(SimpleNamespace(url=None))
    if cmd == "tui":
        return commands.cmd_interactive(SimpleNamespace(url=None))
    if cmd == "scheduler":
        return commands.cmd_scheduler(_repl_args(line))
    if cmd in ("status", "doctor", "serve", "projects", "runs", "run",
               "experiments", "experiment", "compare", "research", "graph",
               "papers", "jobs", "pool", "manage"):
        handler = {
            "status": commands.cmd_status,
            "doctor": commands.cmd_doctor,
            "serve": commands.cmd_serve,
            "projects": commands.cmd_projects,
            "runs": commands.cmd_runs,
            "run": commands.cmd_run,
            "experiments": commands.cmd_experiments,
            "experiment": commands.cmd_experiment,
            "compare": commands.cmd_compare,
            "research": commands.cmd_research,
            "graph": commands.cmd_graph,
            "papers": commands.cmd_papers,
            "jobs": commands.cmd_jobs,
            "pool": commands.cmd_pool,
            "manage": commands.cmd_manage,
        }[cmd]
        args = _repl_args(line)
        if cmd == "projects":
            args.action = args.action or "list"
        if cmd == "research":
            args.action = args.action or "list"
        return handler(args)
    print(f"  {err(f'unknown command `{cmd}`')}  "
          + dim("(try `help`)"))
    return 0
