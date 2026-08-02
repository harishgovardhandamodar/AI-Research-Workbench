"""Shared path constants."""

from __future__ import annotations

from pathlib import Path

# Repository root (parent of this backend package).
ROOT = Path(__file__).resolve().parent.parent

# Where projects, artifacts and config live.
WORKBENCH_DIR = ROOT / "workbench"
PROJECTS_DIR = WORKBENCH_DIR / "projects"
CONFIG_PATH = WORKBENCH_DIR / "config.json"
FRONTEND_DIR = ROOT / "frontend"
