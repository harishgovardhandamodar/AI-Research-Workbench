"""Experiment management repo: export Fox experiments + artifacts into a sibling
git repo (e.g. ``personal-experiments``) and auto-commit during experiment runs.

The management repo is configured under ``config.management.repo_dir``. Each
project snapshots into ``<repo>/fox/<project>/``:

    experiments.json   all experiments + their runs (metrics / config / review)
    runs/<id>.json     per-run records
    artifacts/         figure/table/data artifacts produced by runs
    data/              imported datasets (e.g. Kaggle imports)

Auto-commit is best-effort and non-blocking: it runs off the event loop, never
touches files outside ``<repo>/fox/``, and only stages that subtree (so unrelated
changes already in the management repo are left alone).
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from pathlib import Path

from .state import CONFIG

GIT_TIMEOUT = 60


def management_repo_dir() -> Path | None:
    raw = (CONFIG.get("management") or {}).get("repo_dir") or ""
    raw = raw.strip()
    if not raw:
        return None
    p = Path(raw).expanduser()
    return p.resolve() if p.exists() else p


def autocommit_enabled() -> bool:
    return bool((CONFIG.get("management") or {}).get("auto_commit", True))


def autopush_enabled() -> bool:
    return bool((CONFIG.get("management") or {}).get("auto_push", False))


def sibling_git_repos() -> list[dict]:
    """Discover sibling git repos (next to the workbench repo) to offer in the
    settings tab as the experiment management repo."""
    from .paths import ROOT

    out: list[dict] = []
    try:
        base = Path(ROOT).resolve().parent
        for d in sorted(base.iterdir()):
            if not d.is_dir() or d.name.startswith("."):
                continue
            if (d / ".git").exists():
                out.append({"name": d.name, "path": str(d.resolve()),
                            "branch": _git(d, "rev-parse", "--abbrev-ref", "HEAD")[1].strip()})
    except OSError:
        pass
    return out


def _git(repo: Path | str, *args: str) -> tuple[int, str]:
    cmd = ["git", "-C", str(repo), *args]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=GIT_TIMEOUT)
    except FileNotFoundError:
        return 127, "git not found on PATH"
    except subprocess.TimeoutExpired:
        return 124, "git timed out"
    out = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode, out.strip()


def ensure_repo(repo: Path) -> tuple[bool, str]:
    """Make sure `repo` is an existing git work tree (else git init it)."""
    if not repo.exists():
        try:
            repo.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return False, f"cannot create {repo}: {e}"
    if not (repo / ".git").exists():
        code, out = _git(repo, "init", "-b", "main")
        if code != 0:
            code, out = _git(repo, "init")  # older git
        if code != 0:
            return False, f"git init failed: {out}"
    return True, "ok"


def _write_if_changed(path: Path, content: str) -> bool:
    """Write only when content differs, so unchanged snapshots don't dirty the
    worktree (and don't produce no-op commits)."""
    try:
        if path.exists() and path.read_text() == content:
            return False
    except OSError:
        pass
    path.write_text(content)
    return True


def _experiments_payload(rt) -> list[dict]:
    """Store reads happen on the event loop (SQLite connections are
    thread-bound); the returned payload is what gets snapshotted."""
    out = []
    for exp in rt.store.list_experiments():
        e = dict(exp)
        e["runs"] = rt.store.experiment_runs(e["id"], limit=200)
        out.append(e)
    return out


def snapshot_project(rt, repo: Path, experiments: list[dict] | None = None) -> list[str]:
    """Write this project's experiments + artifacts under <repo>/fox/<project>/.

    Returns the list of project-relative paths written (for the commit)."""
    base = repo / "fox" / rt.name
    base.mkdir(parents=True, exist_ok=True)

    payload = {"project": rt.name, "experiments": experiments or _experiments_payload(rt)}
    written: list[str] = []
    if _write_if_changed(base / "experiments.json", json.dumps(payload, indent=2)):
        written.append(f"fox/{rt.name}/experiments.json")

    run_dir = base / "runs"
    run_dir.mkdir(parents=True, exist_ok=True)
    for exp in payload["experiments"]:
        for run in exp["runs"]:
            target = run_dir / f"{run['id']}.json"
            if _write_if_changed(target, json.dumps(run, indent=2, default=str)):
                written.append(f"fox/{rt.name}/runs/{run['id']}.json")

    for rel in ("artifacts", "data"):
        src = rt.dir / rel
        if src.is_dir():
            try:
                shutil.copytree(src, base / rel, dirs_exist_ok=True)
                written.append(f"fox/{rt.name}/{rel}")
            except OSError:
                pass
    return written


def _commit_message(rt, run: dict) -> str:
    exp = None
    eid = run.get("experiment_id")
    if eid is not None:
        try:
            exp = rt.store.get_experiment(eid)
        except Exception:  # noqa: BLE001
            exp = None
    name = f"experiment {exp['name']!r}" if exp else "experiment run"
    rid = run.get("id")
    suffix = ""
    metrics = run.get("metrics") or {}
    if exp and exp.get("goal_metric") and exp["goal_metric"] in metrics:
        try:
            suffix = f" — {exp['goal_metric']}={float(metrics[exp['goal_metric']]):.4g}"
        except (TypeError, ValueError):
            suffix = ""
    return f"{name} run #{rid}{suffix}"


def commit_message(rt, run: dict) -> str:
    """Public alias (reads the store; call from the event loop)."""
    return _commit_message(rt, run)


def autocommit(rt, run: dict, experiments: list[dict] | None = None,
               message: str | None = None) -> dict:
    """Sync snapshot + commit (+ optional push) of a just-recorded experiment run.

    Store reads are expected to be precomputed (experiments/message) when called
    from a worker thread; falls back to reading the store when not. Best-effort:
    returns {"ok": bool, "message": str}; never raises."""
    try:
        repo = management_repo_dir()
        if repo is None:
            return {"ok": False, "message": "no management repo configured"}
        ok, msg = ensure_repo(repo)
        if not ok:
            return {"ok": False, "message": msg}
        snapshot_project(rt, repo, experiments)
        rel = "fox/"
        code, out = _git(repo, "add", "--", rel)
        if code != 0:
            return {"ok": False, "message": f"git add failed: {out}"}
        msg = message or _commit_message(rt, run)
        code, out = _git(repo, "commit", "-m", msg, "--no-gpg-sign")
        if code not in (0, 1):
            return {"ok": False, "message": f"git commit failed: {out}"}
        if "nothing to commit" in out:
            return {"ok": True, "message": "no changes to commit"}
        if autopush_enabled():
            code, out = _git(repo, "push")
            if code != 0:
                code, out = _git(repo, "push", "-u", "origin", "HEAD")
                if code != 0:
                    return {"ok": False, "message": f"committed but push failed: {out}"}
        return {"ok": True, "message": msg}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "message": f"{type(e).__name__}: {e}"}


# A per-project in-flight guard so a burst of rapid runs doesn't stack git jobs.
_inflight: set[str] = set()


async def maybe_autocommit(rt, run: dict) -> None:
    """Schedule a background auto-commit (no-op if disabled / not an experiment
    run / a commit for this project is already in flight)."""
    if not autocommit_enabled():
        return
    if not run.get("experiment_id"):
        return
    if rt.name in _inflight:
        return
    _inflight.add(rt.name)
    try:
        # Precompute store reads on the event loop; git/file work runs in a
        # worker thread so it can't block the chat.
        experiments = _experiments_payload(rt)
        message = _commit_message(rt, run)
        await asyncio.to_thread(autocommit, rt, run, experiments, message)
    except Exception:  # noqa: BLE001
        pass
    finally:
        _inflight.discard(rt.name)
