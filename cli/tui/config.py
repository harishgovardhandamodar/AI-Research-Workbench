"""TUI configuration: theme, mouse, sidebar, keybinding overrides.

Stored as JSON at `$FOX_CONFIG_DIR/tui.json` (default `~/.config/fox/tui.json`,
following the XDG convention). A missing file is not an error — defaults are
used and a file is written on the first save.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_KEYS = {
    "ctrl_c": "quit",
    "ctrl_d": "quit",
    "ctrl_q": "quit",
    "tab": "focus_next",
    "ctrl_t": "theme_picker",
    "ctrl_p": "palette",
    "ctrl_b": "toggle_sidebar",
    "?" : "help",
    "esc": "close",
    "enter": "submit",
    "up": "up",
    "down": "down",
    "left": "left",
    "right": "right",
    "pgup": "scroll_up",
    "pgdn": "scroll_down",
    "home": "home",
    "end": "end",
    "backspace": "backspace",
    "del": "del",
    "tab_next": "focus_next",
    "ctrl_l": "clear_output",
    "ctrl_u": "clear_line",
    "ctrl_w": "delete_word",
}

DEFAULTS = {
    "theme": "opencode-dark",
    "mouse": True,
    "sidebar": True,
    "reduce_motion": False,
    "keys": {},
}


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "fox"


class TuiConfig:
    """Load/save TUI settings from a JSON file."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or config_dir() / "tui.json"
        self.data: dict = json.loads(json.dumps(DEFAULTS))
        self.load()

    # ---------------------------------------------------------------- load --
    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text())
            if not isinstance(raw, dict):
                return
            for key, default in DEFAULTS.items():
                if key in raw:
                    self.data[key] = raw[key]
            if isinstance(raw.get("keys"), dict):
                self.data["keys"] = {**DEFAULT_KEYS, **raw["keys"]}
            else:
                self.data["keys"] = dict(DEFAULT_KEYS)
        except (OSError, json.JSONDecodeError):
            self.data["keys"] = dict(DEFAULT_KEYS)

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.data, indent=2) + "\n")
        except OSError:
            pass  # best-effort: never crash the TUI over config I/O

    # ------------------------------------------------------------- accessors --
    def get(self, key: str, default=None):
        return self.data.get(key, default)

    def set(self, key: str, value) -> None:
        self.data[key] = value
        self.save()

    @property
    def theme(self) -> str:
        return self.get("theme", "opencode-dark")

    @property
    def mouse(self) -> bool:
        return bool(self.get("mouse", True))

    @property
    def sidebar(self) -> bool:
        return bool(self.get("sidebar", True))

    @property
    def reduce_motion(self) -> bool:
        return bool(self.get("reduce_motion", False))

    @property
    def keys(self) -> dict:
        return self.get("keys", DEFAULT_KEYS)

    def key_action(self, key_name: str) -> str | None:
        return self.keys.get(key_name)
