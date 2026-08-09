"""Hyperparameter sweep launch (round 29).

A UI-friendly companion to the agent's `run_sweep` tool: takes a code snippet
that reads `config` and calls `report_metric`, plus either an explicit list of
config dicts or a *grid* of parameter values (expanded to the cartesian product),
and produces a deterministic launch plan the chat/intent layer executes via the
existing parallel-kernel sweep machinery.
"""

from __future__ import annotations

import itertools
import json

from typing import Any


def expand_sweep_grid(grid: dict[str, list] | None,
                      configs: list[dict] | None = None) -> list[dict]:
    """Normalize a sweep config set.

    - If explicit `configs` are given, return a copy of them.
    - Otherwise expand a `grid` of {param: [value, ...]} into the cartesian
      product (one config per combination), ignoring any empty grids.
    - Falls back to a single empty config so a bare code run still works.
    """
    if configs:
        out = []
        for c in configs:
            out.append(dict(c) if isinstance(c, dict) else {})
        return out
    if grid:
        pairs = [(k, list(v)) for k, v in grid.items()
                 if v is not None and len(v) > 0]
        if pairs:
            keys = [k for k, _ in pairs]
            return [dict(zip(keys, combo))
                    for combo in itertools.product(*(v for _, v in pairs))]
    return [{}]


def sweep_configs_json(grid: dict[str, list] | None,
                       configs: list[dict] | None = None) -> str:
    """Compact JSON of the normalized sweep configs (for labels / display)."""
    return json.dumps(expand_sweep_grid(grid, configs), separators=(",", ":"))


def sweep_label_prefix(label_prefix: str = "", n: int = 0) -> str:
    """A label prefix for a UI-launched sweep."""
    lp = (label_prefix or "").strip()
    return lp or (f"sweep·{n}" if n else "sweep")


def validate_sweep_request(code: str, configs: list[dict]) -> str:
    """Return an error message, or '' if the sweep request is launchable."""
    if not (code or "").strip():
        return "Sweep code is required."
    if not configs:
        return "Sweep needs at least one config (or a non-empty grid)."
    if not ("config" in code and "report_metric" in code):
        # Not fatal — the code might still work — but warn the user loudly.
        return ("Hint: sweep code should read its parameters from a `config` dict "
                "and report the metric(s) via report_metric(name, value).")
    return ""


def suggest_grid_from_config(config: dict | None) -> dict[str, list]:
    """Derive a sweep grid from a single example config (best-effort): pick the
    numeric scalars the researcher most likely wants to sweep."""
    if not config:
        return {}
    keys = [k for k, v in config.items()
            if isinstance(v, (int, float)) and not isinstance(v, bool)
            and str(k) not in ("seed", "random_state", "n_jobs", "verbose")]
    out: dict[str, list] = {}
    for k in keys[:4]:
        v = config[k]
        span = max(abs(v) * 0.1, 1e-6)
        out[k] = [round(v - span, 6), v, round(v + span, 6)]
    return out


# Re-exported helpers so the intent layer and tests share one code path.
__all__ = [
    "expand_sweep_grid", "sweep_configs_json", "sweep_label_prefix",
    "validate_sweep_request", "suggest_grid_from_config",
]
