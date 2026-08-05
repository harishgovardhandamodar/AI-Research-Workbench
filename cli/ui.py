"""Terminal UI toolkit: ANSI styling, panels, tables, spinner.

Zero-dependency, stdlib-only. Gives the CLI the same look-and-feel as modern
terminal AI tools (opencode / hermes): warm fox accent palette, rounded
bordered panels, aligned key/value rows, data tables, and a live spinner for
blocking calls.
"""

from __future__ import annotations

import shutil
import sys
import threading
import time
from typing import Any, Callable, Iterable

# ----------------------------------------------------------------- palette ----
# Fox palette (warm amber/orange on dark terminals)
ACCENT = (255, 122, 42)      # fox orange
AMBER = (251, 191, 36)       # amber-400
GREEN = (52, 211, 153)       # emerald-400
RED = (248, 113, 113)        # red-400
YELLOW = (250, 204, 21)      # yellow-400
PURPLE = (192, 132, 252)     # violet-400
CYAN = (34, 211, 238)        # cyan-400
TEXT = (226, 232, 240)       # slate-200
DIM = (148, 163, 184)        # slate-400
FADED = (100, 116, 139)      # slate-500

_BOX = {"tl": "╭", "tr": "╮", "bl": "╰", "br": "╯", "h": "─", "v": "│"}


def _rgb(rgb: tuple[int, int, int]) -> str:
    return f"\x1b[38;2;{rgb[0]};{rgb[1]};{rgb[2]}m"


def c(text: Any, rgb: tuple[int, int, int] = TEXT) -> str:
    return f"{_rgb(rgb)}{text}\x1b[0m"


def bold(text: Any, rgb: tuple[int, int, int] | None = None) -> str:
    if rgb:
        return f"\x1b[1m{_rgb(rgb)}{text}\x1b[0m"
    return f"\x1b[1m{text}\x1b[0m"


def dim(text: Any) -> str:
    return f"\x1b[2m{text}\x1b[0m"


def italic(text: Any) -> str:
    return f"\x1b[3m{text}\x1b[0m"


def underline(text: Any) -> str:
    return f"\x1b[4m{text}\x1b[0m"


def ok(text: Any = "✓") -> str:
    return c(text, GREEN)


def warn(text: Any = "!") -> str:
    return c(text, YELLOW)


def err(text: Any = "✗") -> str:
    return c(text, RED)


def accent(text: Any) -> str:
    return c(text, ACCENT)


def strip_ansi(s: str) -> str:
    import re

    return re.sub(r"\x1b\[[0-9;]*m", "", s)


def width() -> int:
    try:
        return shutil.get_terminal_size((88, 24)).columns
    except Exception:  # noqa: BLE001
        return 88


# ------------------------------------------------------------------ panels ----

def panel(title: str, body: str, title_rgb: tuple[int, int, int] = ACCENT,
          border_rgb: tuple[int, int, int] = DIM, pad: int = 1) -> str:
    """A rounded box with an optional title on the top border."""
    w = width()
    inner_w = max(w - 4 - pad * 2, 10)
    lines: list[str] = []
    b = _BOX
    tb = c(b["h"] * 2 + " " + b["h"] * (inner_w + pad * 2), border_rgb)
    tl, tr, bl, br = (c(b["tl"], border_rgb), c(b["tr"], border_rgb),
                      c(b["bl"], border_rgb), c(b["br"], border_rgb))
    if title:
        label = f" {bold(title, title_rgb)} "
        n = max(len(strip_ansi(tb)) - len(strip_ansi(label)) - 4, 0)
        lines.append(f"{tl}{label}{c(b['h'] * n, border_rgb)}{tr}")
    else:
        lines.append(f"{tl}{tb}{tr}")
    body_lines = body.split("\n") if body else [""]
    for raw in body_lines:
        text = raw if raw else " "
        padded = " " * pad + text
        remaining = max(inner_w + pad * 2 - len(strip_ansi(padded)), 0)
        lines.append(f"{c(b['v'], border_rgb)} {padded}{' ' * remaining} {c(b['v'], border_rgb)}")
    lines.append(f"{bl}{tb}{br}")
    return "\n".join(lines)


def keyval(key: str, value: Any, key_rgb: tuple[int, int, int] = DIM,
           val_rgb: tuple[int, int, int] = TEXT, col: int = 10) -> str:
    k = c(key, key_rgb) + ":" + " " * max(1, col - len(key))
    return f"  {k} {c(value, val_rgb)}"


def kv_block(rows: Iterable[tuple[str, Any]], **kw: Any) -> str:
    return "\n".join(keyval(k, v, **kw) for k, v in rows)


def table(headers: list[str], rows: Iterable[Iterable[Any]],
          header_rgb: tuple[int, int, int] = ACCENT,
          row_rgb: tuple[int, int, int] = TEXT,
          dim_rgb: tuple[int, int, int] = DIM) -> str:
    """Align a table by stripping ANSI codes for width math."""
    data = [[str(x) for x in row] for row in rows]
    cols = len(headers)
    widths = [len(h) for h in headers]
    for row in data:
        for i in range(min(cols, len(row))):
            widths[i] = max(widths[i], len(strip_ansi(row[i])))
    lines = ["  " + "  ".join(
        bold(h, header_rgb).ljust(widths[i] + 2) for i, h in enumerate(headers))]
    sep = "  " + "  ".join(c("─" * (w + 2), dim_rgb) for w in widths)
    lines.append(sep)
    for row in data:
        cells = []
        for i in range(cols):
            val = row[i] if i < len(row) else ""
            cells.append(strip_ansi(val).ljust(widths[i] + 2))
        lines.append("  " + "".join(cells))
    return "\n".join(lines)


def hr(rgb: tuple[int, int, int] = FADED) -> str:
    return c("─" * width(), rgb)


# ------------------------------------------------------------------ spinner ---

class Spinner:
    """Animated spinner for blocking work (opencode-style bottom activity)."""

    FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

    def __init__(self, label: str = "", stream=None) -> None:
        self.label = label
        self.stream = stream or sys.stderr
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "Spinner":
        self.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stop()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        i = 0
        while not self._stop.is_set():
            frame = c(self.FRAMES[i % len(self.FRAMES)], ACCENT)
            label = " " + self.label if self.label else ""
            msg = f"\r  {frame}{c(label, DIM)}"
            try:
                self.stream.write(msg)
                self.stream.flush()
            except Exception:  # noqa: BLE001
                break
            i += 1
            self._stop.wait(0.08)

    def stop(self, final: str = "✓") -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=0.3)
        msg = f"\r  {c(final, GREEN)} {c(self.label, DIM)}"
        try:
            self.stream.write(msg + "\n")
            self.stream.flush()
        except Exception:  # noqa: BLE001
            pass


def run_with_spinner(label: str, fn: Callable[..., Any], *args: Any,
                     **kwargs: Any) -> Any:
    """Run ``fn(*args, **kwargs)`` while showing a spinner; returns the result
    (or re-raises)."""
    spinner = Spinner(label, stream=kwargs.pop("stream", None))
    spinner.start()
    try:
        return fn(*args, **kwargs)
    finally:
        spinner.stop()


def progress(percent: float, bar_w: int = 22, rgb: tuple[int, int, int] = ACCENT) -> str:
    """A tiny inline progress bar: `[███████····] 42%`"""
    pct = max(0.0, min(1.0, float(percent)))
    filled = int(round(bar_w * pct))
    bar = c("█" * filled, rgb) + dim("░" * (bar_w - filled))
    return f"[{bar}] {int(pct * 100)}%"


def spin_wait(seconds: float, label: str = "") -> None:
    """Blocking (short) animated wait — used by the splash animation."""
    stop = time.time() + seconds
    i = 0
    while time.time() < stop:
        frame = Spinner.FRAMES[i % len(Spinner.FRAMES)]
        print(f"\r  {c(frame, ACCENT)} {c(label, DIM)}", end="", flush=True)
        i += 1
        time.sleep(0.08)
    print("\r" + " " * (len(label) + 8), end="\r", flush=True)


def fmt_time(ts: float | None) -> str:
    if not ts:
        return "—"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
