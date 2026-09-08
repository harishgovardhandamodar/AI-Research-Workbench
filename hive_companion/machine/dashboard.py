"""Analysis dashboard — auditable proofs for Hive-Machine.

Tracks network usage, data outflow, file/folder usages, command history
with severity and impact, and renders CLI tables + Textual dashboard.

All events from hive/machine/audit.py (sqlite chain-hashed).
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import List

from rich.console import Console
from rich.table import Table
from rich.markdown import Markdown

from hive.machine.audit import query_audit, stats, verify_chain, export_proofs
from hive.machine import AUDIT_DB, MACHINE_DIR

console = Console()

def _severity_label(s: int) -> str:
    return {1: "[green]low[/green]", 2: "[yellow]medium[/yellow]", 3: "[orange3]high[/orange3]", 4: "[red]high[/red]", 5: "[bold red]critical[/bold red]"}.get(s, str(s))

def render_audit_table(limit: int = 50, min_severity: int = 0):
    rows = query_audit(limit=limit, min_severity=min_severity)
    if not rows:
        console.print("[dim]No audit events yet[/dim]")
        return
    t = Table(title=f"Audit — last {limit} (severity≥{min_severity})", show_lines=False)
    t.add_column("ID", style="dim", width=4)
    t.add_column("Time", style="dim")
    t.add_column("Tool")
    t.add_column("File/Network/Command", style="dim", overflow="fold")
    t.add_column("Bytes", justify="right")
    t.add_column("Severity", justify="center")
    t.add_column("Impact", justify="right")
    t.add_column("Hash", style="dim")
    for r in rows:
        sev = _severity_label(r["severity"])
        # file/network/command summary
        target = r["file_path"] or r["network_url"] or (r["command"] or "")[:40] or r["tool"]
        t.add_row(
            str(r["id"]),
            time.strftime("%m-%d %H:%M", time.localtime(r["ts"])),
            r["tool"],
            str(target)[:40],
            f"{r['bytes_out']}→{r['bytes_in']}",
            sev,
            f"{r['impact']}",
            r["hash"][:8],
        )
    console.print(t)
    # proofs
    ok, msg = verify_chain(limit=1000)
    console.print(f"[dim]Chain: {'✓' if ok else '✗'} {msg} — DB: {AUDIT_DB}[/dim]")

def render_network_dashboard():
    rows = query_audit(limit=1000)
    net = [r for r in rows if r["tool"] in ("web_fetch", "web_search")]
    total_out = sum(r["bytes_out"] for r in net)
    total_in = sum(r["bytes_in"] for r in net)
    t = Table(title="Network Usage & Data Outflow (audited proofs)")
    t.add_column("Tool")
    t.add_column("Count", justify="right")
    t.add_column("Bytes Out", justify="right")
    t.add_column("Bytes In", justify="right")
    t.add_column("Avg Severity", justify="right")
    t.add_column("Avg Impact", justify="right")
    from collections import defaultdict
    by = defaultdict(list)
    for r in net:
        by[r["tool"]].append(r)
    for tool, lst in by.items():
        t.add_row(tool, str(len(lst)), str(sum(x["bytes_out"] for x in lst)), str(sum(x["bytes_in"] for x in lst)),
                  f"{sum(x['severity'] for x in lst)/len(lst):.1f}", f"{sum(x['impact'] for x in lst)/len(lst):.0f}")
    t.add_row("[bold]total[/bold]", str(len(net)), str(total_out), str(total_in), "", "")
    console.print(t)
    # list recent URLs with severity
    if net:
        u = Table(title="Recent network — auditable")
        u.add_column("Time", style="dim")
        u.add_column("URL/Query")
        u.add_column("Severity")
        u.add_column("Hash")
        for r in net[:10]:
            u.add_row(time.strftime("%H:%M", time.localtime(r["ts"])), (r["network_url"] or r["args"])[:60], str(r["severity"]), r["hash"][:8])
        console.print(u)

def render_file_dashboard():
    rows = query_audit(limit=1000)
    files = [r for r in rows if r["tool"] in ("list_files", "read_file", "write_file", "delete_path")]
    t = Table(title="File & Folder Usages (audited)")
    t.add_column("Tool")
    t.add_column("Count", justify="right")
    t.add_column("Unique Paths", justify="right")
    t.add_column("Avg Impact", justify="right")
    from collections import defaultdict
    by = defaultdict(list)
    for r in files:
        by[r["tool"]].append(r)
    for tool, lst in by.items():
        uniq = len(set(x["file_path"] for x in lst if x["file_path"]))
        t.add_row(tool, str(len(lst)), str(uniq), f"{sum(x['impact'] for x in lst)/len(lst):.0f}" if lst else "0")
    console.print(t)
    if files:
        v = Table(title="Recent file ops")
        v.add_column("Time", style="dim")
        v.add_column("Tool")
        v.add_column("Path")
        v.add_column("Severity/Impact")
        for r in files[:15]:
            v.add_row(time.strftime("%H:%M", time.localtime(r["ts"])), r["tool"], (r["file_path"] or "")[:40], f"{r['severity']}/{r['impact']}")
        console.print(v)

def render_command_dashboard():
    rows = query_audit(limit=1000)
    cmds = [r for r in rows if r["tool"] in ("run_bash", "run_python")]
    t = Table(title="Executed Commands History (audited, severity/impact)")
    t.add_column("Time", style="dim")
    t.add_column("Tool")
    t.add_column("Command", overflow="fold")
    t.add_column("Severity", justify="center")
    t.add_column("Impact", justify="right")
    t.add_column("Hash", style="dim")
    for r in cmds[:20]:
        t.add_row(time.strftime("%m-%d %H:%M", time.localtime(r["ts"])), r["tool"], (r["command"] or "")[:60], _severity_label(r["severity"]), str(r["impact"]), r["hash"][:8])
    console.print(t)
    if not cmds:
        console.print("[dim]No commands yet[/dim]")

def render_full_dashboard(limit: int = 50):
    s = stats()
    console.print(Markdown(f"# Hive-Machine — Auditable Dashboard\n**DB:** `{AUDIT_DB}`  **Export:** `hive machine audit export`  **Verify:** `verify_chain()`\nTotal events: {s['total'][0]}  Bytes out: {s['total'][1] or 0}  Bytes in: {s['total'][2] or 0}"))
    render_audit_table(limit=limit)
    render_network_dashboard()
    render_file_dashboard()
    render_command_dashboard()
    ok, msg = verify_chain()
    console.print(f"[bold]{'✓ Chain verified' if ok else '✗ Chain broken'}:[/bold] {msg}")

def export_command(path: str = "audit_export.json"):
    out = export_proofs(Path(path))
    console.print(f"[green]Exported {len(out['events'])} events to {path} — verified={out['verified']}[/green]")
    return out

# ── Textual dashboard app ────────────────────────────────────────────
try:
    from textual.app import App, ComposeResult
    from textual.containers import Horizontal, Vertical
    from textual.widgets import Header, Footer, Static, RichLog, Button, Input
    from textual.binding import Binding
    from textual import work, on
    _HAS_TEXTUAL = True
except ImportError:
    _HAS_TEXTUAL = False

if _HAS_TEXTUAL:
    class AuditDashboardApp(App):
        CSS = """
        Screen { layout: vertical; }
        #main { height: 1fr; }
        #left { width: 1fr; border: tall $primary; }
        #right { width: 1fr; border: tall $accent; }
        #bottom { height: 18; border: tall $secondary; }
        """
        TITLE = "Hive-Machine — Auditable Dashboard"
        BINDINGS = [
            Binding("q", "quit", "Quit"),
            Binding("r", "refresh", "Refresh"),
            Binding("e", "export", "Export"),
        ]
        def compose(self) -> ComposeResult:
            yield Header(show_clock=True)
            with Horizontal(id="main"):
                with Vertical(id="left"):
                    yield Static(" Audit Log (severity/impact, hash chain)", id="left-title")
                    yield RichLog(id="audit", highlight=True, markup=True)
                with Vertical(id="right"):
                    yield Static(" Network / Files / Commands", id="right-title")
                    yield RichLog(id="stats", highlight=True, markup=True)
            with Vertical(id="bottom"):
                yield Static(" [r] refresh  [e] export audit_export.json  [q] quit — proofs: hash chain, bytes, sensitivity", id="bottom-help")
            yield Footer()
        def on_mount(self):
            self._refresh()
        def action_refresh(self):
            self._refresh()
        def action_export(self):
            try:
                export_command("audit_export.json")
                self.query_one("#audit", RichLog).write("[green]Exported audit_export.json[/green]")
            except Exception as e:
                self.query_one("#audit", RichLog).write(f"[red]{e}[/red]")
        @work(thread=True)
        def _refresh(self):
            import io
            from rich.console import Console as RConsole
            # audit table
            buf = io.StringIO()
            rc = RConsole(file=buf, force_terminal=True, width=90)
            # we reuse query + rich tables by capturing console output via string
            # simpler: build text manually
            rows = query_audit(limit=30)
            txt = f"Audit events: {len(rows)} | DB: {AUDIT_DB}\n"
            for r in rows[:15]:
                txt += f"{r['id']:3} {time.strftime('%H:%M', time.localtime(r['ts']))} {r['tool']:12} sev={r['severity']} imp={r['impact']:3} hash={r['hash'][:8]} { (r['file_path'] or r['network_url'] or r['command'] or '')[:40]}\n"
            txt += "\nNetwork / Files / Commands stats:\n"
            s = stats()
            txt += f"Total: {s['total'][0]} events, out={s['total'][1] or 0} in={s['total'][2] or 0}\n"
            for tool, cnt, bout, binb, avgsev, avgimp in s['by_tool']:
                txt += f"  {tool:12} cnt={cnt} out={bout or 0} in={binb or 0} sev={avgsev:.1f} imp={avgimp:.0f}\n"
            ok, msg = verify_chain()
            txt += f"\nChain: {'✓' if ok else '✗'} {msg}\n"
            self.call_from_thread(self.query_one("#audit", RichLog).write, txt)
            self.call_from_thread(self.query_one("#stats", RichLog).write, f"[dim]Refreshed {time.strftime('%H:%M:%S')} — severity 1=low 5=critical, impact 0-100[/dim]")

    def run_dashboard():
        if not _HAS_TEXTUAL:
            print("Textual required: pip install textual")
            raise SystemExit(1)
        AuditDashboardApp().run()
else:
    def run_dashboard():
        print("Textual not installed")
        raise SystemExit(1)
