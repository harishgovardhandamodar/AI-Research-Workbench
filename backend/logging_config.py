"""B2: central structured logging configuration.

Configures the root logger with a console handler plus a rotating file handler
under ``<WORKBENCH_DIR>/logs/fox.log`` (created best-effort — a read-only
workbench dir degrades to console-only rather than crashing). Module loggers
use the ``fox.*`` namespace (e.g. ``fox.coordinator``, ``fox.runtime``,
``fox.campaign``), which makes individual subsystems filterable.
"""

from __future__ import annotations

import contextvars
import logging
import logging.config
import os
import sys
from logging.handlers import RotatingFileHandler

try:
    from .paths import WORKBENCH_DIR
except Exception:  # noqa: BLE001
    WORKBENCH_DIR = None

_FORMAT = ("%(asctime)s | %(levelname)-7s | %(name)s | "
           "%(message)s")
_DATEFMT = "%Y-%m-%dT%H:%M:%S%z"

_configured = False

# Correlation context: per-task (contextvar) project / run / message ids so a
# single log line can be traced across the whole pipeline. Set with
# ``set_log_context``, cleared with ``clear_log_context``.
LOG_CTX: contextvars.ContextVar[dict] = contextvars.ContextVar(
    "fox_log_ctx", default={})


def set_log_context(**kw) -> None:
    """Merge key/value pairs into the current task's log-correlation context
    (empty/None values are ignored)."""
    merged = dict(LOG_CTX.get())
    for k, v in kw.items():
        if v is not None and str(v) != "":
            merged[k] = str(v)
    LOG_CTX.set(merged)


def clear_log_context(*keys) -> None:
    """Remove one or more keys from the current task's log context."""
    if not keys:
        LOG_CTX.set({})
        return
    merged = dict(LOG_CTX.get())
    for k in keys:
        merged.pop(k, None)
    LOG_CTX.set(merged)


class ContextFormatter(logging.Formatter):
    """Formatter that appends the task's correlation context (project/run/…)
    to every record, so log lines are greppable by project/run id."""

    def format(self, record: logging.LogRecord) -> str:
        line = super().format(record)
        ctx = LOG_CTX.get()
        if ctx:
            suffix = " | " + " ".join(f"{k}={v}" for k, v in sorted(ctx.items()))
            line = line + suffix
        return line


def _context_formatter(fmt: str | None = None, datefmt: str | None = None):
    return ContextFormatter(fmt=fmt or _FORMAT, datefmt=datefmt or _DATEFMT)


def _file_handler() -> logging.Handler | None:
    """Best-effort rotating file handler (None when unwritable)."""
    if WORKBENCH_DIR is None:
        return None
    try:
        logs_dir = WORKBENCH_DIR / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            logs_dir / "fox.log", maxBytes=5_000_000, backupCount=3,
            encoding="utf-8")
        handler.setFormatter(logging.Formatter(_FORMAT, datefmt=_DATEFMT))
        return handler
    except Exception:  # noqa: BLE001
        return None


def setup_logging(level: str | int = logging.INFO,
                  force: bool = False) -> None:
    """Install structured logging on the root logger (idempotent).

    Subsystems log via ``logging.getLogger("fox.<subsystem>")``. ``force=True``
    re-applies the configuration (useful in tests).
    """
    global _configured
    if _configured and not force:
        return

    handlers: list[logging.Handler] = []
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter(_FORMAT, datefmt=_DATEFMT))
    handlers.append(console)
    fh = _file_handler()
    if fh is not None:
        handlers.append(fh)

    logging.config.dictConfig({
        "version": 1,
        "disable_existing_loggers": False,
        "root": {"level": level, "handlers": ["console", "file"] if fh else ["console"]},
        "handlers": {
            "console": {"class": "logging.StreamHandler", "stream": "ext://sys.stdout",
                        "formatter": "standard"},
            **({"file": {"class": "logging.handlers.RotatingFileHandler",
                         "filename": str(fh.baseFilename), "maxBytes": 5_000_000,
                         "backupCount": 3, "encoding": "utf-8",
                         "formatter": "standard"}} if fh else {}),
        },
        "formatters": {
            "standard": {"()": _context_formatter, "format": _FORMAT,
                         "datefmt": _DATEFMT},
        },
    })
    # Silence noisy third-party loggers.
    for noisy in ("uvicorn.access", "httpcore", "httpx", "openai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _configured = True
