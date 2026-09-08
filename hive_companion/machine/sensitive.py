"""Sensitive data classifier — local-first, no egress.

Detects PII/secrets in file contents / tool args / network payloads
and assigns sensitivity level for audit severity.
"""

from __future__ import annotations

import re

PATTERNS = {
    "api_key": re.compile(r"(sk-[a-zA-Z0-9]{20,}|ghp_[a-zA-Z0-9]{30,}|AKIA[0-9A-Z]{16})"),
    "private_key": re.compile(r"-----BEGIN (?:RSA )?PRIVATE KEY-----"),
    "email": re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}"),
    "aws_secret": re.compile(r"aws_secret_access_key\s*=\s*[A-Za-z0-9/+=]{40}"),
    "credit_card": re.compile(r"\b(?:4[0-9]{12}(?:[0-9]{3})?|5[1-5][0-9]{14})\b"),
    "ssn": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    "hf_token": re.compile(r"hf_[a-zA-Z0-9]{30,}"),
}

LEVELS = {"none": 0, "pii": 1, "secret": 2, "critical": 3}

def classify(text: str) -> tuple[str, list[str]]:
    """Return (level, matched_types)."""
    if not text:
        return "none", []
    hits = []
    for name, pat in PATTERNS.items():
        if pat.search(text):
            hits.append(name)
    if not hits:
        return "none", []
    # secrets dominate
    if any(h in ("api_key", "private_key", "aws_secret", "hf_token") for h in hits):
        return "critical", hits
    if any(h in ("credit_card", "ssn") for h in hits):
        return "secret", hits
    return "pii", hits

def is_sensitive(text: str) -> bool:
    lvl, _ = classify(text)
    return lvl != "none"

def redact(text: str, repl: str = "[REDACTED]") -> str:
    for pat in PATTERNS.values():
        text = pat.sub(repl, text)
    return text
