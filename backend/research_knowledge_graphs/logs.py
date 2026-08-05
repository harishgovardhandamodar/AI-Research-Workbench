from __future__ import annotations

import logging
import threading
from collections import deque
from typing import Any


class LogCapture(logging.Handler):
    def __init__(self, maxlen: int = 500) -> None:
        super().__init__(level=logging.INFO)
        self._buf: deque[dict[str, Any]] = deque(maxlen=maxlen)
        self._lock = threading.Lock()
        formatter = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        )
        self.setFormatter(formatter)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            with self._lock:
                self._buf.append({
                    "time": record.asctime if hasattr(record, "asctime") else "",
                    "level": record.levelname,
                    "name": record.name,
                    "message": record.getMessage(),
                    "formatted": msg,
                })
        except Exception:
            self.handleError(record)

    def get_recent(self, n: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._buf)[-n:]

    def clear(self) -> None:
        with self._lock:
            self._buf.clear()


_capture: LogCapture | None = None


def get_capture() -> LogCapture:
    global _capture
    if _capture is None:
        _capture = LogCapture()
        root = logging.getLogger()
        root.addHandler(_capture)
    return _capture
