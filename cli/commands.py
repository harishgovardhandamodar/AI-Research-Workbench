"""Implementation of every ``fox`` subcommand.

Each handler takes a parsed ``args`` namespace and prints directly to stdout
using the UI toolkit. Functions return an exit code (0 on success).
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .client import FoxClient, FoxClientError
from . import ui
from .ui import (Spinner, accent, bold, c, dim, err, hr, italic, keyval,
                 ok, panel, progress, run_with_spinner, table, warn)

from .splash import render_splash_panel

VERSION = "0.1.0"


# ---------------------------------------------------------------- helpers ----

def _client(args) -> FoxClient:
    return FoxClient(url=getattr(args, "url", None))


def _fmt_td(seconds: float | None) -> str:
    if not seconds:
        return "—"
    seconds = float(seconds)
    if seconds < 60:
        return f"{seconds:.0f}s"
    m, s = divmod(seconds, 60)
    if m < 60:
        return f"{int(m)}m{int(s):02d}s"
    h, m = divmod(m, 60)
    return f"{int(h)}h{int(m)}m"


def _status_color(status: str) -> str:
    s = str(status).lower()
    if s == "done" or s == "ok" or s == "success":
        return ok(status)
    if s == "error" or s == "failed":
        return err(status)
    if s in ("running", "pending", "queued", "building", "importing"):
        return accent(status)
    if s in ("warning", "degraded"):
        return warn(status)
    return c(status, ui.DIM)


def _job_poll(cli: FoxClient, job_id: str, label: str) -> int:
    """Poll a background job with a live progress display; returns exit code."""
    last = {}
    state = {"spinner": Spinner(label)}
    state["spinner"].start()
    try:
        import threading

        stop = threading.Event()

        def _render(job):
            st = job.get("status", "")
            if st == "running":
                last_detail = (job.get("log") or job.get("stage") or [])
                detail = ""
                if isinstance(last_detail, list) and last_detail:
                    detail = str(last_detail[-1])[:70]
                elif isinstance(last_detail, str):
                    detail = last_detail[:70]
                state["spinner"].label = f"{label} {c(detail, ui.DIM)}"
        # loop polls manually so we can update the spinner label
        waited = 0.0
        while waited < 1800:
            job = cli.job(job_id)
            last = job
            _render(job)
            if job.get("status") in ("done", "error"):
                break
            time.sleep(4)
            waited += 4
    finally:
        state["spinner"].stop()
    status = last.get("status")
    if status == "done":
        print(f"\n  {ok('job completed')} {c(job_id, ui.DIM)}")
        return 0
    print(f"\n  {err('job failed')} {c(job_id, ui.DIM)}")
    detail = last.get("error") or last.get("log")
    if detail:
        print(f"  {dim(str(detail)[:300])}")
    return 1


# ------------------------------------------------------------------ version --

def cmd_version(args) -> int:
    print(bold(f"fox {VERSION}", ui.ACCENT))
    print(dim("AI Research Workbench CLI"))
    return 0


# ---------------------------------------------------------------- splash ----
def cmd_splash(args) -> int:
    print(render_splash_panel(VERSION))
    return 0


# ----------------------------------------------------------------- status ----
def cmd_status(args) -> int:
    cli = _client(args)
    cfg = None
    server_up = True
    server_error = None
    try:
        cfg = run_with_spinner("contacting workbench", cli.config)
    except FoxClientError as e:
        server_up = False
        server_error = str(e)

    llm = cfg.get("llm", {}) if cfg else {}
    agent = cfg.get("agent", {}) if cfg else {}
    rows = []
    if server_up:
        rows.append(("server", ok("running") + dim(f"  {cli.url}")))
        rows.append(("model", c(llm.get("model", "?"), ui.TEXT)))
        rows.append(("base_url", dim(llm.get("base_url", "?"))))
        rows.append(("tool_base_url", dim(llm.get("tool_base_url", "?"))))
        rows.append(("max_iters", str(agent.get("max_iters", "?"))))
        rows.append(("reviewer", "on" if agent.get("reviewer_enabled", True) else "off"))

        # rkg scenario quick stats
        try:
            scenarios = cli.scenarios()
            n_running = sum(1 for s in scenarios
                            if _scenario_phase(s).lower() not in
                            ("done", "idle", "complete", "completed", ""))
            rows.append(("research scenarios", f"{len(scenarios)} total"
                         + (c(f", {n_running} active", ui.ACCENT) if n_running else "")))
        except FoxClientError:
            pass
    else:
        rows.append(("server", err("unreachable") + dim(f"  {cli.url}")))
        rows.append(("model", dim("unknown (server down)")))
        rows.append(("hint", dim("start it with  fox serve  or the web UI")))

    body = ui.kv_block(rows)
    out = panel("status", body)
    print(out)
    if server_error and not server_up:
        print(f"\n  {dim(str(server_error))}")
    return 0 if server_up else 1


# ----------------------------------------------------------------- doctor ----
def cmd_doctor(args) -> int:
    checks = []
    py = sys.version.split()[0]
    checks.append(("python", c(py, ui.TEXT), True))
    checks.append(("platform", dim(f"{platform.system()} {platform.machine()}"), True))

    # offline capabilities we can always verify
    venv = Path(sys.prefix).name
    checks.append(("venv", dim(venv), True))

    import importlib

    for mod in ("fastapi", "uvicorn", "openai", "numpy", "arxiv", "fitz"):
        try:
            m = importlib.import_module(mod)
            ver = getattr(m, "__version__", "?")
            checks.append((f"pkg:{mod}", c(str(ver), ui.TEXT), True))
        except Exception:  # noqa: BLE001
            checks.append((f"pkg:{mod}", err("missing"), False))

    # server reachability
    cli = _client(args)
    try:
        cli.health()
        checks.append(("server", ok("reachable") + dim(f" {cli.url}"), True))
    except FoxClientError as e:
        checks.append(("server", warn("offline") + dim(f" {cli.url}"), False))

    # repo state
    try:
        git_root = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                                  capture_output=True, text=True, timeout=5)
        ok_git = git_root.returncode == 0
        checks.append(("git repo", ok("yes") if ok_git else err("no"),
                       ok_git))
    except Exception:  # noqa: BLE001
        checks.append(("git repo", err("?"), False))

    rows = [(k, v) for k, v, _ in checks]
    print(panel("doctor", ui.kv_block(rows)))
    failures = sum(1 for _, _, okb in checks if not okb)
    if failures:
        print(f"\n  {warn(f'{failures} check(s) need attention')} "
              + dim("(start the server with `fox serve`)"))
        return 1
    print(f"\n  {ok('all checks passed')}")
    return 0


# ----------------------------------------------------------------- serve ----
def cmd_serve(args) -> int:
    """Launch the workbench server in the foreground (mirrors run.sh)."""
    host = getattr(args, "host", "127.0.0.1")
    port = getattr(args, "port", 8765)
    print(render_splash_panel(VERSION))
    print(f"\n  {accent('■')} {c('starting workbench', ui.TEXT)} "
          + dim(f"http://{host}:{port}"))
    print(dim("  ctrl-c to stop\n"))
    cmd = [sys.executable, "-m", "uvicorn", "backend.main:app",
           "--host", host, "--port", str(port)]
    return subprocess.call(cmd, cwd=os.environ.get("FOX_ROOT", str(Path.cwd())))


# --------------------------------------------------------------- projects ----
def cmd_projects(args) -> int:
    cli = _client(args)
    action = getattr(args, "action", "list")
    if action == "list":
        try:
            projects = run_with_spinner("listing projects", cli.projects)
        except FoxClientError as e:
            print(f"\n  {err(str(e))}")
            return 1
        if not projects:
            print(panel("projects", dim("no projects yet —  fox projects new <name>")))
            return 0
        headers = ["name", "runs", "experiments", "created"]
        rows = [[p.get("name", "?"), p.get("runs", 0), p.get("experiments", 0),
                 ui.fmt_time(p.get("created_at"))] for p in projects]
        print(panel("projects", table(headers, rows)))
        return 0
    name = args.project
    if action == "new":
        desc = getattr(args, "description", "")
        try:
            p = run_with_spinner(f"creating {name}", cli.create_project, name, desc)
        except FoxClientError as e:
            print(f"\n  {err(str(e))}")
            return 1
        print(f"\n  {ok(f'project `{name}` created')}")
        return 0
    if action == "rm":
        try:
            run_with_spinner(f"deleting {name}", cli.delete_project, name)
        except FoxClientError as e:
            print(f"\n  {err(str(e))}")
            return 1
        print(f"\n  {ok(f'project `{name}` deleted')}")
        return 0
    if action == "fork":
        target = args.target
        try:
            run_with_spinner(f"forking {name}", cli.fork_project, name, target)
        except FoxClientError as e:
            print(f"\n  {err(str(e))}")
            return 1
        print(f"\n  {ok(f'forked `{name}` → `{target}`')}")
        return 0
    if action == "show":
        try:
            p = run_with_spinner(f"loading {name}", cli.project, name)
        except FoxClientError as e:
            print(f"\n  {err(str(e))}")
            return 1
        body = ui.kv_block([("name", p.get("name", "?")),
                            ("description", dim(p.get("description", ""))),
                            ("runs", p.get("runs", 0)),
                            ("experiments", p.get("experiments", 0)),
                            ("created", ui.fmt_time(p.get("created_at")))])
        print(panel(f"project {name}", body))
        return 0
    print(f"\n  {err(f'unknown action `{action}`')}")
    return 1


# ------------------------------------------------------------------ runs -----
def cmd_runs(args) -> int:
    cli = _client(args)
    name = args.project
    try:
        runs = run_with_spinner(f"loading runs of {name}", cli.runs, name)
    except FoxClientError as e:
        print(f"\n  {err(str(e))}")
        return 1
    if not runs:
        print(panel(f"runs · {name}", dim("no runs yet")))
        return 0
    headers = ["id", "model", "status", "iters", "duration", "started"]
    rows = [[r.get("rid", r.get("id", "?")),
             str(r.get("model", "?"))[:22],
             _status_color(r.get("status", "?")),
             r.get("iterations", r.get("iters", "?")),
             _fmt_td(r.get("duration")),
             ui.fmt_time(r.get("started_at") or r.get("created_at"))]
            for r in runs[:20]]
    print(panel(f"runs · {name}", table(headers, rows)))
    return 0


# ------------------------------------------------------------ experiments ----
def cmd_experiments(args) -> int:
    cli = _client(args)
    name = args.project
    action = getattr(args, "action", "list")
    if action == "list":
        try:
            exps = run_with_spinner(f"loading experiments of {name}", cli.experiments, name)
        except FoxClientError as e:
            print(f"\n  {err(str(e))}")
            return 1
        if not exps:
            print(panel(f"experiments · {name}", dim("no experiments yet")))
            return 0
        headers = ["id", "status", "score", "started"]
        rows = [[e.get("id", "?"), _status_color(e.get("status", "?")),
                 e.get("best_score", e.get("score", "?")),
                 ui.fmt_time(e.get("started_at") or e.get("created_at"))]
                for e in exps[:20]]
        print(panel(f"experiments · {name}", table(headers, rows)))
        return 0
    if action == "start":
        try:
            e = run_with_spinner(f"starting experiment on {name}", cli.start_experiment, name)
        except FoxClientError as e2:
            print(f"\n  {err(str(e2))}")
            return 1
        print(f"\n  {ok('experiment started')}  {c(e.get('id', '?'), ui.DIM)}")
        return 0
    print(f"\n  {err(f'unknown action `{action}`')}")
    return 1


# ---------------------------------------------------------------- research ---
def _scenario_phase(s: dict) -> str:
    st = s.get("status")
    if isinstance(st, dict):
        return st.get("phase", st.get("phase_label", "?")) or "?"
    return str(st) if st is not None else "?"


def _phase_color(phase: str) -> str:
    p = str(phase).lower()
    if p in ("done", "complete", "completed", "idle"):
        return c(phase, ui.GREEN if "done" in p else ui.DIM)
    if p in ("running", "pending", "queued", "working"):
        return accent(phase)
    return c(phase, ui.DIM)


def cmd_research(args) -> int:
    cli = _client(args)
    action = getattr(args, "action", "list")
    if action == "list":
        try:
            scenarios = run_with_spinner("listing research scenarios", cli.scenarios)
        except FoxClientError as e:
            print(f"\n  {err(str(e))}")
            return 1
        if not scenarios:
            print(panel("research", dim("no scenarios yet")))
            return 0
        headers = ["id", "name", "phase", "papers", "score"]
        rows = []
        for s in scenarios:
            phase = _scenario_phase(s)
            rows.append([c(s.get("id", "?"), ui.DIM),
                         str(s.get("name", "?"))[:40],
                         _phase_color(phase),
                         len(s.get("corpus", []) or []),
                         s.get("best_score", s.get("report_score", "—"))])
        print(panel("research scenarios", table(headers, rows)))
        return 0
    sid = args.scenario
    if not sid:
        print(f"\n  {err('a scenario id is required')}  "
              + dim("(fox research list)"))
        return 2
    if action == "status":
        try:
            resp = run_with_spinner(f"status of {sid}", cli.scenario_status, sid)
        except FoxClientError as e:
            print(f"\n  {err(str(e))}")
            return 1
        s = resp.get("status", resp)
        phase = s.get("phase", "?")
        prog = s.get("progress")
        log = s.get("log") or []
        body = ui.kv_block([("id", sid),
                            ("phase", _phase_color(phase)),
                            ("progress", progress(prog) if prog is not None else "—"),
                            ("message", s.get("message", "—"))])
        if log:
            last = "\n".join(f"  {dim(str(l.get('msg', l)))}" for l in log[-3:])
            body += "\n\n  " + dim("recent:") + "\n" + last
        print(panel(f"research · {sid}", body))
        return 0
    if action == "report":
        try:
            rep = run_with_spinner(f"report of {sid}", cli.scenario_report, sid)
        except FoxClientError as e:
            print(f"\n  {err(str(e))}")
            return 1
        print(panel(f"report · {sid}", ""))
        print(rep if rep.strip() else dim("  (no report generated yet)"))
        return 0
    if action in ("build", "synthesize", "experiments", "loop"):
        try:
            job = run_with_spinner(f"submitting {action}", cli.scenario_action, sid, action)
        except FoxClientError as e:
            print(f"\n  {err(str(e))}")
            return 1
        job_id = job.get("id")
        if not job_id:
            print(f"\n  {err(job.get('error', 'no job id returned'))}")
            return 1
        print(f"\n  {accent('●')} {bold(action, ui.TEXT)} on {c(sid, ui.DIM)} "
              + dim(f"(job {job_id})"))
        return _job_poll(cli, job_id, f"{action} · {sid}")
    print(f"\n  {err(f'unknown action `{action}`')}")
    return 1


# ------------------------------------------------------------------- graph ---
def cmd_graph(args) -> int:
    cli = _client(args)
    try:
        stats = run_with_spinner("loading knowledge graph", cli.rkg_stats)
    except FoxClientError as e:
        print(f"\n  {err(str(e))}")
        return 1
    rows = [("papers", stats.get("papers", "?")),
            ("concepts", stats.get("concepts", "?")),
            ("relations", stats.get("relations", "?"))]
    rag = stats.get("rag") or {}
    if rag:
        rows.append(("rag chunks", f"{rag.get('chunks', 0)} "
                                    f"({rag.get('dimension', '?')}d)"))
    gpu = stats.get("gpu") or {}
    if gpu:
        gpu_ok = gpu.get("available")
        name = (gpu.get("devices") or [{}])[0].get("name", "GPU")
        rows.append(("gpu", (ok(name) if gpu_ok else dim(name))
                     + dim(f"  {gpu.get('count', '?')} device(s)")))
    print(panel("knowledge graph", ui.kv_block(rows)))
    return 0


# ------------------------------------------------------------------ papers ---
def cmd_papers(args) -> int:
    cli = _client(args)
    try:
        papers = run_with_spinner("loading papers", cli.rkg_papers)
    except FoxClientError as e:
        print(f"\n  {err(str(e))}")
        return 1
    if not papers:
        print(panel("papers", dim("no papers ingested")))
        return 0
    headers = ["id", "title", "year", "concepts"]
    rows = [[str(p.get("id", "?"))[:16], str(p.get("title", "?"))[:44],
             p.get("year", "—"),
             len(p.get("concepts", []) or [])]
            for p in papers[:30]]
    print(panel(f"papers ({len(papers)})", table(headers, rows)))
    return 0


# ----------------------------------------------------------------- manual ----
def cmd_manual(args) -> int:
    from .manual import MANUAL

    topic = getattr(args, "topic", None)
    if topic:
        text = MANUAL.section(topic)
    else:
        text = MANUAL.text
    print(text)
    return 0


# ------------------------------------------------------------ interactive ----
def cmd_interactive(args) -> int:
    """A tiny opencode-style REPL: no args → splash + `>` prompt."""
    from .interactive import run_repl

    return run_repl(args)
