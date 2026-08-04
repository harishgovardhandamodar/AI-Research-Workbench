"""Built-in GitHub/git MCP server for the Fox workbench.

Exposes git/GitHub operations (status, log, commit, push, pull, managed repos)
over the Model Context Protocol so the agent can version experiment work, commit
into the experiment management repo (see Settings -> Experiment management), or
drive any local git worktree.

Auth: uses the host's git configuration (SSH keys / credential helpers). The
management repo path is injected via the FOX_MGMT_REPO env var when the server is
spawned by the workbench.

Run it standalone (stdio):

    .venv/bin/python mcp_servers/github_tools.py
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

mcp = MCPServer("fox-github-tools", version="0.1.0")
RO = ToolAnnotations(read_only_hint=True)

ROOT = Path(__file__).resolve().parent.parent
GIT_TIMEOUT = 60


def _default_repo() -> str:
    return (os.environ.get("FOX_MGMT_REPO") or os.environ.get("FOX_GITHUB_REPO") or "").strip()


def _resolve(repo):
    repo = str(repo or _default_repo() or "").strip()
    if not repo:
        return None, "no repo given and FOX_MGMT_REPO / FOX_GITHUB_REPO not set"
    p = Path(repo).expanduser().resolve()
    if not p.is_dir():
        return None, f"repo directory not found: {repo}"
    return p, None


def _git(repo: Path, *args: str) -> tuple[int, str]:
    try:
        proc = subprocess.run(["git", "-C", str(repo), *args],
                              capture_output=True, text=True, timeout=GIT_TIMEOUT)
    except FileNotFoundError:
        return 127, "git not found on PATH"
    except subprocess.TimeoutExpired:
        return 124, "git timed out"
    out = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode, out.strip()


@mcp.tool(annotations=RO)
def github_managed_repos() -> str:
    """List sibling git repos next to the workbench (candidates for the
    experiment management repo)."""
    lines = []
    try:
        for d in sorted(ROOT.parent.iterdir()):
            if d.is_dir() and not d.name.startswith(".") and (d / ".git").exists():
                branch = _git(d, "rev-parse", "--abbrev-ref", "HEAD")[1].strip()
                lines.append(f"{d.name} — {d} — branch {branch or '?'}")
    except OSError:
        pass
    return "\n".join(lines) if lines else "no sibling git repos found"


@mcp.tool(annotations=RO)
def github_status(repo: str = "") -> str:
    """Show git status (branch + changed files + last commit) of a repo.
    Defaults to the experiment management repo."""
    p, err = _resolve(repo)
    if p is None:
        return err
    branch = _git(p, "rev-parse", "--abbrev-ref", "HEAD")[1] or "?"
    code, st = _git(p, "status", "--short")
    code, last = _git(p, "log", "--oneline", "-1")
    return (f"repo: {p}\nbranch: {branch}\nlast: {last or '(no commits)'}\n"
            f"--- status ---\n{st or '(clean)'}")


@mcp.tool(annotations=RO)
def github_log(repo: str = "", n: int = 5) -> str:
    """Show the last n commits of a repo (defaults to the management repo)."""
    p, err = _resolve(repo)
    if p is None:
        return err
    code, out = _git(p, "log", "--oneline", f"-{max(1, int(n))}")
    return out or "(no commits yet)"


@mcp.tool()
def github_commit(repo: str = "", message: str = "", paths: list[str] | None = None) -> str:
    """Stage and commit changes in a git repo. `paths` are repo-relative (default:
    everything). Returns the commit outcome. Writable — approval required."""
    if not (message or "").strip():
        return "error: a commit message is required"
    p, err = _resolve(repo)
    if p is None:
        return err
    add_paths = paths if paths else ["."]
    code, out = _git(p, "add", "--", *add_paths)
    if code != 0:
        return f"git add failed: {out}"
    code, out = _git(p, "commit", "-m", message.strip(), "--no-gpg-sign")
    if code != 0:
        if "nothing to commit" in out:
            return "nothing to commit — working tree clean"
        return f"git commit failed: {out}"
    return f"committed: {out.splitlines()[-1] if out else message.strip()}"


@mcp.tool()
def github_push(repo: str = "", branch: str = "") -> str:
    """Push the current branch to its remote. Uses the host's SSH/credential
    setup. Writable — approval required."""
    p, err = _resolve(repo)
    if p is None:
        return err
    args = ["push"]
    if branch:
        args.append(branch)
    code, out = _git(p, *args)
    if code != 0:
        code2, out2 = _git(p, "push", "-u", "origin", "HEAD")
        if code2 != 0:
            return f"push failed: {out2 or out}"
        return f"pushed (set upstream): {out2.splitlines()[-1]}"
    return f"pushed: {out.splitlines()[-1] if out else 'ok'}"


@mcp.tool()
def github_pull(repo: str = "") -> str:
    """Pull the current branch from its remote. Writable — approval required."""
    p, err = _resolve(repo)
    if p is None:
        return err
    code, out = _git(p, "pull", "--ff-only")
    if code != 0:
        return f"pull failed: {out}"
    return out.splitlines()[-1] if out else "up to date"


@mcp.tool()
def github_commit_and_push(repo: str = "", message: str = "", paths: list[str] | None = None) -> str:
    """Commit and push in one step. Writable — approval required."""
    p, err = _resolve(repo)
    if p is None:
        return err
    if not (message or "").strip():
        return "error: a commit message is required"
    res = github_commit(repo=p, message=message, paths=paths)
    if not res.startswith("committed"):
        return res
    code, out = _git(p, "push")
    if code != 0:
        code2, out2 = _git(p, "push", "-u", "origin", "HEAD")
        if code2 != 0:
            return f"{res}; push failed: {out2 or out}"
    return res + " (pushed)"


if __name__ == "__main__":
    mcp.run(transport="stdio")
