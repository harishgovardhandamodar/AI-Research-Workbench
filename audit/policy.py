"""Policy & permission tracking.

A tiny risk-tiering engine over tool names / MCP servers / path patterns plus
a persisted allow/deny policy store. It also exposes the *risk tier* of a tool
call and keeps a running "declared vs observed" permission view so the
dashboard can flag drift (granted-but-unused, used-but-not-granted).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .models import POLICY_ALLOW, POLICY_DENY, POLICY_OVERRIDE, PolicyRule

DEFAULT_RISK_TIERS: dict[str, str] = {
    "low": {
        "tools": {"run_python", "list_kernel_variables", "save_artifact",
                  "rkg__query_rag", "rkg__paper_notes", "rkg__scenario_status",
                  "rkg__scenario_report", "editor__list_files", "editor__read_file",
                  "editor__open", "create_notebook"},
        "prefixes": [],
    },
    "medium": {
        "tools": {"run_notebook", "create_experiment", "start_run", "finish_run"},
        "prefixes": ["science__", "graphrag__", "privacy__assess",
                     "privacy__detect", "arxiv__query"],
    },
    "high": {
        "tools": {"run_r", "editor__edit_file", "privacy__apply_laplace_dp",
                  "privacy__apply_gaussian_dp", "arxiv__ingest_arxiv_paper",
                  "arxiv__extract_paper_text"},
        "prefixes": ["robustness__", "github__", "privacy__reidentif",
                     "privacy__redteam", "privacy__generate_synthetic"],
    },
    "critical": {
        "tools": {"run_shell"},
        "prefixes": [],
    },
}

HIGH_RISK_TOOLS = {"run_shell", "run_r", "editor__edit_file"}


def risk_tier_for(tool_name: str | None, server: str | None = None,
                  custom: dict | None = None) -> str:
    """Classify a tool call into low/medium/high/critical.

    Uses the default tiers merged with any custom overrides from the workbench
    config. Exact tool membership wins first; otherwise the most *specific*
    (longest) matching prefix decides, so a broad ``privacy__`` critical rule
    never shadows a narrower ``privacy__generate_synthetic`` high rule.
    """
    tiers = DEFAULT_RISK_TIERS
    if custom:
        merged: dict[str, dict] = {}
        for tier, spec in DEFAULT_RISK_TIERS.items():
            merged[tier] = {
                "tools": set(spec["tools"]) | set((custom.get(tier) or {}).get("tools", [])),
                "prefixes": list(spec["prefixes"]) + list((custom.get(tier) or {}).get("prefixes", [])),
            }
        tiers = merged
    name = tool_name or ""
    order = ["critical", "high", "medium", "low"]
    for tier in order:
        if name in tiers[tier]["tools"]:
            return tier
    best_prefix, best_tier = "", "low"
    for tier in order:
        for prefix in tiers[tier]["prefixes"]:
            if name.startswith(prefix) and len(prefix) > len(best_prefix):
                best_prefix, best_tier = prefix, tier
    if best_tier != "low":
        return best_tier
    if server:
        return risk_tier_for(f"{server}__*")
    return "low"


def severity_for_tier(tier: str) -> str:
    return {"low": "info", "medium": "info", "high": "warning",
            "critical": "critical"}.get(tier, "info")


class PolicyEngine:
    """Persisted allow/deny rules + a runtime decision helper."""

    def __init__(self, store=None):
        self.store = store  # optional object exposing get_rule/set_rule/list_rules

    def decide(self, key: str, pattern: str, default: str = POLICY_ALLOW) -> dict:
        rule = self.store.get_rule(key, pattern) if self.store else None
        if rule is not None:
            return {"outcome": rule["decision"], "rule": rule["key"],
                    "pattern": rule["pattern"], "risk_tier": rule["risk_tier"],
                    "reason": rule["reason"]}
        return {"outcome": default, "rule": None, "pattern": pattern,
                "risk_tier": risk_tier_for(pattern), "reason": ""}


@dataclass
class ObservedPermission:
    key: str
    pattern: str
    decision: str = POLICY_ALLOW
    usage_count: int = 0
    last_used: float | None = None
    overrides: int = 0
    granted_at: float | None = None
    risk_tier: str = "medium"

    def to_public(self) -> dict:
        return {
            "key": self.key, "pattern": self.pattern, "decision": self.decision,
            "usage_count": self.usage_count, "last_used": self.last_used,
            "overrides": self.overrides, "granted_at": self.granted_at,
            "risk_tier": self.risk_tier,
        }


class PermissionTracker:
    """In-memory view of declared vs observed permissions per agent.

    The dashboard merges this with the store's ``audit_events`` policy
    decisions to surface drift warnings (used-but-not-granted / granted-but-
    unused).
    """

    def __init__(self):
        self._perms: dict[str, ObservedPermission] = {}

    def observe(self, key: str, pattern: str, decision: str, tier: str = "medium",
                now: float | None = None):
        import time
        now = now if now is not None else time.time()
        k = f"{key}|{pattern}"
        p = self._perms.setdefault(k, ObservedPermission(
            key=key, pattern=pattern, decision=decision,
            granted_at=now if decision in (POLICY_ALLOW, POLICY_OVERRIDE) else None,
            risk_tier=tier))
        p.usage_count += 1
        p.last_used = now
        if decision == POLICY_OVERRIDE:
            p.overrides += 1
        if decision == POLICY_ALLOW and p.granted_at is None:
            p.granted_at = now

    def list(self) -> list[dict]:
        return [p.to_public() for p in self._perms.values()]


class FilePolicyStore:
    """Very small persisted rule store (JSON on disk) so allow/deny lists
    survive restarts. A local-first YAML-free alternative to a full policy
    language."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._rules: dict[str, PolicyRule] = {}
        self._load()

    def _load(self):
        if self.path.exists():
            import json
            try:
                data = json.loads(self.path.read_text())
                for r in data:
                    rule = PolicyRule(**r)
                    self._rules[f"{rule.key}|{rule.pattern}"] = rule
            except (OSError, ValueError):
                pass

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(_json_dump([r.model_dump(mode="json")
                                         for r in self._rules.values()]))

    def set_rule(self, rule: PolicyRule):
        self._rules[f"{rule.key}|{rule.pattern}"] = rule
        self.save()

    def get_rule(self, key: str, pattern: str) -> dict | None:
        r = self._rules.get(f"{key}|{pattern}")
        return r.model_dump(mode="json") if r else None

    def list_rules(self) -> list[dict]:
        return [r.model_dump(mode="json") for r in self._rules.values()]

    def remove_rule(self, key: str, pattern: str) -> bool:
        removed = self._rules.pop(f"{key}|{pattern}", None)
        if removed:
            self.save()
        return removed is not None


def _json_dump(obj: Any) -> str:
    import json
    return json.dumps(obj, indent=2, default=str)
