"""Implementation of every ``fox`` subcommand.

Each handler takes a parsed ``args`` namespace and prints either a styled panel
(human) or raw JSON (``--json``, for scripting). Handlers return an exit code
(0 on success). Debug tracing goes to stderr via :mod:`cli.log`.
"""

from __future__ import annotations

import json
import os
import platform
import re
import subprocess
import sys
import time
from pathlib import Path

from .client import FoxClient, FoxClientError
from . import ui
from .log import log as _log
from .ui import (Spinner, accent, bold, c, dim, err, hr, italic, keyval,
                 ok, panel, progress, run_with_spinner, table, warn)

from .splash import render_splash_panel

VERSION = "0.1.0"


# ---------------------------------------------------------------- helpers ----

def _client(args) -> FoxClient:
    return FoxClient(url=getattr(args, "url", None))


def _want_json(args) -> bool:
    return bool(getattr(args, "json", False))


def _fail(args, msg: str, code: int = 1) -> int:
    """Report an error: JSON object in ``--json`` mode, styled line otherwise."""
    if _want_json(args):
        print(json.dumps({"error": msg}, default=str))
    else:
        print(f"\n  {err(msg)}")
    return code


def _emit(args, data) -> int:
    if _want_json(args):
        print(json.dumps(data, indent=2, default=str, ensure_ascii=False))
    return 0


def _spinner(args, label: str, fn, *a, **kw):
    """Run ``fn`` with a spinner unless ``--json``/``--quiet``."""
    if _want_json(args) or getattr(args, "quiet", False):
        return fn(*a, **kw)
    return run_with_spinner(label, fn, *a, **kw)


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


def _best_metric(run: dict) -> str:
    """Render a run's most relevant metric (goal metric first, else first)."""
    metrics = run.get("metrics") or {}
    if not metrics:
        return "—"
    gm = (run.get("config") or {}).get("goal_metric", "")
    key = gm if gm in metrics else next(iter(metrics))
    val = metrics[key]
    if isinstance(val, (int, float)):
        val = f"{val:.4g}"
    return f"{key.replace('_', ' ')}={val}"


def _job_poll(args, cli: FoxClient, job_id: str, label: str) -> int:
    """Poll a background job; returns exit code and, in JSON mode, emits it."""
    if _want_json(args) or getattr(args, "quiet", False):
        job = cli.wait_job(job_id)
        if _want_json(args):
            _emit(args, job)
        elif job.get("status") != "done":
            print(f"\n  {err('job failed')} {c(job_id, ui.DIM)}")
            detail = job.get("error") or job.get("log")
            if detail:
                print(f"  {dim(str(detail)[:300])}")
        return 0 if job.get("status") == "done" else 1
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


def _submit_job(args, cli: FoxClient, label: str, fn, *a, **kw) -> dict:
    """Submit a background job; returns the finished job view."""
    job = _spinner(args, f"submitting {label}", fn, *a, **kw)
    job_id = job.get("id")
    if not job_id:
        raise FoxClientError(400, job.get("error", "no job id returned"),
                             label)
    return _spinner(args, f"waiting for {label}",
                    cli.wait_job, job_id)


def _require(args, *names: str) -> bool:
    for n in names:
        if not getattr(args, n, None):
            print(f"\n  {err('missing argument')}  "
                  + dim(f"({n} required)"))
            return False
    return True


# ------------------------------------------------------------------ version --

def cmd_version(args) -> int:
    if _want_json(args):
        return _emit(args, {"name": "fox", "version": VERSION})
    print(bold(f"fox {VERSION}", ui.ACCENT))
    print(dim("AI Research Workbench CLI"))
    return 0


# ---------------------------------------------------------------- splash ----
def cmd_splash(args) -> int:
    if _want_json(args):
        return _emit(args, {"name": "fox", "version": VERSION})
    print(render_splash_panel(VERSION))
    return 0


# ----------------------------------------------------------------- status ----
def cmd_status(args) -> int:
    _log.debug("status url={}", getattr(args, "url", None))
    cli = _client(args)
    cfg = None
    server_up = True
    server_error = None
    try:
        cfg = _spinner(args, "contacting workbench", cli.config)
    except FoxClientError as e:
        server_up = False
        server_error = str(e)
    if _want_json(args):
        return _emit(args, {"server_up": server_up,
                            "error": server_error,
                            "config": cfg})

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
    _log.debug("doctor url={}", getattr(args, "url", None))
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

    if _want_json(args):
        return _emit(args, [{"check": k, "value": ui.strip_ansi(v),
                             "ok": o} for k, v, o in checks])
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
    _log.debug("serve host={} port={}", host, port)
    if _want_json(args):
        return _emit(args, {"host": host, "port": port})
    print(render_splash_panel(VERSION))
    print(f"\n  {accent('■')} {c('starting workbench', ui.TEXT)} "
          + dim(f"http://{host}:{port}"))
    print(dim("  ctrl-c to stop\n"))
    cmd = [sys.executable, "-m", "uvicorn", "backend.main:app",
           "--host", host, "--port", str(port)]
    return subprocess.call(cmd, cwd=os.environ.get("FOX_ROOT", str(Path.cwd())))


# --------------------------------------------------------------- projects ----
def cmd_projects(args) -> int:
    _log.debug("projects action={} project={}", getattr(args, "action", "list"),
               getattr(args, "project", None))
    cli = _client(args)
    action = getattr(args, "action", "list")
    if action == "list":
        try:
            projects = _spinner(args, "listing projects", cli.projects)
        except FoxClientError as e:
            return _fail(args, str(e))
        if _want_json(args):
            return _emit(args, projects)
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
            p = _spinner(args, f"creating {name}", cli.create_project, name, desc)
        except FoxClientError as e:
            return _fail(args, str(e))
        if _want_json(args):
            return _emit(args, p)
        print(f"\n  {ok(f'project `{name}` created')}")
        return 0
    if action == "rm":
        try:
            _spinner(args, f"deleting {name}", cli.delete_project, name)
        except FoxClientError as e:
            return _fail(args, str(e))
        if _want_json(args):
            return _emit(args, {"deleted": name})
        print(f"\n  {ok(f'project `{name}` deleted')}")
        return 0
    if action == "fork":
        target = args.target
        try:
            _spinner(args, f"forking {name}", cli.fork_project, name, target)
        except FoxClientError as e:
            return _fail(args, str(e))
        if _want_json(args):
            return _emit(args, {"forked": name, "target": target})
        print(f"\n  {ok(f'forked `{name}` → `{target}`')}")
        return 0
    if action == "show":
        try:
            p = _spinner(args, f"loading {name}", cli.project, name)
        except FoxClientError as e:
            return _fail(args, str(e))
        if _want_json(args):
            return _emit(args, p)
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
    _log.debug("runs project={}", getattr(args, "project", None))
    cli = _client(args)
    name = args.project
    try:
        runs = _spinner(args, f"loading runs of {name}", cli.runs, name)
    except FoxClientError as e:
            return _fail(args, str(e))
    if _want_json(args):
        return _emit(args, runs)
    if not runs:
        print(panel(f"runs · {name}", dim("no runs yet")))
        return 0
    headers = ["id", "label", "status", "metric", "started"]
    rows = [[r.get("rid", r.get("id", "?")),
             str(r.get("label") or r.get("kind", "?"))[:22],
             _status_color(r.get("status", "?")),
             _best_metric(r)[:26],
             ui.fmt_time(r.get("started_at") or r.get("created_at"))]
            for r in runs[:20]]
    print(panel(f"runs · {name}", table(headers, rows)))
    return 0


# ------------------------------------------------------------ experiments ----
def cmd_experiments(args) -> int:
    _log.debug("experiments project={} action={}",
               getattr(args, "project", None), getattr(args, "action", "list"))
    cli = _client(args)
    name = args.project
    action = getattr(args, "action", "list")
    if action == "list":
        try:
            exps = _spinner(args, f"loading experiments of {name}",
                            cli.experiments, name)
        except FoxClientError as e:
            return _fail(args, str(e))
        if _want_json(args):
            return _emit(args, exps)
        if not exps:
            print(panel(f"experiments · {name}", dim("no experiments yet")))
            return 0
        headers = ["id", "name", "status", "score", "started"]
        rows = [[e.get("id", "?"),
                 str(e.get("name", "?"))[:32],
                 _status_color(e.get("status", "?")),
                 e.get("best_score", e.get("score", "?")),
                 ui.fmt_time(e.get("started_at") or e.get("created_at"))]
                for e in exps[:20]]
        print(panel(f"experiments · {name}", table(headers, rows)))
        return 0
    if action == "start":
        try:
            e = _spinner(
                args, f"starting experiment on {name}",
                cli.start_experiment, name,
                getattr(args, "exp_name", None) or getattr(args, "name", None),
                getattr(args, "hypothesis", "") or "",
                getattr(args, "goal_metric", "") or "",
                getattr(args, "goal_target", None),
                getattr(args, "plan", "") or "")
        except FoxClientError as e2:
            return _fail(args, str(e2))
        exp = e.get("experiment") or e
        if _want_json(args):
            return _emit(args, exp)
        print(f"\n  {ok('experiment started')}  "
              + c(f"{exp.get('name', '?')} #{exp.get('id', '?')}", ui.DIM))
        return 0
    if action == "run-obfuscation":
        n_rows = int(getattr(args, "n_rows", 2000) or 2000)
        seed = int(getattr(args, "seed", 42) or 42)
        try:
            resp = _spinner(
                args, f"running bank obfuscation on {name}",
                cli.run_obfuscation, name, n_rows, seed)
        except FoxClientError as e3:
            return _fail(args, str(e3))
        if _want_json(args):
            return _emit(args, resp)
        exp = resp.get("experiment", {})
        runs = resp.get("runs", [])
        headers = ["scenario", "technique", "metric", "value"]
        rows = []
        for r in runs:
            metrics = r.get("metrics") or {}
            mkey = next(iter(metrics), "")
            mval = metrics.get(mkey) if mkey else ""
            mstr = (f"{mval:.2g}" if isinstance(mval, (int, float))
                    else str(mval))
            rows.append([str((r.get("label") or r.get("id")))[:42],
                         str((r.get("config") or {}).get("technique", ""))[:34],
                         mkey.replace("_", " ") if mkey else "—",
                         mstr])
        print(panel(f"obfuscation (bank) · {name}",
                    table(headers, rows)))
        print(dim(f"\n  experiment id {c(exp.get('id', '?'), ui.DIM)} · "
                  f"{len(runs)} scenario runs recorded"))
        print(dim(f"  view in the app: open the {c(name, ui.DIM)} project → "
                  f"Experiments → 'obfuscation (bank)'"))
        return 0
    print(f"\n  {err(f'unknown action `{action}`')}")
    return 1


# ------------------------------------------------------------------ run -----
def cmd_run(args) -> int:
    """Inspect a single run, or generate its lab-notebook report."""
    _log.debug("run project={} rid={} report={}", getattr(args, "project", None),
               getattr(args, "rid", None), bool(getattr(args, "report", False)))
    if not _require(args, "project", "rid"):
        return 2
    cli = _client(args)
    name = args.project
    rid = args.rid
    if getattr(args, "report", False) or getattr(args, "action", "show") == "report":
        try:
            rep = _spinner(args, f"report of run {rid}",
                           cli.run_report, name, rid)
        except FoxClientError as e:
            return _fail(args, str(e))
        if _want_json(args):
            return _emit(args, rep)
        print(panel(f"run report · {name} #{rid}", ""))
        print((rep.get("report") or "").strip() or dim("  (no report)"))
        return 0
    try:
        run = _spinner(args, f"loading run {rid}", cli.run, name, rid)
    except FoxClientError as e:
            return _fail(args, str(e))
    if _want_json(args):
        return _emit(args, run)
    metrics = run.get("metrics") or {}
    review = run.get("review") or {}
    findings = review.get("findings") or []
    rows = [("id", run.get("id", "?")),
            ("label", run.get("label") or run.get("kind", "?")),
            ("status", _status_color(run.get("status", "?"))),
            ("experiment", run.get("experiment_id") or "—"),
            ("started", ui.fmt_time(run.get("started_at"))),
            ("finished", ui.fmt_time(run.get("finished_at"))),
            ("duration", _fmt_td((run.get("finished_at") or 0)
                                 - (run.get("started_at") or 0)
                                 if run.get("finished_at") else None))]
    if metrics:
        rows.append(("metrics", ", ".join(
            f"{k.replace('_', ' ')}={v:.4g}"
            if isinstance(v, (int, float)) else f"{k}={v}"
            for k, v in sorted(metrics.items()))[:80]))
    body = ui.kv_block(rows)
    prompt = run.get("prompt")
    if prompt:
        body += "\n\n  " + dim("prompt:") + f" {prompt[:300]}"
    if findings:
        body += "\n\n  " + dim("review findings:") + "\n" + "\n".join(
            f"  - {dim(f.get('severity', ''))}: {f.get('message', '')}"
            for f in findings[:5])
    arts = run.get("artifact_ids") or []
    if arts:
        body += "\n\n  " + dim(f"artifacts: {', '.join(map(str, arts[:8]))}")
    print(panel(f"run · {name} #{rid}", body))
    print(dim(f"  tip: `fox run {name} {rid} report` for the lab-notebook report"))
    return 0


# ------------------------------------------------------------- experiment ----
def cmd_experiment(args) -> int:
    """Inspect an experiment, or its ranking leaderboard."""
    _log.debug("experiment project={} eid={} ranking={}",
               getattr(args, "project", None), getattr(args, "eid", None),
               bool(getattr(args, "ranking", False)))
    if not _require(args, "project", "eid"):
        return 2
    cli = _client(args)
    name = args.project
    eid = args.eid
    if getattr(args, "ranking", False) or getattr(args, "action", "show") == "ranking":
        try:
            data = _spinner(args, f"ranking {eid}",
                            cli.experiment_ranking, name, eid,
                            getattr(args, "metric", "") or "")
        except FoxClientError as e:
            return _fail(args, str(e))
        if _want_json(args):
            return _emit(args, data)
        ranking = data.get("ranking") or {}
        exp = data.get("experiment") or {}
        rows = ranking.get("rows") or []
        if not rows:
            print(panel(f"ranking · {name} #{eid}",
                        dim("no ranked runs (goal metric? "
                            "`fox experiment <p> <eid> ranking --metric m`)")))
            return 0
        headers = ["rank", "run", "label", "metric", "Δ best", "% best"]
        table_rows = [[r.get("rank", "?"), r.get("run_id", "?"),
                       str(r.get("label") or "?")[:24],
                       f"{r.get('metric', 0):.4g}" if isinstance(
                           r.get("metric"), (int, float)) else r.get("metric", "—"),
                       f"{r.get('delta_best', 0):+.4g}" if isinstance(
                           r.get("delta_best"), (int, float)) else "—",
                       f"{r.get('pct_best', 0):.1f}%" if isinstance(
                           r.get("pct_best"), (int, float)) else "—"]
                      for r in rows]
        body = table(headers, table_rows)
        if ranking.get("metric"):
            body += "\n\n  " + dim(f"metric: {ranking.get('metric')} · "
                                   f"best: {ranking.get('best'):.4g} · "
                                   f"higher_better: {ranking.get('higher_better')}")
        print(panel(f"ranking · {name} #{eid} · {exp.get('name', '?')}", body))
        return 0
    try:
        data = _spinner(args, f"loading experiment {eid}",
                        cli.experiment, name, eid)
    except FoxClientError as e:
            return _fail(args, str(e))
    exp = data.get("experiment") or {}
    if _want_json(args):
        return _emit(args, exp)
    runs = exp.get("runs") or []
    body = ui.kv_block([
        ("id", exp.get("id", "?")),
        ("name", exp.get("name", "?")),
        ("status", _status_color(exp.get("status", "?"))),
        ("goal_metric", exp.get("goal_metric") or "—"),
        ("goal_target", exp.get("goal_target") or "—"),
        ("higher_better", str(bool(exp.get("higher_better", True)))),
        ("created", ui.fmt_time(exp.get("created_at"))),
    ])
    if exp.get("hypothesis"):
        body += "\n\n  " + dim("hypothesis:") + f" {exp['hypothesis']}"
    if exp.get("plan"):
        body += "\n\n  " + dim("plan:") + f" {exp['plan']}"
    if runs:
        headers = ["id", "label", "status", "metric"]
        table_rows = [[r.get("id", "?"),
                       str(r.get("label") or r.get("kind", "?"))[:24],
                       _status_color(r.get("status", "?")),
                       _best_metric(r)[:30]]
                      for r in runs[:20]]
        body += "\n\n" + table(headers, table_rows)
    print(panel(f"experiment · {name} #{eid}", body))
    print(dim(f"  tip: `fox experiment {name} {eid} ranking` for the leaderboard"))
    return 0


# --------------------------------------------------------------- compare ----
def cmd_compare(args) -> int:
    """Metric delta between two runs of a project."""
    _log.debug("compare project={} a={} b={}", getattr(args, "project", None),
               getattr(args, "run_a", None), getattr(args, "run_b", None))
    if not _require(args, "project", "run_a", "run_b"):
        return 2
    cli = _client(args)
    try:
        cmp = _spinner(args, f"comparing {args.run_a} vs {args.run_b}",
                       cli.compare, args.project, args.run_a, args.run_b)
    except FoxClientError as e:
            return _fail(args, str(e))
    if _want_json(args):
        return _emit(args, cmp)
    rows = cmp.get("rows") or []
    if not rows:
        print(panel(f"compare · {args.project}",
                    dim("no shared numeric metrics between the two runs")))
        return 0
    headers = ["metric", args.run_a, args.run_b, "Δ", "%"]
    table_rows = [[r.get("metric", "?"),
                   f"{r.get('a', 0):.4g}" if isinstance(r.get("a"), (int, float)) else "—",
                   f"{r.get('b', 0):.4g}" if isinstance(r.get("b"), (int, float)) else "—",
                   f"{r.get('delta', 0):+.4g}" if isinstance(r.get("delta"), (int, float)) else "—",
                   f"{r.get('pct', 0):+.1f}%" if isinstance(r.get("pct"), (int, float)) else "—"]
                  for r in rows]
    body = table(headers, table_rows)
    s = cmp.get("summary") or {}
    if s:
        body += "\n\n  " + dim(f"shared {s.get('shared', 0)} · "
                               f"increased {s.get('increased', 0)} · "
                               f"decreased {s.get('decreased', 0)} · "
                               f"unchanged {s.get('unchanged', 0)}")
    print(panel(f"compare · {args.project}", body))
    return 0


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
    _log.debug("research action={} scenario={}", getattr(args, "action", "list"),
               getattr(args, "scenario", None))
    cli = _client(args)
    action = getattr(args, "action", "list")
    if action == "list":
        try:
            scenarios = _spinner(args, "listing research scenarios", cli.scenarios)
        except FoxClientError as e:
            return _fail(args, str(e))
        if _want_json(args):
            return _emit(args, scenarios)
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
            resp = _spinner(args, f"status of {sid}",
                            cli.scenario_status, sid)
        except FoxClientError as e:
            return _fail(args, str(e))
        if _want_json(args):
            return _emit(args, resp)
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
            rep = _spinner(args, f"report of {sid}", cli.scenario_report, sid)
        except FoxClientError as e:
            return _fail(args, str(e))
        if _want_json(args):
            return _emit(args, {"scenario": sid, "report": rep})
        print(panel(f"report · {sid}", ""))
        print(rep if rep.strip() else dim("  (no report generated yet)"))
        return 0
    if action in ("build", "synthesize", "experiments", "loop"):
        try:
            job = _spinner(args, f"submitting {action}",
                           cli.scenario_action, sid, action)
        except FoxClientError as e:
            return _fail(args, str(e))
        job_id = job.get("id")
        if not job_id:
            print(f"\n  {err(job.get('error', 'no job id returned'))}")
            return 1
        if not _want_json(args):
            print(f"\n  {accent('●')} {bold(action, ui.TEXT)} on {c(sid, ui.DIM)} "
                  + dim(f"(job {job_id})"))
        return _job_poll(args, cli, job_id, f"{action} · {sid}")
    print(f"\n  {err(f'unknown action `{action}`')}")
    return 1


# ------------------------------------------------------------------- graph ---
def cmd_graph(args) -> int:
    _log.debug("graph url={}", getattr(args, "url", None))
    cli = _client(args)
    try:
        stats = _spinner(args, "loading knowledge graph", cli.rkg_stats)
    except FoxClientError as e:
            return _fail(args, str(e))
    if _want_json(args):
        return _emit(args, stats)
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
_ARXIV_ID = re.compile(r"^(?:arXiv:)?\d{4}\.\d{4,5}(?:v\d+)?$")


def _dispatch_paper_add(args, cli: FoxClient, ref: str) -> dict:
    """Route an arXiv id / URL / free-text query to the right RKG job."""
    if ref.startswith(("http://", "https://")):
        _log.debug("paper add url={}", ref)
        return _submit_job(args, cli, "web add",
                           cli.rkg_web_add, ref)
    if _ARXIV_ID.match(ref.strip()):
        _log.debug("paper add arxiv={}", ref)
        return _submit_job(args, cli, "pool import",
                           cli.rkg_pool_import, ref)
    _log.debug("paper add query={}", ref)
    return _submit_job(args, cli, "import", cli.rkg_import, ref)


def cmd_papers(args) -> int:
    _log.debug("papers action={} query={}", getattr(args, "action", "list"),
               getattr(args, "query", None))
    cli = _client(args)
    action = getattr(args, "action", "list")
    if action == "search":
        if not _require(args, "query"):
            return 2
        try:
            papers = _spinner(args, f"searching '{args.query}'",
                              cli.rkg_papers_search, args.query)
        except FoxClientError as e:
            return _fail(args, str(e))
        if _want_json(args):
            return _emit(args, papers)
        if not papers:
            print(panel("papers search", dim("no matches")))
            return 0
        headers = ["id", "title", "year", "authors"]
        rows = [[str(p.get("id", "?"))[:18],
                 str(p.get("title", "?"))[:44],
                 p.get("published", p.get("year", "—")),
                 str(p.get("authors", ""))[:26]]
                for p in papers[:30]]
        print(panel(f"papers search · {args.query}", table(headers, rows)))
        return 0
    if action == "add":
        if not _require(args, "query"):
            return 2
        try:
            job = _dispatch_paper_add(args, cli, args.query)
        except FoxClientError as e:
            return _fail(args, str(e))
        return _emit_job_result(args, job, "paper ingest")
    try:
        papers = _spinner(args, "loading papers", cli.rkg_papers)
    except FoxClientError as e:
            return _fail(args, str(e))
    if _want_json(args):
        return _emit(args, papers)
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


def _emit_job_result(args, job: dict, label: str) -> int:
    """Render a finished RKG job (add/import) as JSON or a result line."""
    if _want_json(args):
        return _emit(args, job)
    if job.get("status") != "done":
        print(f"\n  {err(f'{label} failed')}")
        if job.get("error"):
            print(f"  {dim(str(job['error'])[:300])}")
        return 1
    result = job.get("result")
    if isinstance(result, list):
        print(f"\n  {ok(f'{label} complete')} "
              + dim(f"({job.get('id')})"))
        for r in result:
            st = r.get("status", "?")
            line = f"  {ok('✓') if st == 'added' else dim(st)} " \
                   f"{r.get('paper_id', '?')}"
            if r.get("concepts"):
                line += dim(f" · {len(r['concepts'])} concepts")
            print(line)
        return 0
    if isinstance(result, dict):
        status = result.get("status")
        if status == "error":
            msg = result.get("message", "")
            print(f"\n  {err(label + ' failed')}  {dim(str(msg))}")
            return 1
        print(f"\n  {ok(f'{label} complete')} "
              + dim(f"({job.get('id')})"))
        print(f"  {ok('✓') if status == 'added' else dim(status)} "
              + f"{result.get('paper_id') or result.get('node_id') or ''}"
              + dim(f" · {result.get('concepts', '')} concepts" if result.get("concepts") else ""))
        return 0
    print(f"\n  {ok(f'{label} complete')}  {dim(str(job.get('id', '?')))}")
    return 0


# ------------------------------------------------------------------- jobs ----
def cmd_jobs(args) -> int:
    _log.debug("jobs job_id={}", getattr(args, "job_id", None))
    cli = _client(args)
    if getattr(args, "job_id", None):
        try:
            job = _spinner(args, f"job {args.job_id}", cli.job, args.job_id)
        except FoxClientError as e:
            return _fail(args, str(e))
        if _want_json(args):
            return _emit(args, job)
        body = ui.kv_block([
            ("id", job.get("id", "?")),
            ("kind", job.get("kind", "?")),
            ("label", job.get("label", "?")),
            ("status", _status_color(job.get("status", "?"))),
            ("started", ui.fmt_time(job.get("started_at"))),
            ("finished", ui.fmt_time(job.get("finished_at"))),
        ])
        if job.get("error"):
            body += "\n\n  " + dim(f"error: {str(job['error'])[:300]}")
        print(panel(f"job · {job.get('id', '?')}", body))
        return 0
    try:
        jobs = _spinner(args, "listing jobs", cli.rkg_jobs)
    except FoxClientError as e:
            return _fail(args, str(e))
    if _want_json(args):
        return _emit(args, jobs)
    if not jobs:
        print(panel("jobs", dim("no jobs yet")))
        return 0
    headers = ["id", "kind", "status", "label", "finished"]
    rows = [[j.get("id", "?")[:12], j.get("kind", "?")[:14],
             _status_color(j.get("status", "?")),
             str(j.get("label", "?"))[:36],
             ui.fmt_time(j.get("finished_at") or j.get("started_at"))]
            for j in jobs[:30]]
    print(panel(f"jobs ({len(jobs)})", table(headers, rows)))
    return 0


# ------------------------------------------------------------- scheduler ----
def cmd_scheduler(args) -> int:
    _log.debug("scheduler url={}", getattr(args, "url", None))
    cli = _client(args)
    try:
        st = _spinner(args, "scheduler status", cli.rkg_scheduler_status)
    except FoxClientError as e:
            return _fail(args, str(e))
    if _want_json(args):
        return _emit(args, st)
    rows = [("enabled", "on" if st.get("enabled") else "off"),
            ("check every", f"{st.get('configured_check_minutes', '?')} min"),
            ("synthesize", "on" if st.get("synthesize") else "off"),
            ("active", "running" if st.get("active") else "idle")]
    due = st.get("due_scenarios") or []
    if due:
        rows.append(("due", ", ".join(str(d) for d in due[:10])))
    print(panel("research scheduler", ui.kv_block(rows)))
    return 0


# -------------------------------------------------------------------- pool ----
def cmd_pool(args) -> int:
    _log.debug("pool action={} name={} query={} arxiv={}",
               getattr(args, "action", "list"), getattr(args, "name", None),
               getattr(args, "query", None), getattr(args, "arxiv_id", None))
    cli = _client(args)
    action = getattr(args, "action", "list")
    if action == "topics":
        try:
            topics = _spinner(args, "pool topics", cli.rkg_pool_topics)
        except FoxClientError as e:
            return _fail(args, str(e))
        if _want_json(args):
            return _emit(args, topics)
        if not topics:
            print(panel("pool topics", dim("no topics — "
                                           "`fox pool topics add <name> <query>`")))
            return 0
        headers = ["topic", "query"]
        rows = [[t.get("name", "?"), str(t.get("query", ""))[:60]]
                for t in topics]
        print(panel("pool topics", table(headers, rows)))
        return 0
    if action == "topics-add":
        if not _require(args, "name", "query"):
            return 2
        try:
            _spinner(args, f"adding topic {args.name}",
                     cli.rkg_pool_topic_add, args.name, args.query)
        except FoxClientError as e:
            return _fail(args, str(e))
        if _want_json(args):
            return _emit(args, {"added": args.name})
        print(f"\n  {ok(f'topic `{args.name}` added')}")
        return 0
    if action == "topics-rm":
        if not _require(args, "name"):
            return 2
        try:
            _spinner(args, f"removing topic {args.name}",
                     cli.rkg_pool_topic_remove, args.name)
        except FoxClientError as e:
            return _fail(args, str(e))
        if _want_json(args):
            return _emit(args, {"removed": args.name})
        print(f"\n  {ok(f'topic `{args.name}` removed')}")
        return 0
    if action == "import":
        if not _require(args, "arxiv_id"):
            return 2
        try:
            job = _submit_job(args, cli, "pool import",
                              cli.rkg_pool_import, args.arxiv_id)
        except FoxClientError as e:
            return _fail(args, str(e))
        return _emit_job_result(args, job, "pool import")
    try:
        pool = _spinner(args, "loading pool", cli.rkg_pool)
    except FoxClientError as e:
            return _fail(args, str(e))
    if _want_json(args):
        return _emit(args, pool)
    if not pool:
        print(panel("research pool", dim("pool empty — "
                                         "`fox pool topics add <name> <query>`")))
        return 0
    rows = []
    total = 0
    for topic, papers in pool.items():
        n = len(papers or [])
        total += n
        rows.append([str(topic)[:36], str(n)])
    print(panel(f"research pool ({total} papers)",
                table(["topic", "papers"], rows)))
    return 0


# ------------------------------------------------------------- management ----
def cmd_manage(args) -> int:
    _log.debug("manage action={} project={}", getattr(args, "action", "status"),
               getattr(args, "project", None))
    cli = _client(args)
    action = getattr(args, "action", "status")
    if action == "repos":
        try:
            repos = _spinner(args, "management repos", cli.mgmt_repos)
        except FoxClientError as e:
            return _fail(args, str(e))
        if _want_json(args):
            return _emit(args, repos)
        if not repos:
            print(panel("management repos", dim("no sibling repos found")))
            return 0
        headers = ["name", "branch", "path"]
        rows = [[r.get("name", "?"), r.get("branch", "?"),
                 str(r.get("path", ""))[:56]] for r in repos]
        print(panel("management repos", table(headers, rows)))
        return 0
    if action == "status":
        try:
            st = _spinner(args, "management status", cli.mgmt_status)
        except FoxClientError as e:
            return _fail(args, str(e))
        if _want_json(args):
            return _emit(args, st)
        rows = [("repo_dir", dim(st.get("repo_dir") or "—")),
                ("github_repo", st.get("github_repo") or "—"),
                ("remote", st.get("remote") or "—")]
        print(panel("management", ui.kv_block(rows)))
        return 0
    if action == "link":
        if not _require(args, "github_repo"):
            return 2
        try:
            st = _spinner(args, "linking github repo",
                          cli.mgmt_link, args.github_repo)
        except FoxClientError as e:
            return _fail(args, str(e))
        if _want_json(args):
            return _emit(args, st)
        if st.get("ok"):
            print(f"\n  {ok('github repo linked')}  {c(st.get('remote', ''), ui.DIM)}")
            return 0
        print(f"\n  {warn('not linked')}  {dim(st.get('message', ''))}")
        return 1
    if action in ("commit", "push", "commit-and-push"):
        if not _require(args, "project"):
            return 2
        name = args.project
        msg = getattr(args, "message", "") or ""
        try:
            if action == "commit":
                st = _spinner(args, f"committing {name}",
                              cli.mgmt_commit, name, msg)
            elif action == "push":
                st = _spinner(args, f"pushing {name}", cli.mgmt_push, name)
            else:
                st = _spinner(args, f"commit+push {name}",
                              cli.mgmt_commit_and_push, name, msg)
        except FoxClientError as e:
            return _fail(args, str(e))
        if _want_json(args):
            return _emit(args, st)
        if st.get("ok"):
            commit = st.get("commit") or (st.get("pushed") or {}).get("commit")
            line = f"  {ok(action + ' ok')}"
            if commit:
                line += dim(f"  {str(commit)[:10]}")
            print(line)
            if st.get("message"):
                print(f"  {dim(st['message'])}")
            return 0
        print(f"\n  {err(action + ' failed')}  {dim(st.get('message', ''))}")
        return 1
    print(f"\n  {err(f'unknown action `{action}`')}")
    return 1


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
    """No args → the opencode-style terminal window (falls back to `>` shell)."""
    from .tui import run_tui

    return run_tui(args)
