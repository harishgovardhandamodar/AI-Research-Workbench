"""Local agent audit trail system.

Captures, stores, redacts, chains (tamper-evident), and surfaces an audit
trail of agent + MCP tool activity — data access, network, filesystem,
permissions and behavioural deviations — fully local. See ``audit/cli.py`` for
the ``agent-audit`` CLI (proxy / dashboard / query / verify / baseline /
export) and ``audit/proxy.py`` for the transparent MCP proxy.
"""

from __future__ import annotations

from .models import AuditEvent, DeviationRecord, PolicyRule, ulid_now
from .redaction import redact, redact_string
from .store import LocalAuditStore
from .emitter import AuditEmitter
from .policy import (DEFAULT_RISK_TIERS, FilePolicyStore, PermissionTracker,
                     PolicyEngine, risk_tier_for, severity_for_tier)
from .deviation import DeviationDetector
from .middleware import audit_tool, AuditedSession

__version__ = "0.1.0"

__all__ = [
    "AuditEvent", "DeviationRecord", "PolicyRule", "ulid_now",
    "redact", "redact_string",
    "LocalAuditStore", "AuditEmitter",
    "DEFAULT_RISK_TIERS", "FilePolicyStore", "PermissionTracker",
    "PolicyEngine", "risk_tier_for", "severity_for_tier",
    "DeviationDetector", "audit_tool", "AuditedSession",
    "__version__",
]
