"""Secret redaction.

Scans arbitrary JSON data (tool arguments, headers, file paths, command
lines) for known sensitive keys / values and masks them before an
:class:`audit.models.AuditEvent` is persisted. Redaction is recursive and
non-destructive to the original caller's payload (a deep copy is returned).
"""

from __future__ import annotations

import re
from typing import Any

from .models import REDACT_KEYS

MASK = "***REDACTED***"

# Inline secret patterns commonly found in command lines / URLs:
#   --token=abc  -H "Authorization: Bearer xyz"  https://user:pass@host  key=...
_INLINE_PATTERNS = [
    re.compile(r"(?P<k>(?:bearer|token|apikey|api_key|api-key|password|passwd|"
               r"secret|authorization|auth|key|pwd))\s*[:=]\s*\S+", re.IGNORECASE),
    re.compile(r"(?P<k>authorization)\s*:\s*[^\s\"']+", re.IGNORECASE),
]
_INLINE_URL_CREDS = re.compile(r"(//[^/@\s]+):[^/@\s]+@", re.IGNORECASE)  # user:pass@host

_SECRETISH_VALUE = re.compile(
    r"(?i)(?:^|[^a-z0-9_-])((?:[a-z0-9_-]{10,}\.?){2,}[a-z0-9_-]{4,}|"
    r"[A-Za-z0-9_\-]{24,})(?:$|[^a-z0-9_-])")


def _should_redact_value(value: str) -> bool:
    """Heuristic for values that look like live secrets (long/token-like)."""
    if len(value) < 12:
        return False
    return bool(_SECRETISH_VALUE.match(value))


def redact(obj: Any, extra_keys: set[str] | None = None) -> Any:
    """Deep-copy `obj` with sensitive keys and token-like values masked."""
    keys = REDACT_KEYS | (extra_keys or set())

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            out: dict = {}
            for k, v in node.items():
                kk = str(k).lower()
                if kk in keys:
                    out[k] = MASK
                elif isinstance(v, str) and _should_redact_value(v):
                    out[k] = redact_string(v)
                else:
                    out[k] = walk(v)
            return out
        if isinstance(node, list):
            return [walk(x) for x in node]
        if isinstance(node, str):
            return redact_string(node)
        return node

    return walk(obj)


def redact_string(text: str) -> str:
    """Mask inline secrets inside a free-text string (commands, URLs, headers)."""
    masked = text
    for pattern in _INLINE_PATTERNS:
        masked = pattern.sub(f"\\g<k>={MASK}", masked)
    masked = _INLINE_URL_CREDS.sub(f"\\1:{MASK}@", masked)
    return masked


def sanitize_key(key: str) -> bool:
    """True when a key name should be treated as sensitive."""
    return key.lower() in REDACT_KEYS
