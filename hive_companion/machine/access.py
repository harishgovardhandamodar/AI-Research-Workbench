"""Access control for 3-tier workflow folders.

Tiers for every workflow (hive/machine/workflows/<name>/ and workspace/<name>/):
  1. local-only     — no copies, no commits, no websearch, no web LLM, sensitive datasets, private reports, full logs
  2. core-workflow  — web allowed, commits allowed, logs except private datapoints (redacted)
  3. public         — all can be committed, public

Enforced via check_access() in tools and workflows.
"""

from __future__ import annotations

from pathlib import Path

TIERS = ("local-only", "core-workflow", "public")
TIER_ORDER = {t: i for i, t in enumerate(TIERS)}

# What each tier allows
ALLOW = {
    "local-only": {
        "web": False,          # no web_search, web_fetch, web LLM
        "commit": False,       # no git commit/push, no copies out
        "copy_out": False,
        "log_sensitive": True, # logs keep sensitive entries (not redacted)
        "llm_web": False,
    },
    "core-workflow": {
        "web": True,
        "commit": True,        # allowed to commit to repo
        "copy_out": True,
        "log_sensitive": False, # logs except private datapoints (redacted)
        "llm_web": True,
    },
    "public": {
        "web": True,
        "commit": True,
        "copy_out": True,
        "log_sensitive": False,
        "llm_web": True,
    },
}

# Tools that are considered web
WEB_TOOLS = {"web_search", "web_fetch"}
# Tools/commands that imply commit/copy
COMMIT_TOOLS = {"run_bash", "run_python"}  # checked via args content
COMMIT_PATTERNS = ["git commit", "git push", "gh repo", "git add", "commit -m"]

def check_access(tool: str, access: str, args: dict | None = None) -> None:
    """Raise PermissionError if tool not allowed for access tier."""
    access = access or "core-workflow"
    if access not in ALLOW:
        raise ValueError(f"unknown access tier: {access}")
    allow = ALLOW[access]
    args = args or {}

    if tool in WEB_TOOLS and not allow["web"]:
        raise PermissionError(f"tool '{tool}' not allowed in '{access}' (no web)")

    # llm with web: if prompt contains web-like intent and tier is local-only, block
    if tool == "llm_chat" and not allow["llm_web"]:
        prompt = str(args.get("prompt", "")).lower()
        if any(k in prompt for k in ["web_search", "web_fetch", "http", "fetch url"]):
            raise PermissionError(f"LLM with web not allowed in '{access}'")

    # commit/copy checks for run_bash/run_python
    if tool in COMMIT_TOOLS and not allow["commit"]:
        cmd = str(args.get("cmd", "") + args.get("code", ""))
        if any(p in cmd for p in COMMIT_PATTERNS):
            raise PermissionError(f"commit/copy not allowed in '{access}' (no repo commits)")
        # also block cp out of local-only
        if not allow["copy_out"] and ("cp " in cmd and "local-only" in cmd):
            raise PermissionError(f"copy out not allowed in '{access}'")

def folder_for(access: str) -> str:
    if access not in TIERS:
        raise ValueError(access)
    return access

def ensure_workflow_dirs(base: Path) -> dict[str, Path]:
    """Create 3-tier folder structure under base Path. Returns dict tier->Path."""
    out = {}
    for tier in TIERS:
        p = base / tier
        (p / "datasets").mkdir(parents=True, exist_ok=True) if tier == "local-only" else None
        (p / "reports").mkdir(parents=True, exist_ok=True)
        (p / "artifacts").mkdir(parents=True, exist_ok=True)
        (p / "logs").mkdir(parents=True, exist_ok=True)
        if tier == "local-only":
            # ensure .gitignore to prevent commits
            gi = p / ".gitignore"
            if not gi.exists():
                gi.write_text("# local-only: never commit\n*\n!.gitignore\n", encoding="utf-8")
        else:
            # core-workflow and public have README to allow commits
            readme = p / "README.md"
            if not readme.exists():
                readme.write_text(f"# {tier}\n\nThis folder is `{tier}` — {'web allowed, commits allowed (redacted logs)' if tier=='core-workflow' else 'public, all can be committed'}.\n", encoding="utf-8")
        out[tier] = p
    return out

def is_sensitive_path(path: str) -> bool:
    return "local-only" in Path(path).parts
