"""Fox TUI — an Opencode-style full-screen terminal window.

Entry point: ``fox tui`` or ``fox`` with no arguments (falls back to the plain
``>`` REPL when stdin/stdout is not a TTY). Themes are semantic-token driven
(``--theme NAME``), configurable via ``~/.config/fox/tui.json``, and switchable
in-app with Ctrl+T.

Preview mode for docs/tests: ``fox tui --render-preview [--theme NAME]
[--view output|logs|settings|help|theme]``.
"""

from __future__ import annotations

from .app import (HELP_TEXT, PALETTE_ACTIONS, FoxTUI, argv_for,
                  completion_words, render_preview)
from .components import LineBuffer, _Completer, _Key, decode_key, _is_printable
from .config import TuiConfig
from .state import State
from .theme import Capabilities, Theme, detect_capabilities
from .themes import builtin_names, load_theme
from .views import render_frame, render_help, render_palette, render_theme_picker


def run_tui(args) -> int:
    """Launch the full-screen window (or fall back to the REPL)."""
    if getattr(args, "render_preview", False):
        return render_preview(args)
    return FoxTUI(args).run()


__all__ = [
    "run_tui", "FoxTUI", "State", "Theme", "Capabilities", "detect_capabilities",
    "LineBuffer", "_Completer", "_Key", "decode_key", "_is_printable",
    "argv_for", "completion_words", "PALETTE_ACTIONS", "HELP_TEXT",
    "builtin_names", "load_theme", "TuiConfig", "render_preview",
]
