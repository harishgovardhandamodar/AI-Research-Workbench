from __future__ import annotations

import json
from typing import Optional

from hive.config import load_config, CONFIG_FILE
from hive.llm import get_provider, ChatMessage, chat
from hive.papers import resolver as resolver_mod
from hive.research.workflows import search_and_rank, enrich_full_text, run_workflow
from hive.research.session import list_sessions

try:
    from textual.app import App, ComposeResult
    from textual.containers import Horizontal, Vertical
    from textual.widgets import Header, Footer, Input, Button, Static, RichLog, ListView, ListItem, Label, DataTable, LoadingIndicator, Markdown
    from textual.binding import Binding
    from textual import work, on
    _HAS_TEXTUAL = True
except ImportError:
    _HAS_TEXTUAL = False


MENU = [
    ("rank", "Rank papers"),
    ("paper", "Paper lookup"),
    ("ask", "Ask LLM"),
    ("deepresearch", "/deepresearch"),
    ("lit", "/lit"),
    ("compare", "/compare"),
    ("review", "/review"),
    ("audit", "/audit"),
    ("replicate", "/replicate"),
    ("recipe", "/recipe"),
    ("draft", "/draft"),
    ("autoresearch", "/autoresearch"),
    ("watch", "/watch"),
    ("report", "Deep Report (Feynman)"),
    ("sessions", "Sessions (/outputs)"),
    ("doctor", "Doctor"),
]

HELP_TEXT = """**Hive Research - A Local research companion** — local research companion (Ollama / LM Studio)

- Type a topic in the input and press Enter (or click Run).
- Left menu: choose workflow. `Rank` shows deterministic PaperRank; `DeepResearch` etc. run full LLM synthesis via local model.
- `Paper` accepts DOI / arXiv ID / title. `Ask` chats directly.
- `Deep Report` → 20-section Feynman-parity deep analysis (hero, provenance, Pi chat, BioTools, synthesis, matrix, evidence, previews, checklist, compute/lineage, audit, gaps, reading list, changelog) — saved to `~/.hive/hive.db` + `~/.hive/machine/workspace/report_*.md` for preview.
- All results are saved to `~/.hive/hive.db` (view via Sessions).
- Keys: `q` quit, `f` focus input, `s` sessions, `d` doctor, `1-9` quick menu.
"""

if _HAS_TEXTUAL:
    class HiveTUI(App):
        CSS = """
        Screen { layout: vertical; }
        #top { height: 3; }
        #main { height: 1fr; }
        #menu { width: 28; min-width: 22; border: tall $primary; }
        #content { width: 1fr; }
        #input-row { height: 5; dock: bottom; }
        #output { height: 1fr; border: tall $secondary; }
        #status { height: 1; color: $text-muted; }
        ListView > ListItem { padding: 0 1; }
        ListView > ListItem.--highlight { background: $accent; }
        DataTable { height: auto; }
        """

        TITLE = "Hive Research - A Local research companion"
        BINDINGS = [
            Binding("q", "quit", "Quit"),
            Binding("f", "focus_input", "Focus input"),
            Binding("s", "show_sessions", "Sessions"),
            Binding("d", "show_doctor", "Doctor"),
            Binding("question_mark", "help", "Help"),
        ]

        def __init__(self):
            super().__init__()
            self.cfg = load_config()
            self.current = "rank"
            self._pending_query: str = ""

        def compose(self) -> ComposeResult:
            yield Header(show_clock=True)
            with Horizontal(id="main"):
                with Vertical(id="menu"):
                    yield Static(" Workflows", id="menu-title")
                    yield ListView(
                        *[ListItem(Label(f"{label}"), id=f"menu-{key}") for key, label in MENU],
                        id="menu-list",
                    )
                    yield Static("", id="status")
                with Vertical(id="content"):
                    yield Static(HELP_TEXT, id="help-md")
                    yield RichLog(id="output", highlight=True, markup=True, wrap=True)
                    with Horizontal(id="input-row"):
                        yield Input(placeholder="Topic — e.g. 'mechanistic interpretability sparse autoencoders'  (Enter to run)", id="query")
                        yield Input(placeholder="top", value="10", id="topk", compact=True)
                        yield Button("Run", id="run", variant="primary")
            yield Footer()

        def on_mount(self) -> None:
            self.query_one("#query").focus()
            self._refresh_status()
            self.query_one("#output", RichLog).write("[dim]Welcome to Hive Research - A Local research companion — press ? for help[/dim]")

        def _refresh_status(self):
            prov = get_provider(self.cfg)
            ok, msg = prov.health()
            status = f"{prov.name}@{prov.base_url} {'✓' if ok else '✗ '+msg[:40]}"
            try:
                self.query_one("#status", Static).update(f" LLM: {status}\n cfg: {CONFIG_FILE}")
            except Exception:
                pass

        @on(ListView.Selected, "#menu-list")
        def on_menu_selected(self, event: ListView.Selected):
            # id like menu-rank
            list_id = event.item.id or ""
            key = list_id.replace("menu-", "")
            if key:
                self.current = key
                if key == "sessions":
                    self.run_sessions()
                elif key == "doctor":
                    self.run_doctor()
                else:
                    self.query_one("#help-md", Static).update(f"**Mode: {self.current}** — enter query below and press Run/Enter")
                    self.query_one("#query", Input).focus()

        @on(Button.Pressed, "#run")
        def on_run(self, event: Button.Pressed):
            self._dispatch()

        @on(Input.Submitted, "#query")
        def on_submit(self, event: Input.Submitted):
            self._dispatch()

        def action_focus_input(self):
            self.query_one("#query", Input).focus()

        def action_show_sessions(self):
            self.current = "sessions"
            self.run_sessions()

        def action_show_doctor(self):
            self.current = "doctor"
            self.run_doctor()

        def action_help(self):
            self.query_one("#output", RichLog).write(HELP_TEXT)

        def _dispatch(self):
            q = self.query_one("#query", Input).value.strip()
            if not q:
                self.query_one("#output", RichLog).write("[yellow]Enter a topic / DOI / question[/yellow]")
                return
            topk_raw = self.query_one("#topk", Input).value.strip()
            try:
                topk = int(topk_raw) if topk_raw else 10
            except ValueError:
                topk = 10
            kind = self.current
            log = self.query_one("#output", RichLog)
            log.write(f"[bold cyan]▶ {kind}: {q} (top={topk})[/bold cyan]")
            # dispatch async
            if kind == "rank":
                self._run_rank(q, topk)
            elif kind == "paper":
                self._run_paper(q)
            elif kind == "ask":
                self._run_ask(q)
            elif kind == "report":
                self._run_report(q, topk)
            elif kind in ("deepresearch", "lit", "compare", "review", "audit", "replicate", "recipe", "draft", "autoresearch", "watch"):
                self._run_workflow(kind, q, topk)
            elif kind == "sessions":
                self.run_sessions()
            elif kind == "doctor":
                self.run_doctor()
            else:
                self._run_workflow(kind, q, topk)

        @work(thread=True)
        def _run_rank(self, query: str, top_k: int):
            try:
                papers = search_and_rank(query, top_k=top_k, cfg=self.cfg)
                if not papers:
                    self.call_from_thread(self.query_one("#output", RichLog).write, "[yellow]No papers found[/yellow]")
                    return
                # build table text
                lines = []
                for i, p in enumerate(papers, 1):
                    lines.append(f"{i}. **{p.title}** — {', '.join(p.authors[:2])} ({p.year or 'n.d.'}) [score {p.score} | cites {p.cited_by_count} | {p.venue or ''}] DOI:{p.doi or '-'}")
                    lines.append(f"   [dim]{p.url or p.id} breakdown={p.score_breakdown}[/dim]")
                # optional LLM rescore if healthy
                prov = get_provider(self.cfg)
                ok, _ = prov.health()
                extra = ""
                if ok and len(papers) >= 1:
                    try:
                        ft = enrich_full_text(papers, top_n=min(3, len(papers)))
                        from hive.research.prompts import RANK_RESCORING
                        block = "\n".join(f"{p.id}: {p.title} score={p.score}" for p in papers)
                        ft_block = "\n".join(f"{p.title}\n{(txt or '')[:3000]}" for p, txt in ft)
                        prompt = RANK_RESCORING.format(topic=query, papers_block=block, fulltext_block=ft_block)
                        resp = chat(self.cfg, [ChatMessage(role="system", content="You are a ranking assistant. Return JSON and brief justification."), ChatMessage(role="user", content=prompt)])
                        extra = f"\n\n**LLM rescoring (full-text top 3):**\n{resp.content[:4000]}"
                    except Exception as e:
                        extra = f"\n[dim]LLM rescoring skipped: {e}[/dim]"
                out = "\n".join(lines) + extra
                self.call_from_thread(self.query_one("#output", RichLog).write, out)
            except Exception as e:
                self.call_from_thread(self.query_one("#output", RichLog).write, f"[red]Rank error: {e}[/red]")

        @work(thread=True)
        def _run_paper(self, identifier: str):
            try:
                p = resolver_mod.resolve(identifier)
                if not p:
                    self.call_from_thread(self.query_one("#output", RichLog).write, f"[red]Could not resolve: {identifier}[/red]")
                    return
                header = f"# {p.title}\nAuthors: {', '.join(p.authors)}\nVenue: {p.venue} {p.year or ''}\nDOI: {p.doi}\nURL: {p.url or p.oa_url or p.pdf_url}\nCited by: {p.cited_by_count} OA:{p.is_open_access}\n\n{p.abstract or '(no abstract)'}"
                txt = resolver_mod.fetch_full_text(p)
                if txt:
                    header += f"\n\n**Full-text evidence (truncated):**\n{txt[:8000]}"
                self.call_from_thread(self.query_one("#output", RichLog).write, header)
            except Exception as e:
                self.call_from_thread(self.query_one("#output", RichLog).write, f"[red]Paper error: {e}[/red]")

        @work(thread=True)
        def _run_ask(self, question: str):
            try:
                prov = get_provider(self.cfg)
                ok, msg = prov.health()
                if not ok:
                    self.call_from_thread(self.query_one("#output", RichLog).write, f"[red]No LLM: {msg}[/red]")
                    return
                # stream via sync chat (simple)
                resp = chat(self.cfg, [ChatMessage(role="user", content=question)])
                self.call_from_thread(self.query_one("#output", RichLog).write, resp.content)
            except Exception as e:
                self.call_from_thread(self.query_one("#output", RichLog).write, f"[red]Ask error: {e}[/red]")

        @work(thread=True)
        def _run_report(self, topic: str, top_k: int):
            try:
                from hive.research.report import generate_deep_report
                self.call_from_thread(self.query_one("#output", RichLog).write, f"[dim]Deep Report — Feynman parity (20 sections) via {get_provider(self.cfg).name}… top={top_k}[/dim]")
                md, sid, aid = generate_deep_report(topic, self.cfg, top_k=top_k, full_text_top=self.cfg.full_text_top, depth="deep")
                # show provenance + preview stub
                preview = md[:12000]
                self.call_from_thread(self.query_one("#output", RichLog).write, preview + f"\n\n[dim]Saved artifact {aid} session {sid} → ~/.hive/machine/workspace/report_{sid}.md — view via `hive sessions` or `hive machine ls`[/dim]")
            except Exception as e:
                self.call_from_thread(self.query_one("#output", RichLog).write, f"[red]Deep Report error: {e}[/red]")

        @work(thread=True)
        def _run_workflow(self, kind: str, topic: str, top_k: int):
            try:
                prov = get_provider(self.cfg)
                ok, msg = prov.health()
                if not ok:
                    self.call_from_thread(self.query_one("#output", RichLog).write, f"[red]No LLM: {msg}[/red]")
                    return
                self.call_from_thread(self.query_one("#output", RichLog).write, f"[dim]Running {kind} via {prov.name}…[/dim]")
                out = run_workflow(kind, topic, self.cfg, top_k=top_k, full_text_top=self.cfg.full_text_top)
                self.call_from_thread(self.query_one("#output", RichLog).write, out)
            except Exception as e:
                self.call_from_thread(self.query_one("#output", RichLog).write, f"[red]{kind} error: {e}[/red]")

        @work(thread=True)
        def run_sessions(self):
            try:
                rows = list_sessions(limit=30)
                if not rows:
                    self.call_from_thread(self.query_one("#output", RichLog).write, "[dim]No sessions yet[/dim]")
                    return
                lines = ["**Sessions (/outputs):**"]
                for sid, topic, ts in rows:
                    import datetime
                    dt = datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else ""
                    lines.append(f"- `{sid}` {topic} [{dt}]")
                self.call_from_thread(self.query_one("#output", RichLog).write, "\n".join(lines))
            except Exception as e:
                self.call_from_thread(self.query_one("#output", RichLog).write, f"[red]Sessions error: {e}[/red]")

        @work(thread=True)
        def run_doctor(self):
            try:
                cfg = self.cfg
                lines = [f"Config: {CONFIG_FILE} provider={cfg.llm.provider}"]
                for label, prov in [
                    ("Ollama", __import__("hive.llm.ollama", fromlist=["OllamaProvider"]).OllamaProvider(cfg.llm.ollama_url, cfg.llm.ollama_model)),
                    ("LM Studio", __import__("hive.llm.lmstudio", fromlist=["LMStudioProvider"]).LMStudioProvider(cfg.llm.lmstudio_url, cfg.llm.lmstudio_model)),
                ]:
                    ok, msg = prov.health()
                    lines.append(f"{label} @ {prov.base_url}: {'ok' if ok else msg}")
                    if ok:
                        try:
                            ms = prov.list_models()
                            lines.append(f"  models: {', '.join(ms[:5])}")
                        except Exception as e:
                            lines.append(f"  models error: {e}")
                import httpx
                for name, url in [("OpenAlex", "https://api.openalex.org/works?search=test&per-page=1"), ("arXiv", "https://export.arxiv.org/api/query?search_query=all:test&max_results=1")]:
                    try:
                        with httpx.Client(timeout=8) as c:
                            r = c.get(url)
                            lines.append(f"{name}: {r.status_code}")
                    except Exception as e:
                        lines.append(f"{name}: {e}")
                self.call_from_thread(self.query_one("#output", RichLog).write, "\n".join(lines))
            except Exception as e:
                self.call_from_thread(self.query_one("#output", RichLog).write, f"[red]Doctor error: {e}[/red]")

else:
    class HiveTUI:  # type: ignore
        def __init__(self, *a, **kw):
            raise RuntimeError("Textual not installed. Install with: pip install 'hive-research[tui]'  or  pip install textual")


def run():
    if not _HAS_TEXTUAL:
        print("Textual is required for TUI. Install: pip install textual  or  pip install -e \".[tui]\"")
        raise SystemExit(1)
    app = HiveTUI()
    app.run()
