"""Tests for the Fox TUI package: theme engine, themes, config, components,
key/action dispatch, and static preview rendering. No raw-terminal IO."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from cli.tui import app as tui_app
from cli.tui import config as tui_config
from cli.tui import theme as theme_mod
from cli.tui import views
from cli.tui.components import (LineBuffer, _Completer, _Key, _is_printable,
                                decode_key)
from cli.tui.config import TuiConfig
from cli.tui.theme import (Capabilities, Theme, detect_capabilities,
                           hex_to_rgb, rgb_to_256)
from cli.tui.themes import builtin_names, load_theme


class ThemeEngineTests(unittest.TestCase):

    def test_hex_to_rgb(self):
        self.assertEqual(hex_to_rgb("#0b0e14"), (11, 14, 20))
        self.assertEqual(hex_to_rgb("#abc"), (0xAA, 0xBB, 0xCC))

    def test_rgb_to_256_within_range(self):
        for rgb in [(11, 14, 20), (255, 255, 255), (0, 0, 0), (200, 30, 30)]:
            self.assertTrue(16 <= rgb_to_256(*rgb) <= 255)

    def test_capabilities_detection(self):
        with patch.dict("os.environ", {"COLORTERM": "truecolor",
                                       "TERM": "xterm-256color"}):
            cap = detect_capabilities()
            self.assertTrue(cap.truecolor)
            self.assertTrue(cap.color256)
        with patch.dict("os.environ", {"COLORTERM": "", "TERM": "xterm"}):
            cap = detect_capabilities()
            self.assertFalse(cap.truecolor)
            self.assertFalse(cap.color256)
            self.assertTrue(cap.basic)

    def test_fg_levels(self):
        th = Theme("t", "dark", {"accent": "#7aa2f7"})
        cap3 = Capabilities(truecolor=True, color256=True, basic=True)
        cap2 = Capabilities(truecolor=False, color256=True, basic=True)
        cap1 = Capabilities(truecolor=False, color256=False, basic=True)
        self.assertIn("38;2;", th.fg("accent", cap3))
        self.assertIn("38;5;", th.fg("accent", cap2))
        self.assertTrue(th.fg("accent", cap1).startswith("\x1b["))
        self.assertTrue(th.fg("accent", cap1).endswith("m"))

    def test_paint_resets(self):
        th = Theme("t", "dark", {"accent": "#ffffff"})
        out = th.paint("accent", "hi", Capabilities(truecolor=True,
                                                    color256=True, basic=True))
        self.assertTrue(out.startswith("\x1b["))
        self.assertTrue(out.endswith("\x1b[0m"))


class BuiltinThemeTests(unittest.TestCase):

    def test_required_themes_exist(self):
        names = builtin_names()
        for required in ("opencode-dark", "opencode-light",
                         "opencode-midnight", "high-contrast-dark",
                         "high-contrast-light"):
            self.assertIn(required, names)

    def test_all_themes_complete_and_valid(self):
        for name in builtin_names():
            th = load_theme(name)
            self.assertEqual(th.missing(), [], f"{name} missing tokens")
            self.assertEqual(th.validate(), [], f"{name} invalid colors")

    def test_dark_light_types(self):
        self.assertEqual(load_theme("opencode-dark").type, "dark")
        self.assertEqual(load_theme("opencode-light").type, "light")

    def test_unknown_theme_falls_back(self):
        th = load_theme("nope")
        self.assertEqual(th.name, "opencode-dark")


class ConfigTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = Path(self.tmp) / "tui.json"

    def test_defaults_when_missing(self):
        cfg = TuiConfig(self.path)
        self.assertEqual(cfg.theme, "opencode-dark")
        self.assertTrue(cfg.mouse)
        self.assertTrue(cfg.sidebar)
        self.assertEqual(cfg.key_action("ctrl_t"), "theme_picker")

    def test_roundtrip(self):
        cfg = TuiConfig(self.path)
        cfg.set("theme", "opencode-light")
        cfg.set("sidebar", False)
        cfg2 = TuiConfig(self.path)
        self.assertEqual(cfg2.theme, "opencode-light")
        self.assertFalse(cfg2.sidebar)

    def test_bad_file_ignored(self):
        self.path.write_text("{ not json")
        cfg = TuiConfig(self.path)
        self.assertEqual(cfg.theme, "opencode-dark")

    def test_key_override(self):
        self.path.write_text(json.dumps({"keys": {"ctrl_t": "help"}}))
        cfg = TuiConfig(self.path)
        self.assertEqual(cfg.key_action("ctrl_t"), "help")


class LineBufferTests(unittest.TestCase):

    def test_insert_backspace(self):
        b = LineBuffer()
        for ch in "abc":
            b.insert(ch)
        self.assertEqual(b.text, "abc")
        self.assertEqual(b.pos, 3)
        b.backspace()
        self.assertEqual(b.text, "ab")

    def test_cursor(self):
        b = LineBuffer("hello")
        b.home()
        b.right(2)
        b.insert("X")
        self.assertEqual(b.text, "heXllo")
        b.end()
        b.left()
        b.delete()
        self.assertEqual(b.text, "heXll")

    def test_delete_word(self):
        b = LineBuffer("fox experiments proj")
        b.end()
        b.delete_word()
        self.assertEqual(b.text, "fox experiments ")

    def test_word_before(self):
        b = LineBuffer("fox exp")
        b.end()
        self.assertEqual(b.word_before(), "exp")


class CompleterTests(unittest.TestCase):

    def test_single(self):
        b = LineBuffer("fox exp")
        b.end()
        comp = _Completer(tui_app.completion_words())
        comp.next(b)
        self.assertEqual(b.text, "fox experiments")

    def test_cycles(self):
        b = LineBuffer("run")
        b.end()
        comp = _Completer(tui_app.completion_words())
        seen = {comp.words and b.text}
        for _ in range(20):
            comp.next(b)
            seen.add(b.text)
        self.assertGreater(len(seen), 2)

    def test_none_no_crash(self):
        b = LineBuffer("zzz")
        b.end()
        _Completer([]).next(b)
        self.assertEqual(b.text, "zzz")


class KeyDecodeTests(unittest.TestCase):

    def test_arrows_and_edits(self):
        self.assertEqual(decode_key(b"\x1b[A").value, "up")
        self.assertEqual(decode_key(b"\x1b[6~").value, "pgdn")
        self.assertEqual(decode_key(b"\x1b[H").value, "home")
        self.assertEqual(decode_key(b"\x03").value, "ctrl_c")
        self.assertEqual(decode_key(b"\x7f").value, "backspace")
        self.assertEqual(decode_key(b"\r").value, "enter")

    def test_printable(self):
        self.assertEqual(decode_key(b"a"), "a")
        self.assertEqual(decode_key("é".encode()), "é")
        self.assertIsNone(decode_key(b""))
        self.assertTrue(_is_printable(b"x"))
        self.assertFalse(_is_printable(b"\x1b[A"))


class ArgvMappingTests(unittest.TestCase):

    def test_native(self):
        for line in ("exit", "quit", "help", "clear"):
            self.assertIsNone(tui_app.argv_for(line))

    def test_commands(self):
        self.assertEqual(tui_app.argv_for("status"), ["status"])
        self.assertEqual(tui_app.argv_for("run p 8 report"),
                         ["run", "p", "8", "report"])
        self.assertIsNone(tui_app.argv_for(""))


class AppDispatchTests(unittest.TestCase):

    def _app(self, cfg=None):
        args = SimpleNamespace(theme=None)
        cfg = cfg or TuiConfig(Path(tempfile.mkdtemp()) / "tui.json")
        return tui_app.FoxTUI(args, config=cfg)

    def test_theme_picker_apply_persists(self):
        app = self._app()
        app._run_action("theme_picker")
        self.assertEqual(app.state.overlay, "theme")
        idx = builtin_names().index("opencode-light")
        app.state.overlay_idx = idx
        app._on_enter()
        self.assertEqual(app.theme.name, "opencode-light")
        self.assertIsNone(app.state.overlay)
        self.assertEqual(app.config.theme, "opencode-light")

    def test_toggle_sidebar(self):
        app = self._app()
        app._run_action("toggle_sidebar")
        self.assertFalse(app.state.sidebar_visible)
        app._run_action("toggle_sidebar")
        self.assertTrue(app.state.sidebar_visible)

    def test_switch_views(self):
        app = self._app()
        app._run_action("view:logs")
        self.assertEqual(app.state.view, "logs")
        app._run_action("view:settings")
        self.assertEqual(app.state.view, "settings")

    def test_submit_native(self):
        app = self._app()
        app._submit_line("clear")
        app._submit_line("help")
        app._submit_line("frobnicate")
        self.assertTrue(any("unknown command" in l for l in app.state.lines))
        self.assertIn("fox — terminal window",
                      "".join(app.state.lines))

    def test_exit(self):
        app = self._app()
        app._submit_line("exit")
        self.assertTrue(app._quit)

    def test_unknown_command_no_launch(self):
        app = self._app()
        with patch.object(app, "_launch") as m:
            app._submit_line("frobnicate")
            m.assert_not_called()

    def test_palette_filter(self):
        app = self._app()
        app.state.overlay = "palette"
        app.state.overlay_query = "theme"
        results = app._filter_palette()
        self.assertIn("open theme picker", results)

    def test_mouse_scroll(self):
        app = self._app()
        app.state.lines = [f"line {i}" for i in range(50)]
        app._on_mouse(tui_app._MouseEvent(btn=64, x=5, y=5))  # wheel up
        self.assertGreater(app.state.scroll, 0)

    def test_read_keys_arrow_and_enter(self):
        app = self._app()
        keys = app._read_keys(b"abc\x1b[B\r")
        values = [k.value if isinstance(k, _Key) else k for k in keys]
        self.assertEqual(values, ["a", "b", "c", "down", "enter"])

    def test_read_keys_home_end_del(self):
        app = self._app()
        values = [k.value if isinstance(k, _Key) else k
                  for k in app._read_keys(b"\x1b[H\x1b[F\x1b[3~")]
        self.assertEqual(values, ["home", "end", "del"])

    def test_read_keys_mouse(self):
        app = self._app()
        keys = app._read_keys(b"\x1b[<64;10;5M")
        self.assertEqual(len(keys), 1)
        self.assertIsInstance(keys[0], tui_app._MouseEvent)
        self.assertEqual(keys[0].btn, 64)

    def test_input_editing_via_key(self):
        app = self._app()
        for ch in ("r", "u", "n"):
            app._on_key(ch)
        self.assertEqual(app.state.buf.text, "run")
        app._on_key(_Key("backspace"))
        self.assertEqual(app.state.buf.text, "ru")


class RenderPreviewTests(unittest.TestCase):

    def _preview(self, view="output", theme="opencode-dark"):
        args = SimpleNamespace(theme=theme, view=view, width=80, height=22,
                               render_preview=True)
        buf = io.StringIO()
        with redirect_stdout(buf):
            tui_app.render_preview(args)
        return buf.getvalue()

    def test_frame_has_chrome(self):
        out = self._preview()
        self.assertIn("fox", out)
        self.assertIn("opencode-dark", out)  # theme in status bar
        self.assertIn("output", out)         # view tab / header mode
        self.assertIn("status", out)         # sample panel content

    def test_theme_picker_preview(self):
        out = self._preview(view="theme")
        self.assertIn("theme picker", out)
        self.assertIn("opencode-dark", out)

    def test_help_preview(self):
        out = self._preview(view="help")
        self.assertIn("Ctrl+T", out)

    def test_theme_switch_affects_frame(self):
        dark = self._preview(theme="opencode-dark")
        light = self._preview(theme="opencode-light")
        self.assertNotEqual(dark, light)


class FrameLayoutTests(unittest.TestCase):
    """render_frame must return exactly h padded rows (flicker fix)."""

    def _rows(self, view="output", overlay=None, sidebar=True, h=20, w=80):
        args = SimpleNamespace(theme="opencode-dark")
        app = tui_app.FoxTUI(args, config=TuiConfig(
            Path(tempfile.mkdtemp()) / "tui.json"))
        st = app.state
        st.view = view
        st.sidebar_visible = sidebar
        st.lines = ["line one", "line two" * 20]
        st.logs = [("12:00", "status", 0)]
        st.overlay = overlay
        return views.render_frame(st, app.theme, app.cap, w, h)

    def test_exact_height_rows(self):
        for view in ("output", "logs", "settings"):
            rows = self._rows(view=view, h=22, w=80)
            self.assertEqual(len(rows), 22, view)
            self.assertEqual(len(theme_mod.strip_ansi(rows[0])), 80)

    def test_no_overlong_rows(self):
        rows = self._rows(h=20, w=64)
        for row in rows:
            self.assertLessEqual(len(theme_mod.strip_ansi(row)), 64)

    def test_overlay_rows_full_height(self):
        rows = self._rows(overlay="help", h=24, w=80)
        self.assertEqual(len(rows), 24)

    def test_no_sidebar_rows_full_width(self):
        rows = self._rows(sidebar=False, h=20, w=70)
        self.assertEqual(len(rows), 20)
        for row in rows:
            self.assertEqual(len(theme_mod.strip_ansi(row)), 70)

    def test_truncate_preserves_width(self):
        from cli.tui.components import _truncate
        for width in (10, 40, 80):
            long = "x" * (width * 3)
            self.assertEqual(len(theme_mod.strip_ansi(_truncate(long, width))),
                             width)
        colored = "\x1b[38;2;1;2;3m" + "y" * 50 + "\x1b[0m"
        self.assertEqual(len(theme_mod.strip_ansi(_truncate(colored, 20))), 20)


if __name__ == "__main__":
    unittest.main()
