"""Fox TUI application: state, keybindings, command execution, main loop.

Commands run as ``python -m cli <args>`` subprocesses with ``--quiet`` so their
output streams live into the output panel and Ctrl+C interrupts cleanly. The
frame is built by :mod:`.views` from :class:`State` — no side effects in
renderers.
"""

from __future__ import annotations

import os
import queue
import select
import shlex
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field

from . import views
from .components import (LineBuffer, _Completer, Toast, decode_key)
from .config import TuiConfig
from .state import State
from .theme import Capabilities, Theme, detect_capabilities, strip_ansi
from .themes import builtin_names, load_theme

_SUBCOMMANDS = [
    "splash", "version", "status", "doctor", "serve", "graph", "papers",
    "projects", "runs", "run", "experiments", "experiment", "compare",
    "research", "manage", "jobs", "scheduler", "pool", "manual", "tui",
]
_NATIVE = {"exit", "quit", "q", "help", "h", "?", "clear"}
_ACTIONS = {
    "projects": ["list", "new", "show", "rm", "fork"],
    "run": ["show", "report"],
    "experiments": ["list", "start", "run-obfuscation"],
    "experiment": ["show", "ranking"],
    "research": ["list", "status", "report", "build", "synthesize",
                 "experiments", "loop"],
    "manage": ["repos", "status", "link", "commit", "push", "commit-and-push"],
    "papers": ["list", "search", "add"],
    "pool": ["list", "topics", "topics-add", "topics-rm", "import"],
}
_FLAGS = ["--json", "--quiet", "--debug", "--url", "--name", "--hypothesis",
          "--goal-metric", "--goal-target", "--plan", "--metric",
          "--n-rows", "--seed", "--message"]

PALETTE_ACTIONS = [
    "view output", "view logs", "view settings",
    "toggle sidebar", "clear output", "open help", "open theme picker",
    "status", "projects", "graph", "scheduler", "projects list", "manual",
    "quit",
]

HELP_TEXT = """\
  fox — terminal window
  ─────────────────────
  Commands (tab-complete, ↑/↓ history):
    status doctor serve
    projects [list|new|show|rm|fork]
    runs <p> · run <p> <id> [report]
    experiments <p> [start|run-obfuscation]
    experiment <p> <id> [ranking] · compare <p> <a> <b>
    research [list|status|report|build|synthesize|experiments|loop]
    graph · papers [search|add] · jobs · scheduler · pool · manage
    manual [section]
  Keys:
    Enter run · Tab complete · ↑/↓ history · PgUp/PgDn scroll
    Ctrl+L clear · Ctrl+C interrupt · Ctrl+T theme · Ctrl+P palette
    Ctrl+B sidebar · ? help · Ctrl+D quit
  Flags:
    --json (machine output) · --url <server> · --debug
"""


def argv_for(line: str) -> list[str] | None:
    toks = shlex.split(line)
    if not toks or toks[0].lower() in _NATIVE:
        return None
    return toks


def completion_words() -> list[str]:
    return (["help", "exit", "quit", "clear", "tui"] + _SUBCOMMANDS
            + sorted({a for acts in _ACTIONS.values() for a in acts})
            + _FLAGS)


class FoxTUI:
    """The full-screen terminal application."""

    def __init__(self, args, config: TuiConfig | None = None) -> None:
        self.args = args
        self.config = config or TuiConfig()
        self.cap = detect_capabilities()
        self._theme_name = getattr(args, "theme", None) or self.config.theme
        self.theme = load_theme(self._theme_name)
        self.state = State()
        self.state.sidebar_visible = self.config.sidebar
        self.completer = _Completer(completion_words())
        self.outq: queue.Queue | None = None
        self.proc: subprocess.Popen | None = None
        self._w, self._h = 80, 24
        self._resized = False
        self._last_t = 0.0
        self._quit = False
        self._dirty = True

    # -------------------------------------------------------------- sizing --
    def _size(self) -> tuple[int, int]:
        try:
            import shutil
            w, h = shutil.get_terminal_size((80, 24))
        except Exception:  # noqa: BLE001
            w, h = 80, 24
        if w != self._w or h != self._h:
            self._w, self._h = w, h
            self._resized = False
        return self._w, self._h

    # --------------------------------------------------------------- theme --
    def _set_theme(self, name: str) -> None:
        self.theme = load_theme(name)
        self.config.set("theme", name)
        self._toast(f"theme: {name}", "success")

    def _toast(self, message: str, kind: str = "info") -> None:
        self.state.toast = Toast(message, kind,
                                 deadline=time.time() + 2.5)

    # ----------------------------------------------------------- execution --
    def _submit_line(self, line: str) -> None:
        line = line.strip()
        if not line:
            return
        self.state.hist.append(line)
        self.state.hist_idx = len(self.state.hist)
        self.completer.reset()
        first = shlex.split(line)[0].lower()
        if first in ("exit", "quit", "q"):
            self._quit = True
            return
        if first in ("help", "h", "?"):
            self.state.lines.append(HELP_TEXT)
            self.state.scroll = 0
            return
        if first == "clear":
            self.state.lines.clear()
            self.state.scroll = 0
            return
        if first == "tui":
            self.state.lines.append("Already in the terminal window.")
            return
        if first not in _SUBCOMMANDS:
            self.state.lines.append(
                f"  unknown command `{first}`  (try: help)")
            return
        self._launch(argv_for(line))

    def _launch(self, argv: list[str]) -> None:
        self.state.running = True
        self.state.running_label = " ".join(argv)[:40]
        if "--quiet" not in argv and "--json" not in argv:
            argv = ["--quiet"] + argv
        self.state.lines.append("─ " + self.state.running_label + " ─")
        q: queue.Queue = queue.Queue()
        self.outq = q
        try:
            proc = subprocess.Popen(
                [sys.executable, "-m", "cli"] + argv,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                env=dict(os.environ), cwd=os.getcwd(),
                bufsize=1, text=True)
        except OSError as e:
            self._finish_run(f"cannot launch: {e}", code=1, ok=False)
            return
        self.proc = proc

        def pump() -> None:
            try:
                assert proc.stdout is not None
                for ln in proc.stdout:
                    q.put(ln)
                proc.wait()
            finally:
                q.put(None)

        threading.Thread(target=pump, daemon=True).start()

    def _finish_run(self, label: str, code: int | None, ok: bool = True) -> None:
        self.state.running = False
        self.state.running_label = ""
        self.proc = None
        self.outq = None
        self.state.status_msg = f"exit {code}" if code is not None else "done"
        self.state.status_ok = ok
        self.state.log(label, code)
        self._toast(self.state.status_msg, "success" if ok else "error")

    def _interrupt(self) -> None:
        if self.state.running and self.proc:
            try:
                self.proc.send_signal(signal.SIGINT)
            except OSError:
                pass
            return
        if self.state.buf.text:
            self.state.buf.clear()
        else:
            self._quit = True

    def _drain(self) -> None:
        if self.outq is None:
            return
        added = False
        try:
            while True:
                item = self.outq.get_nowait()
                if item is None:
                    code = self.proc.poll() if self.proc else None
                    ok = code in (None, 0)
                    self._finish_run(self.state.running_label, code, ok)
                    break
                self.state.lines.append(item.rstrip("\n"))
                added = True
        except queue.Empty:
            pass
        if added:
            self.state.scroll = 0
            self._dirty = True

    # ------------------------------------------------------------- actions --
    def _run_action(self, action: str) -> None:
        if action == "quit":
            self._quit = True
        elif action == "toggle_sidebar":
            self.state.sidebar_visible = not self.state.sidebar_visible
            self.config.set("sidebar", self.state.sidebar_visible)
        elif action == "clear_output":
            self.state.lines.clear(); self.state.scroll = 0
        elif action == "clear_line":
            self.state.buf.clear()
        elif action == "delete_word":
            self.state.buf.delete_word()
        elif action == "focus_next":
            self.state.focus = ("input" if self.state.focus == "sidebar"
                                else "sidebar")
        elif action == "help":
            self.state.overlay = "help" if not self.state.overlay else None
        elif action == "theme_picker":
            self.state.overlay = ("theme" if not self.state.overlay else None)
            if self.state.overlay == "theme":
                self.state.overlay_idx = 0
                self.state.theme_idx = builtin_names().index(self.theme.name) \
                    if self.theme.name in builtin_names() else 0
        elif action == "palette":
            self.state.overlay = ("palette" if not self.state.overlay else None)
            self.state.overlay_query = ""
            self.state.overlay_idx = 0
        elif action == "close":
            self.state.overlay = None
        elif action == "scroll_up":
            self.state.scroll = min(self.state.scroll + max(self._h - 4, 1),
                                    len(self.state.lines))
        elif action == "scroll_down":
            self.state.scroll = max(self.state.scroll - max(self._h - 4, 1), 0)
        elif action == "top":
            self.state.scroll = len(self.state.lines)
        elif action == "bottom":
            self.state.scroll = 0
        elif action.startswith("view:"):
            self.state.view = action[5:]
            self.state.overlay = None
            self.state.scroll = 0
        elif action.startswith("run:"):
            self._submit_line(action[4:])
        elif action.startswith("palette:"):
            self._palette_execute(action[8:])

    def _palette_execute(self, command: str) -> None:
        self.state.overlay = None
        if command.startswith("view "):
            self.state.view = command[5:]
        elif command == "toggle sidebar":
            self.state.sidebar_visible = not self.state.sidebar_visible
            self.config.set("sidebar", self.state.sidebar_visible)
        elif command == "clear output":
            self.state.lines.clear(); self.state.scroll = 0
        elif command == "open help":
            self.state.overlay = "help"
        elif command == "open theme picker":
            self.state.overlay = "theme"
        elif command == "quit":
            self._quit = True
        elif command.startswith("view "):
            pass
        else:
            self._submit_line(command)

    # ---------------------------------------------------------------- keys --
    def _filter_palette(self) -> list[str]:
        q = self.state.overlay_query.lower()
        return [c for c in PALETTE_ACTIONS if all(ch in c for ch in q)]

    def _on_enter(self) -> None:
        if self.state.overlay == "theme":
            names = builtin_names()
            self._set_theme(names[self.state.overlay_idx])
            self.state.overlay = None
            return
        if self.state.overlay == "palette":
            results = self._filter_palette() or PALETTE_ACTIONS
            if results:
                self._palette_execute(results[self.state.overlay_idx])
            return
        if self.state.overlay == "help":
            self.state.overlay = None
            return
        self._submit_line(self.state.buf.text)
        self.state.buf.clear()

    def _on_key(self, key) -> None:
        if key is None:
            return
        self._dirty = True
        if isinstance(key, _MouseEvent):
            self._on_mouse(key)
            return
        if self.state.running:
            if isinstance(key, str):
                return
            if key.value in ("ctrl_c", "ctrl_d"):
                self._interrupt()
            return
        if isinstance(key, str):
            if self.state.overlay == "palette":
                self.state.overlay_query += key
                self.state.overlay_idx = 0
            else:
                self.state.buf.insert(key)
                self.completer.reset()
            return
        k = key.value
        action = self.config.key_action(k)
        # overlay-aware movement
        if self.state.overlay:
            self._overlay_key(k)
            return
        if action == "submit":
            self._on_enter()
        elif action == "quit":
            self._interrupt()
        elif action == "tab":
            self.completer.next(self.state.buf)
        elif action in ("up", "down") and self.state.focus == "sidebar":
            items = [i for i, it in enumerate(views.SIDEBAR) if it[0]]
            if items:
                idx = items.index(self.state.sidebar_idx) \
                    if self.state.sidebar_idx in items else 0
                idx = (idx - 1 if action == "up" else idx + 1) % len(items)
                self.state.sidebar_idx = items[idx]
        elif action in ("up", "down", "left", "right", "home", "end",
                        "backspace", "del", "clear_line", "delete_word"):
            self._edit_key(action, k)
        elif action in ("scroll_up", "scroll_down", "top", "bottom"):
            self._run_action(action)
        elif action:
            self._run_action(action)

    def _on_mouse(self, ev: "_MouseEvent") -> None:
        if self.state.overlay:
            return
        if ev.btn in (64, 65):  # scroll wheel
            step = max(self._h - 4, 1)
            if ev.btn == 64:  # up
                self.state.scroll = min(self.state.scroll + step,
                                        len(self.state.lines))
            else:
                self.state.scroll = max(self.state.scroll - step, 0)
            return
        if ev.btn == 0:  # left click
            w, h = self._size()
            if ev.y >= h - 1:  # input row
                self.state.focus = "input"
            elif ev.x < views.sidebar_width(w) and self.state.sidebar_visible:
                self.state.focus = "sidebar"

    def _edit_key(self, action: str, k: str) -> None:
        if action == "up":
            if self.state.hist:
                self.state.hist_idx = max(0, self.state.hist_idx - 1)
                self.state.buf.set(self.state.hist[self.state.hist_idx])
        elif action == "down":
            if self.state.hist and self.state.hist_idx < len(self.state.hist) - 1:
                self.state.hist_idx += 1
                self.state.buf.set(self.state.hist[self.state.hist_idx])
            else:
                self.state.hist_idx = len(self.state.hist)
                self.state.buf.clear()
        elif action == "left":
            self.state.buf.left()
        elif action == "right":
            self.state.buf.right()
        elif action == "home":
            self.state.buf.home()
        elif action == "end":
            self.state.buf.end()
        elif action == "backspace":
            self.state.buf.backspace()
        elif action == "del":
            self.state.buf.delete()
        elif action == "clear_line":
            self.state.buf.clear()
        elif action == "delete_word":
            self.state.buf.delete_word()
        self.completer.reset()

    def _overlay_key(self, k: str) -> None:
        overlay = self.state.overlay
        if k in ("esc",):
            self.state.overlay = None
            return
        if k == "enter":
            self._on_enter()
            return
        if k == "backspace" and overlay == "palette":
            self.state.overlay_query = self.state.overlay_query[:-1]
            self.state.overlay_idx = 0
            return
        if k in ("up", "down"):
            if overlay in ("theme", "palette", "confirm"):
                n = self._overlay_count()
                if k == "up":
                    self.state.overlay_idx = (self.state.overlay_idx - 1) % n
                else:
                    self.state.overlay_idx = (self.state.overlay_idx + 1) % n
            return

    def _overlay_count(self) -> int:
        if self.state.overlay == "theme":
            return len(builtin_names())
        if self.state.overlay == "palette":
            return max(len(self._filter_palette()), 1)
        return 1

    # --------------------------------------------------------------- input --
    def _read_keys(self, data: bytes) -> list:
        keys = []
        i = 0
        while i < len(data):
            b = data[i]
            if b == 0x1B:
                if data[i + 1 : i + 3] == b"[<":
                    # SGR mouse sequence: \x1b[<btn;x;yM
                    j = data.find(b"M", i + 3)
                    if j == -1:
                        j = data.find(b"m", i + 3)
                    if j != -1:
                        keys.append(_mouse_event(data[i : j + 1]))
                        i = j + 1
                    else:
                        keys.append(decode_key(b"\x1b"))
                        i += 1
                elif i + 1 < len(data) and data[i + 1] in b"\x1b[O":
                    j = i + 2
                    while j < len(data) and not chr(data[j]).isalpha() \
                            and data[j] != 0x7E:
                        j += 1
                    j = min(j + 1, len(data))
                    keys.append(decode_key(data[i:j]))
                    i = j
                else:
                    keys.append(decode_key(b"\x1b"))
                    i += 1
            else:
                n = 1
                while n < len(data) - i and (data[i + n] & 0xC0) == 0x80:
                    n += 1
                keys.append(decode_key(data[i : i + n]))
                i += n
        return keys

    # ----------------------------------------------------------------- run --
    def run(self) -> int:
        if not self.cap.is_tty:
            return self._fallback()
        fd = sys.stdin.fileno()
        old = self._save_termios(fd)
        try:
            self._set_raw(fd)
            signal.signal(signal.SIGWINCH, lambda *_a: self._winch())
            sys.stdout.write("\x1b[?1049h\x1b[2J")  # alternate screen
            if self.config.mouse:
                sys.stdout.write("\x1b[?1006h\x1b[?1002h")  # SGR mouse
            self.state.lines.extend(self._banner().split("\n"))
            self._loop(fd)
        finally:
            if self.config.mouse:
                sys.stdout.write("\x1b[?1002l\x1b[?1006l")
            self._restore_termios(fd, old)
            signal.signal(signal.SIGWINCH, signal.SIG_DFL)
            sys.stdout.write("\x1b[?25h\x1b[0m\x1b[?1049l\n")
            sys.stdout.flush()
        return 0

    def _loop(self, fd: int) -> None:
        while not self._quit:
            try:
                r, _, _ = select.select([sys.stdin], [], [], 0.05)
                if r:
                    data = os.read(fd, 4096)
                    if not data:
                        break
                    for k in self._read_keys(data):
                        self._on_key(k)
            except KeyboardInterrupt:
                self._interrupt()
            except (OSError, ValueError):
                break
            self._drain()
            # redraw only when something changed (or the spinner is animating)
            if self._dirty or self.state.running:
                self._draw()
                self._dirty = False
        sys.stdout.write("\x1b[?25h")
        sys.stdout.flush()

    def _winch(self) -> None:
        self._resized = True
        self._dirty = True

    def _draw(self) -> None:
        w, h = self._size()
        self._last_t = time.time()
        rows = views.render_frame(self.state, self.theme, self.cap, w, h,
                                  t=self._last_t)
        # Position each row explicitly — raw mode disables OPOST, so `\n` is
        # LF-only and would shift every line off column 0. Absolute positioning
        # also keeps redraws flicker-free (no terminal scroll on rewrite).
        parts = ["\x1b[?25l"]
        for i, row in enumerate(rows[:h], start=1):
            parts.append(f"\x1b[{i};1H{row}")
        if self.state.running or self.state.overlay:
            parts.append("\x1b[?25l")
        else:
            # input line is the second-to-last row (status bar is last)
            prompt_w = len(strip_ansi("fox > "))  # 6
            col = min(prompt_w + self.state.buf.pos + 1, w)
            parts.append(f"\x1b[{max(h - 1, 1)};{col}H\x1b[?25h")
        sys.stdout.write("".join(parts))
        sys.stdout.flush()

    # ------------------------------------------------------------ raw mode --
    @staticmethod
    def _save_termios(fd: int):
        import termios
        try:
            return termios.tcgetattr(fd)
        except (termios.error, OSError):  # pragma: no cover
            return None

    @staticmethod
    def _set_raw(fd: int) -> None:
        import tty
        try:
            tty.setraw(fd)
        except (tty.error, OSError):  # pragma: no cover
            pass

    @staticmethod
    def _restore_termios(fd: int, old) -> None:
        import termios
        try:
            if old is not None:
                termios.tcsetattr(fd, termios.TCSADRAIN, old)
        except (termios.error, OSError):  # pragma: no cover
            pass

    @staticmethod
    def _banner() -> str:
        try:
            from .splash import render_splash_panel  # type: ignore
            from ..commands import VERSION
            return render_splash_panel(VERSION)
        except Exception:  # noqa: BLE001
            return "fox"

    def _fallback(self) -> int:
        from ..interactive import run_repl
        return run_repl(self.args)


def _mouse_event(seq: bytes) -> "_MouseEvent":
    """Parse an SGR mouse sequence `\x1b[<btn;col;rowM` into an event."""
    import re
    m = re.search(rb"<(\d+);(\d+);(\d+)[Mm]", seq)
    if not m:
        return _MouseEvent(btn=0, x=0, y=0)
    return _MouseEvent(btn=int(m.group(1)), x=int(m.group(2)),
                       y=int(m.group(3)))


@dataclass
class _MouseEvent:
    btn: int
    x: int
    y: int


def render_preview(args) -> int:
    """Static frame render for visual verification / docs (no raw mode)."""
    config = TuiConfig()
    app = FoxTUI(args, config=config)
    app.cap = Capabilities(truecolor=True, color256=True, basic=True,
                           is_tty=True)
    theme_name = getattr(args, "theme", None) or config.theme
    app.theme = load_theme(theme_name)
    view = getattr(args, "view", "output")
    w = int(getattr(args, "width", 88))
    h = int(getattr(args, "height", 26))
    st = app.state
    st.view = view if view in ("output", "logs", "settings") else "output"
    st.lines = [
        "╭ fox ╮ status ───────────────────────────────╮",
        "│  server: running  http://127.0.0.1:8765     │",
        "│  model: qwen3.6:latest                      │",
        "│  research scenarios: 2 total                │",
        "╰─────────────────────────────────────────────╯",
        "",
        "╭ experiments · obf-bank-demo ────────────────╮",
        "│  id  name            status  started        │",
        "│  1   obfuscation (bank)  active  19:40       │",
        "│  2   cli smoke test      active  23:41       │",
        "╰─────────────────────────────────────────────╯",
    ]
    st.logs = [("23:41", "status", 0), ("23:42", "projects list", 0),
               ("23:43", "research build autonomous-agents-security", 130)]
    st.buf.set("experiments obf-bank-demo")
    st.theme_idx = builtin_names().index(theme_name) \
        if theme_name in builtin_names() else 0
    if view == "help":
        st.overlay = "help"
    elif view == "theme":
        st.overlay = "theme"
    frame = views.render_frame(st, app.theme, app.cap, w, h)
    sys.stdout.write("\x1b[0m" + "\n".join(frame) + "\x1b[0m\n")
    return 0
