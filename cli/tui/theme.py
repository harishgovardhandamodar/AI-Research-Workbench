"""Theme engine for the Fox TUI.

Semantic color tokens drive every piece of TUI chrome (header, sidebar,
panels, status bar, input, overlays) — components never hardcode colors.
Supports truecolor (24-bit), 256-color, and 16-color ANSI fallback, with
auto-detection of terminal capabilities.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

# ------------------------------------------------------------------ tokens --

# Required semantic tokens (per the Fox TUI design spec).
BASE_TOKENS = [
    "background", "surface", "surfaceAlt", "panel", "panelInactive",
    "border", "borderActive", "borderMuted",
]
TEXT_TOKENS = ["text", "textMuted", "textSubtle", "textInverse", "textLink"]
ACCENT_TOKENS = ["accent", "accentHover", "accentActive", "accentMuted"]
STATE_TOKENS = ["success", "warning", "error", "info"]
SELECTION_TOKENS = ["selectionBackground", "selectionForeground"]
INPUT_TOKENS = ["inputBackground", "inputForeground", "inputPlaceholder",
                "inputCursor"]
SIDEBAR_TOKENS = ["sidebarBackground", "sidebarForeground",
                  "sidebarSelectedBackground", "sidebarSelectedForeground",
                  "sidebarMuted"]
HEADER_TOKENS = ["headerBackground", "headerForeground", "headerAccent"]
STATUSBAR_TOKENS = ["statusBarBackground", "statusBarForeground",
                    "statusBarError", "statusBarWarning", "statusBarSuccess"]
CODE_TOKENS = ["codeBackground", "codeForeground", "codeLineNumber",
               "codeLanguageLabel"]
DIFF_TOKENS = ["diffAddedBackground", "diffAddedForeground",
               "diffRemovedBackground", "diffRemovedForeground",
               "diffContextForeground"]
LOADING_TOKENS = ["spinner", "loadingText"]

ALL_TOKENS = (BASE_TOKENS + TEXT_TOKENS + ACCENT_TOKENS + STATE_TOKENS
              + SELECTION_TOKENS + INPUT_TOKENS + SIDEBAR_TOKENS
              + HEADER_TOKENS + STATUSBAR_TOKENS + CODE_TOKENS
              + DIFF_TOKENS + LOADING_TOKENS)


def hex_to_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return tuple(int(h[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


# ------------------------------------------------------ capability detection --

@dataclass
class Capabilities:
    """What the current terminal can display."""

    truecolor: bool = False
    color256: bool = False
    basic: bool = False
    is_tty: bool = False

    @property
    def level(self) -> int:
        return 3 if self.truecolor else (2 if self.color256
                                         else (1 if self.basic else 0))


def detect_capabilities() -> Capabilities:
    colorterm = os.environ.get("COLORTERM", "").lower()
    term = os.environ.get("TERM", "")
    tc = ("truecolor" in colorterm) or ("24bit" in colorterm)
    c256 = tc or "256color" in term
    try:
        is_tty = bool(os.isatty(0) and os.isatty(1))
    except Exception:  # noqa: BLE001
        is_tty = False
    return Capabilities(truecolor=tc, color256=c256, basic=bool(term) or c256,
                        is_tty=is_tty)


# -------------------------------------------------------- color resolution ----

def rgb_to_256(r: int, g: int, b: int) -> int:
    """Best-effort hex → xterm-256 cube index."""
    def step(v: int) -> int:
        if v < 48:
            return 0
        if v < 115:
            return 1
        if v < 155:
            return 2
        if v < 195:
            return 3
        if v < 235:
            return 4
        return 5
    cube = 16 + 36 * step(r) + 6 * step(g) + step(b)
    gray = 232 + int(round((r + g + b) / 3 / 10.0))
    gray_rgb = _gray_rgb(gray)
    cube_rgb = _cube_rgb(cube)
    gray_d = _dist(gray_rgb, (r, g, b))
    cube_d = _dist(cube_rgb, (r, g, b))
    return gray if gray_d < cube_d else cube


def _cube_rgb(i: int) -> tuple[int, int, int]:
    i -= 16
    def v(x: int) -> int:
        return 0 if x == 0 else (55 + x * 40)
    return v(i // 36), v((i // 6) % 6), v(i % 6)


def _gray_rgb(i: int) -> tuple[int, int, int]:
    v = 8 + (i - 232) * 10
    return v, v, v


def _dist(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    return sum((x - y) ** 2 for x, y in zip(a, b))


_ANSI16 = [
    (0, 0, 0), (128, 0, 0), (0, 128, 0), (128, 128, 0),
    (0, 0, 128), (128, 0, 128), (0, 128, 128), (192, 192, 192),
    (128, 128, 128), (255, 0, 0), (0, 255, 0), (255, 255, 0),
    (0, 0, 255), (255, 0, 255), (0, 255, 255), (255, 255, 255),
]


def rgb_to_ansi16(r: int, g: int, b: int) -> int:
    best, bd = 0, 1e18
    for i, (cr, cg, cb) in enumerate(_ANSI16):
        d = (cr - r) ** 2 + (cg - g) ** 2 + (cb - b) ** 2
        if d < bd:
            bd, best = d, i
    return best


# ------------------------------------------------------------------- theme ---

class Theme:
    """A named set of semantic color tokens, resolved per capability."""

    def __init__(self, name: str, type_: str, colors: dict[str, str]) -> None:
        self.name = name
        self.type = type_  # "dark" | "light"
        self.colors = dict(colors)

    # -- validation ---------------------------------------------------------
    def missing(self) -> list[str]:
        return [t for t in ALL_TOKENS if t not in self.colors]

    def validate(self) -> list[str]:
        bad = []
        for token, value in self.colors.items():
            if not isinstance(value, str) or not value.startswith("#"):
                bad.append(token)
            else:
                try:
                    hex_to_rgb(value)
                except ValueError:
                    bad.append(token)
        return bad

    # -- accessors ----------------------------------------------------------
    def rgb(self, token: str) -> tuple[int, int, int]:
        try:
            return hex_to_rgb(self.colors[token])
        except KeyError:
            return 0, 0, 0

    def fg(self, token: str, cap: Capabilities) -> str:
        """SGR foreground sequence for a token under the given capabilities."""
        r, g, b = self.rgb(token)
        if cap.truecolor:
            return f"\x1b[38;2;{r};{g};{b}m"
        if cap.color256:
            return f"\x1b[38;5;{rgb_to_256(r, g, b)}m"
        if cap.basic:
            return f"\x1b[{30 + rgb_to_ansi16(r, g, b)}m"
        return ""

    def bg(self, token: str, cap: Capabilities) -> str:
        r, g, b = self.rgb(token)
        if cap.truecolor:
            return f"\x1b[48;2;{r};{g};{b}m"
        if cap.color256:
            return f"\x1b[48;5;{rgb_to_256(r, g, b)}m"
        if cap.basic:
            return f"\x1b[{40 + rgb_to_ansi16(r, g, b)}m"
        return ""

    @staticmethod
    def reset() -> str:
        return "\x1b[0m"

    def paint(self, token: str, text: str, cap: Capabilities, *,
              bold: bool = False, dim: bool = False) -> str:
        """Colorize `text` with a token (fg), with optional bold/dim."""
        parts = []
        if bold:
            parts.append("\x1b[1m")
        if dim:
            parts.append("\x1b[2m")
        parts.append(self.fg(token, cap))
        parts.append(str(text))
        parts.append(self.reset())
        return "".join(parts)

    def paint_bg(self, fg_token: str, bg_token: str, text: str,
                 cap: Capabilities, *, bold: bool = False) -> str:
        parts = [self.bg(bg_token, cap)]
        if bold:
            parts.append("\x1b[1m")
        parts.append(self.fg(fg_token, cap))
        parts.append(str(text))
        parts.append(self.reset())
        return "".join(parts)

    def swatch(self, token: str, cap: Capabilities, char: str = "█") -> str:
        """A solid-color swatch for previews (e.g. the theme picker)."""
        return self.bg(token, cap) + char + self.reset()


def strip_ansi(s: str) -> str:
    import re
    return re.sub(r"\x1b\[[0-9;]*m", "", s)


def visible_width(s: str) -> int:
    return len(strip_ansi(s))
