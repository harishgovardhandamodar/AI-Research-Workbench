"""Minimal leveled logging for the Fox CLI.

Writes to stderr so stdout stays clean for data output (panels or ``--json``).
Enable DEBUG via the global ``--debug`` flag or the ``FOX_DEBUG`` env var.
"""

from __future__ import annotations

import os
import sys
import time

DEBUG, INFO, WARN, ERROR = 10, 20, 30, 40
_LEVELS = {"DEBUG": DEBUG, "INFO": INFO, "WARN": WARN, "ERROR": ERROR}

_PREFIX = {
    DEBUG: "debug",
    INFO: "info ",
    WARN: "warn ",
    ERROR: "error",
}


class _Logger:
    def __init__(self) -> None:
        self.level = _LEVELS.get(os.environ.get("FOX_DEBUG", "").upper(), INFO)
        self._enabled = False

    def set_level(self, name: str) -> None:
        self.level = _LEVELS.get(str(name).upper(), INFO)
        self._enabled = self.level <= DEBUG

    @property
    def debug_enabled(self) -> bool:
        return self.level <= DEBUG

    def _write(self, level: int, msg: str) -> None:
        if level < self.level:
            return
        try:
            ts = time.strftime("%H:%M:%S")
            print(f"[fox:{_PREFIX[level]}] {ts} {msg}", file=sys.stderr)
        except Exception:  # noqa: BLE001
            pass

    def debug(self, msg: str, *args: object) -> None:
        if self.level <= DEBUG:
            self._write(DEBUG, msg.format(*args) if args else msg)

    def info(self, msg: str, *args: object) -> None:
        self._write(INFO, msg.format(*args) if args else msg)

    def warn(self, msg: str, *args: object) -> None:
        self._write(WARN, msg.format(*args) if args else msg)

    def error(self, msg: str, *args: object) -> None:
        self._write(ERROR, msg.format(*args) if args else msg)


log = _Logger()
