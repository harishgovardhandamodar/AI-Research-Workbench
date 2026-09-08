"""Plugins package — ledger audit integration all over workbench.

Each plugin is auto-discovered via `backend/plugins/*.py` and can hook into
workbench lifecycle via `register()`.

Currently: `ledger_audit` — hash-chained ledger for every CLI, research workflow,
machine tool, and web action (gathering invariant).
"""

from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path


def load_plugins():
    """Auto-discover and load all plugins in this package."""
    plugins = []
    for _, name, _ in pkgutil.iter_modules([str(Path(__file__).parent)]):
        if name.startswith("_"):
            continue
        try:
            mod = importlib.import_module(f"backend.plugins.{name}")
            if hasattr(mod, "register"):
                plugins.append(mod)
        except Exception:
            continue
    return plugins
