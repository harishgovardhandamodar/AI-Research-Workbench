"""Reusable TUI components (pure rendering — no terminal side effects).

Includes the input line buffer + completion + key decoding (unit-testable
without a terminal) and theme-aware chrome widgets: header, status bar,
panels, lists, tabs, spinner, toasts.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .theme import Capabilities, Theme, strip_ansi, visible_width

# -------------------------------------------------------------- line buffer --

@dataclass
class LineBuffer:
    """Editable single-line input buffer."""

    text: str = ""
    pos: int = 0

    def __post_init__(self) -> None:
        self.pos = max(0, min(self.pos, len(self.text)))

    def insert(self, ch: str) -> None:
        self.text = self.text[: self.pos] + ch + self.text[self.pos :]
        self.pos += 1

    def backspace(self) -> None:
        if self.pos > 0:
            self.text = self.text[: self.pos - 1] + self.text[self.pos :]
            self.pos -= 1

    def delete(self) -> None:
        if self.pos < len(self.text):
            self.text = self.text[: self.pos] + self.text[self.pos + 1 :]

    def delete_word(self) -> None:
        """Delete the word before the cursor (Ctrl+W)."""
        start = self.pos
        while start > 0 and self.text[start - 1] == " ":
            start -= 1
        while start > 0 and self.text[start - 1] != " ":
            start -= 1
        self.text = self.text[:start] + self.text[self.pos :]
        self.pos = start

    def left(self, n: int = 1) -> None:
        self.pos = max(0, self.pos - n)

    def right(self, n: int = 1) -> None:
        self.pos = min(len(self.text), self.pos + n)

    def home(self) -> None:
        self.pos = 0

    def end(self) -> None:
        self.pos = len(self.text)

    def set(self, text: str, pos: int | None = None) -> None:
        self.text = text
        self.pos = len(text) if pos is None else pos

    def clear(self) -> None:
        self.text, self.pos = "", 0

    def word_before(self) -> str:
        start = self.pos
        while start > 0 and self.text[start - 1] not in " \t":
            start -= 1
        return self.text[start : self.pos]

    def replace_word(self, word: str) -> None:
        start = self.pos
        while start > 0 and self.text[start - 1] not in " \t":
            start -= 1
        self.text = self.text[:start] + word + self.text[self.pos :]
        self.pos = start + len(word)


class _Completer:
    """Tab-completion with cycling through candidates."""

    def __init__(self, words: list[str]) -> None:
        self.words = words
        self.cands: list[str] = []
        self.idx = 0
        self.prefix = ""

    def next(self, buf: LineBuffer) -> None:
        word = buf.word_before()
        if not self.cands or (word != self.prefix and word not in self.cands):
            self.cands = [w for w in self.words if w.startswith(word)]
            self.idx = 0
            self.prefix = word
        if not self.cands:
            return
        buf.replace_word(self.cands[self.idx % len(self.cands)])
        self.idx += 1

    def reset(self) -> None:
        self.cands = []
        self.idx = 0
        self.prefix = ""


# --------------------------------------------------------------------- keys --

@dataclass
class _Key:
    value: str


_KEYS = {
    b"\x1b[A": _Key("up"), b"\x1b[B": _Key("down"),
    b"\x1b[C": _Key("right"), b"\x1b[D": _Key("left"),
    b"\x1b[H": _Key("home"), b"\x1b[F": _Key("end"),
    b"\x1b[1~": _Key("home"), b"\x1b[4~": _Key("end"),
    b"\x1b[7~": _Key("home"), b"\x1b[8~": _Key("end"),
    b"\x1b[3~": _Key("del"), b"\x1b[5~": _Key("pgup"),
    b"\x1b[6~": _Key("pgdn"),
    b"\r": _Key("enter"), b"\n": _Key("enter"),
    b"\t": _Key("tab"),
    b"\x03": _Key("ctrl_c"), b"\x04": _Key("ctrl_d"),
    b"\x11": _Key("ctrl_q"),
    b"\x14": _Key("ctrl_t"), b"\x10": _Key("ctrl_p"),
    b"\x02": _Key("ctrl_b"), b"\x15": _Key("ctrl_u"),
    b"\x17": _Key("ctrl_w"), b"\x0c": _Key("ctrl_l"),
    b"\x7f": _Key("backspace"),
    b"\x1b": _Key("esc"),
}


def _is_printable(data: bytes) -> bool:
    if not data or data[0] == 0x1B or data[0] in (0x0D, 0x0A, 0x09, 0x03, 0x04,
                                                  0x11, 0x14, 0x10, 0x02,
                                                  0x15, 0x17, 0x0C, 0x7F):
        return False
    try:
        data.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


def decode_key(data: bytes) -> _Key | str | None:
    if not data:
        return None
    if data in _KEYS:
        return _KEYS[data]
    if data.startswith(b"\x1b[") and data[-1:] in (b"A", b"B", b"C", b"D",
                                                   b"H", b"F", b"~"):
        return _KEYS.get(data) or _Key("unknown")
    if _is_printable(data):
        return data.decode("utf-8")
    return None


def read_escape_sequence(fd: int, first: bytes) -> bytes:
    """After an ESC byte, drain the rest of a CSI sequence with a short poll."""
    import select
    import sys

    seq = first
    end = time.time() + 0.05
    while time.time() < end:
        r, _, _ = select.select([sys.stdin], [], [], 0.005)
        if not r:
            break
        try:
            chunk = os_read(fd, 64)
        except OSError:
            break
        if not chunk:
            break
        seq += chunk
    return seq


def os_read(fd: int, n: int) -> bytes:
    import os
    return os.read(fd, n)


# --------------------------------------------------------------- rendering --

def _truncate(s: str, width: int) -> str:
    """Truncate a possibly-ANSI string to `width` visible columns, preserving
    escape sequences that precede the cut point and appending an ellipsis."""
    if width <= 0:
        return ""
    if len(strip_ansi(s)) <= width:
        return s
    import re
    ansi = re.compile(r"\x1b\[[0-9;]*m")
    out: list[str] = []
    vis = 0
    i = 0
    while i < len(s):
        m = ansi.match(s, i)
        if m:
            out.append(m.group(0))
            i = m.end()
            continue
        if vis >= width:
            break
        out.append(s[i])
        vis += 1
        i += 1
    # keep total visible width at exactly `width`: drop the last visible char
    # (out ends on a visible char — we broke at the first char past width)
    out.pop()
    out.append("…")
    return "".join(out)


def _pad(s: str, width: int) -> str:
    """Pad (or truncate) a string to exactly `width` visible columns."""
    plain = strip_ansi(s)
    if len(plain) > width:
        return _truncate(s, width)
    return s + " " * (width - len(plain))


def box_chars(cap: Capabilities) -> dict[str, str]:
    """Rounded border glyphs (fall back to ASCII when unsupported)."""
    if not cap.basic and not cap.is_tty:
        return {"tl": "+", "tr": "+", "bl": "+", "br": "+",
                "h": "-", "v": "|"}
    return {"tl": "╭", "tr": "╮", "bl": "╰", "br": "╯", "h": "─", "v": "│"}


class Panel:
    """A titled rounded box using theme tokens."""

    def __init__(self, theme: Theme, cap: Capabilities) -> None:
        self.theme = theme
        self.cap = cap

    def render(self, title: str, body_lines: list[str], width: int, *,
               active: bool = True, accent_title: bool = True) -> list[str]:
        t, b = self.theme, self.cap
        ch = box_chars(b)
        inner = max(width - 2, 4)
        border = t.fg("borderActive" if active else "borderMuted", b)
        lines: list[str] = []
        top = border + ch["tl"] + ch["h"] * inner + ch["tr"] + t.reset()
        if title:
            label = t.paint("accent" if accent_title else "textMuted",
                            f" {title} ", b, bold=accent_title)
            space = max(inner - visible_width(title) - 2, 0)
            top = (border + ch["tl"] + label
                   + border + ch["h"] * space + ch["tr"] + t.reset())
        lines.append(top)
        for body in body_lines[: inner]:
            lines.append(border + ch["v"] + _pad(body, inner) + ch["v"]
                         + t.reset())
        lines.append(border + ch["bl"] + ch["h"] * inner + ch["br"] + t.reset())
        return lines


class Header:
    """Top title bar: app name + view + shortcut hints."""

    def __init__(self, theme: Theme, cap: Capabilities) -> None:
        self.theme = theme
        self.cap = cap

    def render(self, title: str, mode: str, hints: list[str],
               width: int) -> str:
        t, cap = self.theme, self.cap
        name = t.paint("headerAccent", title, cap, bold=True)
        mode_txt = t.paint("textMuted", f" {mode} ", cap)
        hints_txt = "  " + t.paint("textSubtle",
                                   "  ".join(f"{k} {v}" for k, v in hints),
                                   cap, dim=True)
        text = name + mode_txt + hints_txt
        bar = t.bg("headerBackground", cap) + _pad(text, width) + t.reset()
        return bar


class StatusBar:
    """Bottom bar: mode, selection/status, spinner, theme, key hints."""

    FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

    def __init__(self, theme: Theme, cap: Capabilities,
                 reduce_motion: bool = False) -> None:
        self.theme = theme
        self.cap = cap
        self.reduce_motion = reduce_motion

    def frame(self, t: float) -> str:
        if self.reduce_motion:
            return "●"
        return self.FRAMES[int(t / 0.08) % len(self.FRAMES)]

    def render(self, left: str, right: str, running: bool = False,
               t: float = 0.0, width: int = 80) -> str:
        th, cap = self.theme, self.cap
        if running:
            spin = th.paint("spinner", self.frame(t), cap)
            left = spin + " " + th.paint("loadingText", left, cap)
        else:
            left = th.paint("statusBarForeground", left, cap)
        right = th.paint("statusBarForeground", right, cap, dim=True)
        inner = max(width - visible_width(left) - visible_width(right) - 1, 0)
        bar = (th.bg("statusBarBackground", cap) + left
               + " " * inner + right + th.reset())
        return bar


class TabBar:
    """A row of tabs; the active one is accented."""

    def __init__(self, theme: Theme, cap: Capabilities) -> None:
        self.theme = theme
        self.cap = cap

    def render(self, tabs: list[str], active: int, width: int) -> str:
        th, cap = self.theme, self.cap
        cells = []
        for i, label in enumerate(tabs):
            if i == active:
                cells.append(th.paint("accent", f" {label} ", cap, bold=True))
            else:
                cells.append(th.paint("textMuted", f" {label} ", cap))
        row = "".join(cells)
        return _pad(row, width)


class List:
    """Selectable list rows with muted secondary text."""

    def __init__(self, theme: Theme, cap: Capabilities) -> None:
        self.theme = theme
        self.cap = cap

    def render(self, items: list[tuple[str, str]], selected: int | None,
               width: int, *, select_bg: str = "selectionBackground",
               select_fg: str = "selectionForeground") -> list[str]:
        th, cap = self.theme, self.cap
        out = []
        for i, (primary, secondary) in enumerate(items):
            label = primary + (th.paint("textSubtle", f" {secondary}", cap,
                                        dim=True)
                               if secondary else "")
            if i == selected:
                row = th.paint_bg(select_fg, select_bg, label, cap, bold=True)
            else:
                row = th.paint("text", label, cap)
            out.append(_pad(row, width))
        return out


class Spinner:
    """Inline spinner text (non-blocking; driven by the render loop)."""

    def __init__(self, theme: Theme, cap: Capabilities,
                 reduce_motion: bool = False) -> None:
        self.status = StatusBar(theme, cap, reduce_motion)

    def text(self, label: str, t: float) -> str:
        return self.status.frame(t) + " " + label


class Toast:
    """Transient notification with a kind and expiry."""

    def __init__(self, message: str, kind: str = "info",
                 deadline: float | None = None) -> None:
        self.message = message
        self.kind = kind  # success | warning | error | info
        self.deadline = deadline

    @property
    def expired(self) -> bool:
        return self.deadline is not None and time.time() > self.deadline
