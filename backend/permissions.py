"""Permission model: explicit grants for shell/compute/network access.

Policy (deny-by-default, least privilege):
  - run_python / save_artifact  -> allowed (runs inside the sandboxed kernel)
  - run_shell                   -> 'ask' the first time, remember the decision
                                   per command; network commands default to 'ask'
                                   and must be explicitly approved by the user.
"""

from __future__ import annotations

import re

from .store import ProjectStore

NETWORK_RE = re.compile(
    r"^\s*(curl|wget|nc|ncat|telnet|ftp|sftp|ssh|scp|rsync|git\s+(clone|fetch|pull)|"
    r"pip\s+install|npm\s+install|apt|apt-get|dnf|yum|brew|kubectl|gh\b)\b",
    re.IGNORECASE,
)

SHELL_BIN_RE = re.compile(r"^\s*([a-zA-Z0-9_.\-+/]+)")


class PermissionError(RuntimeError):
    def __init__(self, command: str, reason: str):
        super().__init__(reason)
        self.command = command
        self.reason = reason


class PermissionManager:
    def __init__(self, store: ProjectStore):
        self.store = store

    def _normalize(self, command: str) -> str:
        return " ".join(command.strip().split())[:200]

    def check(self, kind: str, command: str) -> str:
        """Return 'allow', 'deny', or 'ask'."""
        norm = self._normalize(command)
        saved = self.store.get_grant(kind, norm)
        if saved:
            return saved
        if kind == "run_python":
            return "allow"
        if kind == "save_artifact":
            return "allow"
        if kind == "run_shell":
            if NETWORK_RE.match(norm):
                return "ask"
            m = SHELL_BIN_RE.match(norm)
            if m and m.group(1) in {"rm", "mkfs", "dd", "shutdown", "reboot", "kill",
                                    "pkill", "chmod", "chown", "sudo", "su", "fdisk",
                                    "format"}:
                return "ask"
            return "ask"
        return "ask"

    def record(self, kind: str, command: str, decision: str):
        norm = self._normalize(command)
        self.store.set_grant(kind, norm, decision)
