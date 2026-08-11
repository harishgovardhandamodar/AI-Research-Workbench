"""Audit data model.

Every audited action (agent tool call, MCP request, permission decision,
deviation flag, …) is captured as one :class:`AuditEvent`. Events are
hash-chained (``prev_hash`` + canonical JSON → ``event_hash``) so the local
audit log is tamper-evident, and sensitive values are redacted before anything
is persisted.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

Severity = Literal["info", "warning", "critical"]

# Allowed sources; extensible beyond the spec so the workbench host can tag
# events produced by its own coordinator, approval broker and MCP host.
SOURCE_MCP_PROXY = "mcp_proxy"
SOURCE_MIDDLEWARE = "middleware"
SOURCE_OS_MONITOR = "os_monitor"
SOURCE_COORDINATOR = "coordinator"
SOURCE_APPROVAL = "approval"
SOURCE_POLICY = "policy"
SOURCE_DEVIATION = "deviation"
SOURCE_SYSTEM = "system"
SOURCE_KERNEL = "kernel"

ALLOWED_SOURCES = frozenset({
    SOURCE_MCP_PROXY, SOURCE_MIDDLEWARE, SOURCE_OS_MONITOR,
    SOURCE_COORDINATOR, SOURCE_APPROVAL, SOURCE_POLICY,
    SOURCE_DEVIATION, SOURCE_SYSTEM, SOURCE_KERNEL,
})

POLICY_ALLOW = "ALLOW"
POLICY_DENY = "DENY"
POLICY_ASK = "ASK"
POLICY_OVERRIDE = "OVERRIDE"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def ulid_now() -> str:
    """Sortable, collision-safe event id (ULID-style, no external dependency).

    Encodes a 48-bit millisecond timestamp + 80 random bits using Crockford
    base32 so ids sort lexicographically by creation time and are unique per
    process without a service round-trip.
    """
    import os
    import time

    ms = int(time.time() * 1000) & 0xFFFFFFFFFFFF
    entropy = int.from_bytes(os.urandom(10), "big")
    value = (ms << 80) | entropy
    alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
    out = []
    for _ in range(26):
        value, rem = divmod(value, 32)
        out.append(alphabet[rem])
    return "".join(reversed(out))


def canonical_json(obj: Any) -> str:
    """Deterministic JSON serialisation for hashing/chain linkage.

    ``sort_keys`` + ``separators`` make the serialisation stable across
    processes and Python versions so ``event_hash`` values are reproducible.
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      default=str, ensure_ascii=False)


def _short(val: Any) -> Any:
    """Best-effort compact form for a result summary field."""
    if val is None:
        return None
    try:
        return canonical_json(val)
    except (TypeError, ValueError):
        return str(val)


class AuditEvent(BaseModel):
    """A single audited action. Rich, JSON-serialisable, hash-chained."""

    event_id: str = Field(default_factory=ulid_now)
    timestamp: datetime = Field(default_factory=_now)
    trace_id: str | None = None
    run_id: str | None = None
    session_id: str | None = None
    agent_id: str
    principal: dict = Field(
        default_factory=lambda: {"type": "agent", "id": "", "roles": []})
    source: str = SOURCE_COORDINATOR
    mcp_server: str | None = None
    method: str | None = None
    tool_name: str | None = None
    arguments_redacted: dict | None = None
    result_summary: dict | None = None
    network: dict | None = None
    filesystem: dict | None = None
    policy_decision: dict | None = None
    duration_ms: float | None = None
    severity: Severity = "info"
    prev_hash: str | None = None
    event_hash: str | None = None
    tags: list[str] = Field(default_factory=list)

    @field_validator("source")
    @classmethod
    def _check_source(cls, v: str) -> str:
        return v if v in ALLOWED_SOURCES else SOURCE_SYSTEM

    @field_validator("severity")
    @classmethod
    def _check_severity(cls, v: str) -> str:
        return v if v in ("info", "warning", "critical") else "info"

    def compute_hash(self, prev_hash: str | None) -> str:
        """Return this event's own chain hash: sha256(canonical self + prev)."""
        import hashlib

        self.prev_hash = prev_hash
        self.event_hash = None  # never include the hash itself in its own input
        body = canonical_json(self.model_dump())
        self.event_hash = hashlib.sha256(f"{prev_hash or ''}:{body}".encode()).hexdigest()
        return self.event_hash

    def canonical(self) -> str:
        return canonical_json(self.model_dump())

    @classmethod
    def from_dict(cls, data: dict) -> "AuditEvent":
        if "timestamp" in data and isinstance(data["timestamp"], (int, float)):
            data = dict(data)
            data["timestamp"] = datetime.fromtimestamp(data["timestamp"], tz=timezone.utc)
        return cls(**data)

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def result_summary_for(status: str = "ok", data_classes: list | None = None,
                           size: int | None = None, error: str | None = None) -> dict:
        out: dict[str, Any] = {"status": status}
        if data_classes:
            out["data_classes"] = data_classes
        if size is not None:
            out["size"] = size
        if error:
            out["error"] = error[:2000]
        return out


class DeviationRecord(BaseModel):
    """A flagged behavioural deviation linked to the events that triggered it."""

    deviation_id: str = Field(default_factory=ulid_now)
    agent_id: str
    rule: str
    severity: Severity = "warning"
    explanation: str = ""
    event_ids: list[str] = Field(default_factory=list)
    detail: dict = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=_now)
    reviewed: bool = False
    reviewed_at: datetime | None = None
    reviewed_by: str = ""
    false_positive: bool = False

    def to_public(self) -> dict:
        return self.model_dump(mode="json")


class PolicyRule(BaseModel):
    """A persisted allow/deny rule with an optional reason + risk tier."""

    key: str  # "run_shell" | "mcp_tool" | "<server>__<tool>" | "path"
    pattern: str = "*"
    decision: str = POLICY_ALLOW
    risk_tier: Literal["low", "medium", "high", "critical"] = "medium"
    reason: str = ""
    created_by: str = ""
    created_at: datetime = Field(default_factory=_now)


# Keys (argument or result keys) whose values are always redacted before storage.
REDACT_KEYS = frozenset({
    "api_key", "apikey", "api-key", "token", "access_token", "auth_token",
    "refresh_token", "secret", "secret_key", "client_secret", "password",
    "passwd", "pwd", "authorization", "auth", "bearer", "private_key",
    "cookie", "session_token", "ssh_key", "aws_secret_access_key",
    "x_api_key", "app_secret", "consumer_secret",
})
