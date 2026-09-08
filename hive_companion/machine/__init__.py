"""Hive-Machine — local Perplexity Computer clone.

Sandboxed computer for the local LLM: files, code exec, web, terminal.
Workspace: ~/.hive/machine/workspace (jailed, no escape).
All LLM calls via Ollama / LM Studio only.
"""

from pathlib import Path
from hive.config import CONFIG_DIR

MACHINE_DIR = CONFIG_DIR / "machine"
WORKSPACE = MACHINE_DIR / "workspace"
HISTORY_DB = MACHINE_DIR / "machine.db"
AUDIT_DB = MACHINE_DIR / "audit.db"
WORKFLOWS_DIR = MACHINE_DIR / "workflows"

# 3-tier per-workflow roots (local-only / core-workflow / public)
# e.g. WORKSPACE/<workflow>/local-only/datasets , WORKFLOWS_DIR/<name>/core-workflow/reports
WORKFLOW_TIERS = ("local-only", "core-workflow", "public")

def workflow_base(name: str) -> Path:
    return WORKSPACE / name

def workflow_dirs(name: str) -> dict[str, Path]:
    from hive.machine.access import ensure_workflow_dirs
    base = workflow_base(name)
    # also ensure definition dir
    def_base = WORKFLOWS_DIR / name
    ensure_workflow_dirs(def_base)
    return ensure_workflow_dirs(base)

__all__ = ["MACHINE_DIR", "WORKSPACE", "HISTORY_DB", "AUDIT_DB", "WORKFLOWS_DIR", "WORKFLOW_TIERS", "workflow_base", "workflow_dirs"]
