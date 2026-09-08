from __future__ import annotations

import os
from pathlib import Path

from hive.config import load_config, CONFIG_FILE
from hive.llm import get_provider
from hive.machine import WORKSPACE, MACHINE_DIR
from hive.machine.tools import list_files, read_file
from hive.machine.agent import run_task, list_history

try:
    from textual.app import App, ComposeResult
    from textual.containers import Horizontal, Vertical
    from textual.widgets import Header, Footer, Input, Button, Static, RichLog, ListView, ListItem, Label, Tree
    from textual.binding import Binding
    from textual import work, on
    _HAS_TEXTUAL = True
except ImportError:
    _HAS_TEXTUAL = False

HELP = """**Hive-Machine** — local Perplexity Computer (Ollama / LM Studio only)

- **Task input** → agent loops via sandboxed `~/.hive/machine/workspace` (file, bash, python, web_fetch/search)
- **Tools:** `list_files` `read_file` `write_file` `run_bash` `run_python` `web_fetch` `web_search`
- **Left:** file tree of workspace • **Center:** agent chat/computer output • **Right:** preview of selected file
- Keys: `q` quit, `f` focus input, `r` refresh files, `h` history, `?` help
- Workspace is jailed — no escape above `~/.hive/machine/workspace`
"""

if _HAS_TEXTUAL:
    class HiveMachineApp(App):
        CSS = """
        Screen { layout: vertical; }
        #main { height: 1fr; }
        #left { width: 30; min-width: 22; border: tall $primary; }
        #center { width: 1fr; border: tall $secondary; }
        #right { width: 40; min-width: 24; border: tall $accent; }
        #input-row { height: 5; dock: bottom; }
        #output { height: 1fr; }
        #preview { height: 1fr; }
        #filelist { height: 1fr; }
        """
        TITLE = "Hive-Machine — local Perplexity Computer"
        BINDINGS = [
            Binding("q", "quit", "Quit"),
            Binding("f", "focus_input", "Focus input"),
            Binding("r", "refresh_files", "Refresh"),
            Binding("h", "show_history", "History"),
            Binding("question_mark", "help", "Help"),
        ]

        def __init__(self):
            super().__init__()
            self.cfg = load_config()

        def compose(self) -> ComposeResult:
            yield Header(show_clock=True)
            with Horizontal(id="main"):
                with Vertical(id="left"):
                    yield Static(" Workspace", id="left-title")
                    yield Static("", id="filelist")
                    yield Button("Refresh", id="refresh", variant="default")
                with Vertical(id="center"):
                    yield Static(HELP, id="help")
                    yield RichLog(id="output", highlight=True, markup=True, wrap=True)
                with Vertical(id="right"):
                    yield Static(" Preview", id="prev-title")
                    yield RichLog(id="preview", highlight=True, markup=True, wrap=True)
            with Horizontal(id="input-row"):
                yield Input(placeholder="Task — e.g. 'fetch https://example.com and save summary to report.md + run python to analyze'", id="task")
                yield Input(placeholder="steps", value="12", id="steps", compact=True)
                yield Button("Run", id="run", variant="primary")
            yield Footer()

        def on_mount(self) -> None:
            self.query_one("#task").focus()
            self._refresh_files()
            self._refresh_status()
            self.query_one("#output", RichLog).write("[dim]Welcome to Hive-Machine — local computer for your LLM. Type a task and press Run.[/dim]")
            self.query_one("#preview", RichLog).write("[dim]Preview: select a file in workspace or run a task that writes files.[/dim]")

        def _refresh_status(self):
            prov = get_provider(self.cfg)
            ok, msg = prov.health()
            try:
                self.query_one("#help", Static).update(HELP + f"\n[dim]LLM: {prov.name}@{prov.base_url} {'✓' if ok else '✗ '+msg[:50]} | cfg: {CONFIG_FILE} | ws: {WORKSPACE}[/dim]")
            except Exception:
                pass

        def _refresh_files(self):
            try:
                txt = list_files(".", recursive=False)
                # Render as simple list; Tree would be richer but RichLog is simpler
                self.query_one("#filelist", Static).update(f"[dim]{txt[:2000] or '(empty workspace)'}[/dim]")
                # preview first file if exists
                ws = WORKSPACE
                ws.mkdir(parents=True, exist_ok=True)
                files = [p for p in ws.iterdir() if p.is_file()]
                if files:
                    preview = read_file(str(files[0].relative_to(WORKSPACE)), 3000)
                    self.query_one("#preview", RichLog).clear()
                    self.query_one("#preview", RichLog).write(f"**{files[0].name}**\n\n{preview[:3000]}")
            except Exception as e:
                self.query_one("#filelist", Static).update(f"[red]{e}[/red]")

        @on(Button.Pressed, "#refresh")
        def on_refresh(self, e: Button.Pressed):
            self._refresh_files()

        @on(Button.Pressed, "#run")
        def on_run(self, e: Button.Pressed):
            self._dispatch()

        @on(Input.Submitted, "#task")
        def on_submit(self, e: Input.Submitted):
            self._dispatch()

        def action_focus_input(self):
            self.query_one("#task").focus()

        def action_refresh_files(self):
            self._refresh_files()

        def action_show_history(self):
            self._show_history()

        def action_help(self):
            self.query_one("#output", RichLog).write(HELP)

        def _dispatch(self):
            task = self.query_one("#task", Input).value.strip()
            if not task:
                self.query_one("#output", RichLog).write("[yellow]Enter a task[/yellow]")
                return
            steps_raw = self.query_one("#steps", Input).value.strip()
            try:
                steps = int(steps_raw) if steps_raw else 12
            except ValueError:
                steps = 12
            log = self.query_one("#output", RichLog)
            log.write(f"[bold cyan]▶ task: {task} (max_steps={steps})[/bold cyan]")
            self._run_task(task, steps)

        @work(thread=True)
        def _run_task(self, task: str, max_steps: int):
            try:
                self.call_from_thread(self.query_one("#output", RichLog).write, f"[dim]Hive-Machine working via {get_provider(self.cfg).name}…[/dim]")
                out = run_task(task, self.cfg, max_steps=max_steps, verbose=False)
                self.call_from_thread(self.query_one("#output", RichLog).write, out)
                self.call_from_thread(self._refresh_files)
            except Exception as e:
                self.call_from_thread(self.query_one("#output", RichLog).write, f"[red]Machine error: {e}[/red]")

        @work(thread=True)
        def _show_history(self):
            try:
                rows = list_history(20)
                if not rows:
                    self.call_from_thread(self.query_one("#output", RichLog).write, "[dim]No machine runs yet[/dim]")
                    return
                lines = ["**Machine history:**"]
                import datetime
                for rid, task, created, steps in rows:
                    dt = datetime.datetime.fromtimestamp(created).strftime("%Y-%m-%d %H:%M") if created else ""
                    lines.append(f"- #{rid} [{dt}] steps={steps} {task[:80]}")
                self.call_from_thread(self.query_one("#output", RichLog).write, "\n".join(lines))
            except Exception as e:
                self.call_from_thread(self.query_one("#output", RichLog).write, f"[red]History error: {e}[/red]")

else:
    class HiveMachineApp:  # type: ignore
        def __init__(self, *a, **kw):
            raise RuntimeError("Textual not installed. pip install textual or pip install -e \".[tui]\"")

def run():
    if not _HAS_TEXTUAL:
        print("Textual required: pip install textual or pip install -e \".[tui]\"")
        raise SystemExit(1)
    app = HiveMachineApp()
    app.run()
