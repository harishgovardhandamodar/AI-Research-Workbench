"""Central app state for the TUI.

Rendering reads from State; actions mutate it. No side effects in renderers.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .components import LineBuffer, Toast


@dataclass
class State:
    # views & layout
    view: str = "output"            # output | logs | settings
    sidebar_visible: bool = True
    focus: str = "input"            # input | sidebar
    overlay: str | None = None      # None | help | palette | theme | confirm

    # input
    buf: LineBuffer = field(default_factory=LineBuffer)
    hist: list[str] = field(default_factory=list)
    hist_idx: int = 0

    # content
    lines: list[str] = field(default_factory=list)   # output panel history
    logs: list[tuple[str, str, int | None]] = field(default_factory=list)
    scroll: int = 0

    # execution
    running: bool = False
    running_label: str = ""

    # chrome
    status_msg: str = "ready"
    status_ok: bool = True
    sidebar_idx: int = 0
    theme_idx: int = 0
    overlay_query: str = ""
    overlay_idx: int = 0
    toast: Toast | None = None
    quit: bool = False

    def log(self, command: str, code: int | None) -> None:
        import time as _t
        ts = _t.strftime("%H:%M:%S")
        self.logs.append((ts, command, code))
        self.logs = self.logs[-200:]
