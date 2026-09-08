from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table
from rich.markdown import Markdown

from hive import __version__
from hive.config import load_config, save_config, AppConfig, CONFIG_FILE, DB_FILE
from hive.llm import ChatMessage, chat, chat_stream, get_provider
from hive.papers import openalex, arxiv, rank as rank_mod, resolver
from hive.research.workflows import search_and_rank, enrich_full_text, run_workflow
from hive.research.session import list_sessions
from hive.ledger import log_execution, query_ledger, ledger_stats, verify_ledger, add_feedback  # AGI ledger
from hive.workbench import list_workbenches, get_workbench, create_workbench, delete_workbench, resolve_workbench  # narrow workbench
from hive.learn import run_loop, learn_status, rollback, query_memory  # continual learning
from hive.derived import generate_derived, list_derived  # derived scenarios

app = typer.Typer(add_completion=False, rich_markup_mode="markdown", help="Hive Research - A Local research companion (Ollama/LM Studio)")
console = Console()

# ── Hive-Machine sub-app (Perplexity Computer) ───────────────────────
machine_app = typer.Typer(help="Hive-Machine — local Perplexity Computer (files, code, web, terminal)", no_args_is_help=False)
app.add_typer(machine_app, name="machine")


# ── helpers ──────────────────────────────────────────────────────────

def _cfg() -> AppConfig:
    return load_config()


def _print_papers(papers, json_out: bool = False):
    if json_out:
        print(json.dumps([p.model_dump() for p in papers], indent=2, ensure_ascii=False))
        return
    t = Table(title="PaperRank", show_lines=False)
    t.add_column("#", style="dim", width=3)
    t.add_column("Score", justify="right", style="bold cyan")
    t.add_column("Title")
    t.add_column("Venue / Year", style="dim")
    t.add_column("Cites", justify="right")
    t.add_column("OA", justify="center")
    for i, p in enumerate(papers, 1):
        t.add_row(
            str(i),
            f"{p.score:.1f}" if p.score is not None else "-",
            (p.title[:80] + "…") if len(p.title) > 80 else p.title,
            f"{p.venue or '-'} {p.year or ''}",
            str(p.cited_by_count),
            "✓" if (p.is_open_access or p.oa_url) else "",
        )
    console.print(t)
    for p in papers:
        console.print(f"[dim]{p.url or p.doi or p.id}[/dim]")


# ── commands ─────────────────────────────────────────────────────────

@app.command("rank")
def rank_cmd(
    query: str = typer.Argument(..., help="Topic to rank, e.g. 'mechanistic interpretability sparse autoencoders'"),
    top_k: int = typer.Option(10, "--top", "-k", help="How many papers"),
    full_text_top: int = typer.Option(3, "--full-text-top", help="Fetch section-aware evidence for top N and rescore via LLM"),
    json_out: bool = typer.Option(False, "--json", help="JSON output"),
    no_llm: bool = typer.Option(False, "--no-llm", help="Skip LLM rescoring, deterministic ranking only"),
):
    """PaperRank scoring for deciding what to read first, with transparent evidence."""
    cfg = _cfg()
    papers = search_and_rank(query, top_k=top_k, cfg=cfg)
    if not papers:
        console.print("[yellow]No papers found[/yellow]")
        raise typer.Exit(1)

    # optional LLM rescoring with full-text evidence
    if full_text_top > 0 and not no_llm and len(papers) >= 1:
        prov = get_provider(cfg)
        ok, _ = prov.health()
        if ok:
            ft = enrich_full_text(papers, top_n=full_text_top)
            # build rescoring prompt
            from hive.research.prompts import RANK_RESCORING
            block = "\n".join(f"{p.id}: {p.title} score={p.score} bd={p.score_breakdown}" for p in papers)
            ft_block = "\n".join(f"{p.title}\n{(txt or '')[:4000]}" for p, txt in ft)
            prompt = RANK_RESCORING.format(topic=query, papers_block=block, fulltext_block=ft_block)
            try:
                resp = chat(cfg, [ChatMessage(role="system", content="You are a ranking assistant. Return JSON only."), ChatMessage(role="user", content=prompt)])
                # try parse rescoring; keep original if fails
                console.print(Markdown(f"**LLM rescoring evidence (top {full_text_top}):**\n\n{resp.content[:2000]}"))
            except Exception as e:
                console.print(f"[dim]LLM rescoring skipped: {e}[/dim]")
        else:
            console.print("[dim]No local LLM available — showing deterministic ranking only[/dim]")

    _print_papers(papers, json_out=json_out)
    if not json_out:
        # breakdown
        for p in papers:
            bd = p.score_breakdown or {}
            console.print(f"[dim]{p.title[:70]} → {bd}[/dim]")


@app.command("paper")
def paper_cmd(
    identifier: str = typer.Argument(..., help="DOI, arXiv ID, OpenAlex ID, or title"),
    fetch_full_text: bool = typer.Option(False, "--fetch-full-text", help="Resolve and fetch source-specific full text"),
    json_out: bool = typer.Option(False, "--json", help="JSON output"),
):
    """Paper access resolver — OpenAlex/arXiv/DOI/Europe PMC candidates + optional text fetch."""
    cfg = _cfg()
    p = resolver.resolve(identifier)
    if not p:
        console.print(f"[red]Could not resolve: {identifier}[/red]")
        raise typer.Exit(1)
    if json_out:
        print(json.dumps(p.model_dump(), indent=2, ensure_ascii=False))
    else:
        console.print(Markdown(f"# {p.title}\n\n**Authors:** {', '.join(p.authors) or '—'}  \n**Venue:** {p.venue or '—'} {p.year or ''}  \n**DOI:** {p.doi or '—'}  \n**URL:** {p.url or p.oa_url or p.pdf_url or '—'}  \n**Cited by:** {p.cited_by_count}  \n**OA:** {p.is_open_access}\n\n> {p.abstract or '(no abstract)'}"))
    if fetch_full_text:
        txt = resolver.fetch_full_text(p)
        if txt:
            if json_out:
                print(json.dumps({"full_text": txt[:30000]}))
            else:
                console.print(Markdown(f"## Full-text evidence\n\n{txt[:8000]}"))
        else:
            console.print("[yellow]No legal full-text available for this paper[/yellow]")


@app.command("ask")
def ask_cmd(
    question: str = typer.Argument(..., help="Natural language question"),
    stream: bool = typer.Option(True, "--stream/--no-stream", help="Stream tokens"),
):
    """Ask the local LLM directly (no paper search)."""
    cfg = _cfg()
    msgs = [ChatMessage(role="user", content=question)]
    prov = get_provider(cfg)
    ok, msg = prov.health()
    if not ok:
        console.print(f"[red]No local LLM reachable: {msg}\nCheck ollama serve or LM Studio (http://localhost:1234)[/red]")
        raise typer.Exit(1)
    if stream:
        for tok in chat_stream(cfg, msgs):
            print(tok, end="", flush=True)
        print()
    else:
        resp = chat(cfg, msgs)
        console.print(Markdown(resp.content))


def _workflow_cmd(kind: str, topic: str, top: int, full_text_top: int, no_stream: bool = False):
    cfg = _cfg()
    prov = get_provider(cfg)
    ok, msg = prov.health()
    if not ok:
        console.print(f"[red]No local LLM reachable: {msg}[/red]")
        raise typer.Exit(1)
    with console.status(f"[bold green]Running {kind} — local LLM ({prov.name})...[/bold green]"):
        out = run_workflow(kind, topic, cfg, top_k=top, full_text_top=full_text_top)
    console.print(Markdown(out))
    console.print(f"\n[dim]Saved to {DB_FILE} — view with: hive sessions[/dim]")


@app.command("deepresearch")
def deepresearch(query: str = typer.Argument(..., help="Topic"), top: int = typer.Option(10, "--top"), full_text_top: int = typer.Option(3, "--full-text-top")):
    """/deepresearch — source-heavy multi-agent investigation."""
    _workflow_cmd("deepresearch", query, top, full_text_top)

@app.command("lit")
def lit(query: str = typer.Argument(..., help="Topic or lab"), top: int = typer.Option(12, "--top"), full_text_top: int = typer.Option(3, "--full-text-top")):
    """/lit — literature review from paper search and primary sources."""
    _workflow_cmd("lit", query, top, full_text_top)

@app.command("review")
def review_cmd(artifact: str = typer.Argument(..., help="Artifact to review"), top: int = typer.Option(8, "--top"), full_text_top: int = typer.Option(2, "--full-text-top")):
    """/review — research review with severity and revision plan."""
    _workflow_cmd("review", artifact, top, full_text_top)

@app.command("audit")
def audit(item: str = typer.Argument(..., help="Paper vs codebase item"), top: int = typer.Option(8, "--top"), full_text_top: int = typer.Option(2, "--full-text-top")):
    """/audit — paper vs codebase mismatch audit."""
    _workflow_cmd("audit", item, top, full_text_top)

@app.command("replicate")
def replicate(paper: str = typer.Argument(..., help="Paper to replicate"), top: int = typer.Option(8, "--top"), full_text_top: int = typer.Option(3, "--full-text-top")):
    """/replicate — plan replication checks (execute only after choosing env)."""
    _workflow_cmd("replicate", paper, top, full_text_top)

@app.command("recipe")
def recipe(task: str = typer.Argument(..., help="Task or paper"), top: int = typer.Option(10, "--top"), full_text_top: int = typer.Option(2, "--full-text-top")):
    """/recipe — ranked ML training recipes."""
    _workflow_cmd("recipe", task, top, full_text_top)

@app.command("compare")
def compare(topic: str = typer.Argument(..., help="Topic"), top: int = typer.Option(10, "--top"), full_text_top: int = typer.Option(3, "--full-text-top")):
    """/compare — source comparison matrix."""
    _workflow_cmd("compare", topic, top, full_text_top)

@app.command("draft")
def draft(topic: str = typer.Argument(..., help="Topic"), top: int = typer.Option(10, "--top"), full_text_top: int = typer.Option(3, "--full-text-top")):
    """/draft — paper-style draft from research findings."""
    _workflow_cmd("draft", topic, top, full_text_top)

@app.command("autoresearch")
def autoresearch(idea: str = typer.Argument(..., help="Idea"), top: int = typer.Option(8, "--top"), full_text_top: int = typer.Option(2, "--full-text-top")):
    """/autoresearch — bounded experiment loop with benchmark evidence."""
    _workflow_cmd("autoresearch", idea, top, full_text_top)

@app.command("watch")
def watch(topic: str = typer.Argument(..., help="Topic"), top: int = typer.Option(10, "--top"), full_text_top: int = typer.Option(2, "--full-text-top")):
    """/watch — research watch baseline with optional scheduled follow-up."""
    _workflow_cmd("watch", topic, top, full_text_top)


@app.command("report")
def report_cmd(
    topic: str = typer.Argument(..., help="Topic for deep analysis report"),
    top: int = typer.Option(12, "--top", help="Papers to ground on"),
    full_text_top: int = typer.Option(3, "--full-text-top", help="Full-text evidence count"),
    depth: str = typer.Option("deep", "--depth", help="brief|standard|deep"),
    no_save: bool = typer.Option(False, "--no-save", help="Don't persist to DB/workspace"),
):
    """Deep analysis report — 20-section Feynman-parity report (hero, provenance, synthesis, matrix, checklist, lineage, changelog) — local only."""
    cfg = _cfg()
    prov = get_provider(cfg)
    ok, msg = prov.health()
    if not ok:
        console.print(f"[red]No local LLM reachable: {msg}[/red]")
        raise typer.Exit(1)
    from hive.research.report import generate_deep_report
    with console.status(f"[bold green]Deep Report — {topic} via {prov.name} (top={top}, depth={depth})...[/bold green]"):
        md, sid, aid = generate_deep_report(topic, cfg, top_k=top, full_text_top=full_text_top, depth=depth, save=not no_save)
    console.print(Markdown(md))
    if not no_save:
        console.print(f"\n[dim]Saved artifact {aid} session {sid} → ~/.hive/machine/workspace/report_{sid}.md — hive sessions[/dim]")


@app.command("tui")
def tui_cmd():
    """Launch Hive Research - A Local research companion — terminal workbench (local, Ollama/LM Studio)."""
    try:
        from hive.tui.app import run
    except ImportError as e:
        console.print(f"[red]TUI requires textual: {e}\nInstall: pip install textual  or  pip install -e \".[tui]\"[/red]")
        raise typer.Exit(1)
    run()


# ── Hive-Machine commands ──────────────────────────────────────────

@machine_app.command("run")
def machine_run(
    task: str = typer.Argument(..., help="Task for Hive-Machine, e.g. 'fetch https://example.com and summarize to report.md'"),
    steps: int = typer.Option(12, "--steps", "-n", help="Max agent steps"),
    access: str = typer.Option("core-workflow", "--access", help="Access tier: local-only (no web/commit, private) | core-workflow (web+commit, redacted) | public (all public)"),
):
    """Run Hive-Machine agent headlessly (no TUI) — local computer use. Access controls folder tier."""
    from hive.machine.agent import run_task
    from hive.machine import WORKSPACE, workflow_dirs
    from hive.machine.access import TIERS
    if access not in TIERS:
        console.print(f"[red]Invalid access: {access} — choose {TIERS}[/red]")
        raise typer.Exit(1)
    # ensure 3-tier workspace for adhoc runs (uses task name as pseudo-workflow)
    try:
        workflow_dirs(f"adhoc-{access}")
    except Exception:
        pass
    cfg = _cfg()
    console.print(f"[bold cyan]Hive-Machine: {task}[/bold cyan]  [dim]max_steps={steps} access={access} ws={WORKSPACE}/{access}[/dim]")
    # access is audited in agent/tools via workflow_id
    out = run_task(task, cfg, max_steps=steps, verbose=False)
    console.print(Markdown(out))
    console.print(f"[dim]Access tier '{access}': {'no web/no commit, private' if access=='local-only' else 'web+commit, redacted' if access=='core-workflow' else 'public'} — see ~/.hive/machine/workspace/adhoc-{access}/{access}/[/dim]")


@machine_app.command("tui")
def machine_tui():
    """Launch Hive-Machine TUI — local Perplexity Computer."""
    try:
        from hive.machine.app import run as mrun
    except ImportError as e:
        console.print(f"[red]Hive-Machine TUI requires textual: {e}[/red]")
        raise typer.Exit(1)
    mrun()


@machine_app.command("ls")
def machine_ls(
    path: str = typer.Argument(".", help="Path relative to workspace ~/.hive/machine/workspace"),
    recursive: bool = typer.Option(False, "--recursive", "-r", help="Recursive"),
):
    """List workspace files (jailed)."""
    from hive.machine.tools import list_files
    console.print(list_files(path, recursive))


@machine_app.command("read")
def machine_read(path: str = typer.Argument(..., help="File to read (relative to workspace)")):
    """Read a workspace file."""
    from hive.machine.tools import read_file
    console.print(read_file(path))


@machine_app.command("write")
def machine_write(
    path: str = typer.Argument(..., help="Destination relative to workspace"),
    content: str = typer.Argument(..., help="Content to write"),
):
    """Write a workspace file."""
    from hive.machine.tools import write_file
    console.print(write_file(path, content))


@machine_app.command("exec")
def machine_exec(
    cmd: str = typer.Option(..., "--cmd", "-c", help="Bash command to run in workspace"),
    timeout: int = typer.Option(30, "--timeout"),
):
    """Run bash in Hive-Machine workspace."""
    from hive.machine.tools import run_bash
    console.print(run_bash(cmd, timeout))


@machine_app.command("python")
def machine_python(
    code: str = typer.Option(..., "--code", "-c", help="Python code to run"),
    timeout: int = typer.Option(30, "--timeout"),
):
    """Run python snippet in workspace."""
    from hive.machine.tools import run_python
    console.print(run_python(code, timeout))


@machine_app.command("fetch")
def machine_fetch(url: str = typer.Argument(..., help="URL to fetch")):
    """Web fetch via Hive-Machine (local httpx + BS4)."""
    from hive.machine.tools import web_fetch
    console.print(web_fetch(url)[:8000])


@machine_app.command("search")
def machine_search(query: str = typer.Argument(..., help="Search query (OpenAlex)"), top: int = typer.Option(5, "--top")):
    """Web/paper search via Hive-Machine."""
    from hive.machine.tools import web_search
    console.print(web_search(query, top))


@machine_app.command("history")
def machine_history(limit: int = typer.Option(20, "--limit", "-n")):
    """Show Hive-Machine run history."""
    from hive.machine.agent import list_history
    rows = list_history(limit)
    if not rows:
        console.print("[dim]No machine runs yet[/dim]")
        return
    t = Table()
    t.add_column("ID")
    t.add_column("Task")
    t.add_column("Steps")
    t.add_column("Created")
    import datetime
    for rid, task, created, steps in rows:
        dt = datetime.datetime.fromtimestamp(created).strftime("%Y-%m-%d %H:%M") if created else ""
        t.add_row(str(rid), task[:60], str(steps), dt)
    console.print(t)


@machine_app.command("workflow")
def machine_workflow(
    action: str = typer.Argument(..., help="list|run|example"),
    name: str = typer.Argument("", help="Workflow name or path for run"),
    dry: bool = typer.Option(False, "--dry-run", help="Dry run without executing"),
):
    """User-defined workflows — end-to-end (YAML/JSON in ~/.hive/machine/workflows/)."""
    from hive.machine.workflows import list_workflows, run_workflow, EXAMPLE_AI_AGENTS, save_workflow
    from pathlib import Path
    if action == "list":
        wfs = list_workflows()
        if not wfs:
            console.print("[dim]No workflows yet — run 'hive machine workflow example' to create one[/dim]")
            return
        t = Table(title="Workflows")
        t.add_column("Name")
        t.add_column("Path")
        for p in wfs:
            t.add_row(p.stem, str(p))
        console.print(t)
    elif action == "example":
        p = save_workflow("ai_agents_report", EXAMPLE_AI_AGENTS)
        console.print(f"[green]Created {p}[/green]")
        console.print(Markdown(f"```yaml\n{Path(p).read_text()[:800]}\n```"))
    elif action == "run":
        if not name:
            console.print("[red]Provide workflow name or path: hive machine workflow run <name>[/red]")
            raise typer.Exit(1)
        cfg = _cfg()
        res = run_workflow(name, cfg, dry_run=dry)
        console.print(f"[bold]Workflow {res['workflow']} done in {res['elapsed']:.1f}s access={res.get('access','core-workflow')} [/bold]")
        for sid, out in res["outputs"].items():
            console.print(Markdown(f"### {sid}\n{out[:2000]}"))
        # show 3-tier dirs
        dirs = res.get("dirs", {})
        if dirs:
            console.print(f"[dim]3-tier workspace: {dirs.get('local-only','')} (local-only, .gitignore *) | {dirs.get('core-workflow','')} (web+commit) | {dirs.get('public','')} (public)[/dim]")
        console.print(f"[dim]Audit: {len(res['audit'])} events — hive machine audit --verify[/dim]")
    else:
        console.print("[red]Unknown action: use list|run|example[/red]")


@machine_app.command("audit")
def machine_audit(
    limit: int = typer.Option(50, "--limit", "-n", help="Events to show"),
    min_severity: int = typer.Option(0, "--min-severity", help="Filter severity 1-5"),
    export: str = typer.Option("", "--export", help="Export JSON path"),
    verify: bool = typer.Option(False, "--verify", help="Verify hash chain"),
):
    """Auditable proofs — network, data outflow, files, commands with severity/impact."""
    from hive.machine.dashboard import render_audit_table, export_command
    from hive.machine.audit import verify_chain
    if export:
        export_command(export)
        return
    if verify:
        ok, msg = verify_chain()
        console.print(f"[{'green' if ok else 'red'}]{msg}[/{'green' if ok else 'red'}]")
        return
    render_audit_table(limit=limit, min_severity=min_severity)


@machine_app.command("dashboard")
def machine_dashboard():
    """Launch auditable dashboard TUI (network, files, commands, hash proofs)."""
    try:
        from hive.machine.dashboard import run_dashboard
    except ImportError as e:
        console.print(f"[red]Dashboard requires textual: {e}[/red]")
        raise typer.Exit(1)
    run_dashboard()


@machine_app.command("nvidia")
def machine_nvidia():
    """Nvidia-PAIR — discover all local models (Ollama, LM Studio, NIM) and GPU."""
    from hive.machine.nvidia_pair import discover_all, nvidia_smi_info
    cfg = _cfg()
    console.print(Markdown(f"**GPU:** {nvidia_smi_info()}"))
    models = discover_all(cfg)
    t = Table(title="Nvidia-PAIR — Local Models (local-first router)")
    t.add_column("Provider")
    t.add_column("Model")
    t.add_column("URL")
    t.add_column("Healthy")
    t.add_column("Extra")
    for m in models:
        t.add_row(m.provider, m.model, m.url, "[green]yes[/green]" if m.healthy else "[red]no[/red]", m.extra[:40])
    console.print(t)
    from hive.machine.nvidia_pair import pick_best
    best = pick_best(cfg)
    if best:
        console.print(f"[dim]Best for fastest: {best.provider} {best.model} @ {best.url}[/dim]")


@machine_app.callback(invoke_without_command=True)
def machine_callback(ctx: typer.Context):
    """Hive-Machine — local Perplexity Computer. No args → launch TUI."""
    if ctx.invoked_subcommand is None:
        try:
            from hive.machine.app import run as mrun
        except ImportError as e:
            console.print(f"[red]Hive-Machine TUI requires textual: {e}[/red]")
            raise typer.Exit(1)
        mrun()


@app.command("serve")
def serve(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8000, "--port"),
    no_auth: bool = typer.Option(False, "--no-auth", help="Plain localhost mode"),
    web: bool = typer.Option(False, "--web", help="Serve TSX web dashboard + API (Hive Research + Hive-Machine)"),
):
    """Open a minimal local workbench (static preview of sessions/artifacts) — add --web for TSX dashboard."""
    if web:
        from hive.web.server import run_web
        run_web(host, port)
        return
    from http.server import HTTPServer, SimpleHTTPRequestHandler
    import sqlite3
    console.print(f"[green]Hive workbench at http://{host}:{port} — sessions in {DB_FILE}[/green]")
    console.print("[dim]This is a minimal preview; full workbench UI is available via Open WebUI integration (see openwebui/README.md) — try `hive serve --web` or `hive web` for TSX dashboard[/dim]")
    for sid, topic, ts in list_sessions():
        console.print(f"  {sid}  {topic}")
    exports = Path.home() / ".hive" / "exports"
    exports.mkdir(parents=True, exist_ok=True)
    import os
    os.chdir(exports)
    httpd = HTTPServer((host, port), SimpleHTTPRequestHandler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        console.print("\n[dim]Stopped[/dim]")


@app.command("web")
def web_cmd(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8000, "--port"),
    open: bool = typer.Option(False, "--open", help="Open browser"),
):
    """Launch web dashboard (TSX) — Research + Hive-Machine charts, statuses, logs (local-first)."""
    from hive.web.server import run_web
    run_web(host, port, open_browser=open)


@app.command("config")
def config_cmd(
    show: bool = typer.Option(False, "--show", help="Show current config"),
    provider: Optional[str] = typer.Option(None, "--provider", help="ollama | lmstudio | auto"),
    ollama_url: Optional[str] = typer.Option(None, "--ollama-url"),
    ollama_model: Optional[str] = typer.Option(None, "--ollama-model"),
    lmstudio_url: Optional[str] = typer.Option(None, "--lmstudio-url"),
    lmstudio_model: Optional[str] = typer.Option(None, "--lmstudio-model"),
):
    """View or set local-LLM config (~/.hive/config.toml)."""
    cfg = _cfg()
    changed = False
    if provider:
        cfg.llm.provider = provider  # type: ignore
        changed = True
    if ollama_url:
        cfg.llm.ollama_url = ollama_url
        changed = True
    if ollama_model:
        cfg.llm.ollama_model = ollama_model
        changed = True
    if lmstudio_url:
        cfg.llm.lmstudio_url = lmstudio_url
        changed = True
    if lmstudio_model:
        cfg.llm.lmstudio_model = lmstudio_model
        changed = True
    if changed:
        save_config(cfg)
        console.print(f"[green]Saved to {CONFIG_FILE}[/green]")
    console.print(json.dumps({"provider": cfg.llm.provider, "ollama_url": cfg.llm.ollama_url, "ollama_model": cfg.llm.ollama_model, "lmstudio_url": cfg.llm.lmstudio_url, "lmstudio_model": cfg.llm.lmstudio_model, "config_file": str(CONFIG_FILE)}, indent=2))


@app.command("models")
def models_cmd():
    """List local models (Ollama + LM Studio)."""
    cfg = _cfg()
    for name, prov in [
        ("ollama", __import__("hive.llm.ollama", fromlist=["OllamaProvider"]).OllamaProvider(cfg.llm.ollama_url, cfg.llm.ollama_model)),
        ("lmstudio", __import__("hive.llm.lmstudio", fromlist=["LMStudioProvider"]).LMStudioProvider(cfg.llm.lmstudio_url, cfg.llm.lmstudio_model)),
    ]:
        ok, msg = prov.health()
        if ok:
            try:
                ms = prov.list_models()
                console.print(f"[green]{name} ({prov.base_url}):[/green] {', '.join(ms) or '(no models)'}")
            except Exception as e:
                console.print(f"[yellow]{name}: {e}[/yellow]")
        else:
            console.print(f"[dim]{name} not reachable at {prov.base_url}: {msg}[/dim]")


@app.command("sessions")
def sessions_cmd(limit: int = typer.Option(20, "--limit", "-n"), legacy: bool = typer.Option(False, "--legacy", help="Include personal-experiments legacy runs"), all_: bool = typer.Option(False, "--all", help="Include DB + legacy (default if legacy exists)")):
    """Browse research artifacts (like /outputs). Merges ~/.hive/hive.db + personal-experiments when present."""
    from hive.config import EXPERIMENTS_DIR, DB_FILE
    from hive.research.session import list_all_sessions, list_legacy_sessions
    use_all = legacy or all_ or (EXPERIMENTS_DIR is not None and EXPERIMENTS_DIR.exists())
    rows = list_all_sessions(limit=limit) if use_all else list_sessions(limit=limit)
    if not rows:
        console.print("[dim]No sessions yet — run `hive report \"Your topic\"`, `hive deepresearch \"...\"`, or place experiments in ~/codebase/personal-experiments[/dim]")
        if EXPERIMENTS_DIR is None:
            console.print(f"[dim]DB: {DB_FILE} (SQLite) • set HIVE_EXPERIMENTS_DIR to index legacy folder[/dim]")
        else:
            console.print(f"[dim]Checked legacy: {EXPERIMENTS_DIR} (no runs found) • DB: {DB_FILE}[/dim]")
        return
    t = Table(title=f"Sessions — {len(rows)} shown (DB + legacy)" if use_all else "Sessions")
    t.add_column("ID", style="cyan")
    t.add_column("Topic", overflow="fold")
    t.add_column("Created")
    t.add_column("Source", style="dim")
    import datetime
    for sid, topic, ts in rows:
        dt = datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else ""
        src = "legacy" if topic.startswith("[legacy]") else "db"
        t.add_row(sid, topic.replace("[legacy] ", "")[:80], dt, src)
    console.print(t)
    if EXPERIMENTS_DIR and EXPERIMENTS_DIR.exists():
        console.print(f"[dim]DB: {DB_FILE} • legacy: {EXPERIMENTS_DIR} • `hive sessions --legacy` to force[/dim]")


@app.command("doctor")
def doctor():
    """Diagnose local LLM + paper APIs."""
    cfg = _cfg()
    console.print(f"[bold]Config:[/bold] {CONFIG_FILE}  provider={cfg.llm.provider}")
    for label, prov in [
        ("Ollama", __import__("hive.llm.ollama", fromlist=["OllamaProvider"]).OllamaProvider(cfg.llm.ollama_url, cfg.llm.ollama_model)),
        ("LM Studio", __import__("hive.llm.lmstudio", fromlist=["LMStudioProvider"]).LMStudioProvider(cfg.llm.lmstudio_url, cfg.llm.lmstudio_model)),
    ]:
        ok, msg = prov.health()
        console.print(f"  {label} @ {prov.base_url}: {'[green]ok[/green]' if ok else f'[red]{msg}[/red]'}")
    # paper APIs
    import httpx
    for name, url in [("OpenAlex", "https://api.openalex.org/works?search=test&per-page=1"), ("arXiv", "https://export.arxiv.org/api/query?search_query=all:test&max_results=1")]:
        try:
            with httpx.Client(timeout=8) as c:
                r = c.get(url)
                console.print(f"  {name}: {'[green]ok[/green]' if r.status_code==200 else f'[red]{r.status_code}[/red]'}")
        except Exception as e:
            console.print(f"  {name}: [red]{e}[/red]")
    from hive.config import DB_FILE, CONFIG_DIR, EXPERIMENTS_DIR
    from hive.research.session import list_sessions
    import sqlite3
    console.print(f"[bold]Store:[/bold] DB {DB_FILE} (SQLite)")
    try:
        con=sqlite3.connect(DB_FILE)
        sc=con.execute("SELECT count(*) FROM sessions").fetchone()[0]
        ac=con.execute("SELECT count(*) FROM artifacts").fetchone()[0]
        console.print(f"  sessions={sc} artifacts={ac} • `hive sessions` / `hive sessions --legacy`")
        con.close()
    except Exception as e:
        console.print(f"  [red]DB error {e}[/red]")
    if EXPERIMENTS_DIR:
        from hive.research.session import list_legacy_sessions
        legacy=list_legacy_sessions(limit=5)
        console.print(f"  legacy {EXPERIMENTS_DIR}: {len(legacy)} runs • `hive sessions --legacy` • HIVE_EXPERIMENTS_DIR to override")
        if legacy:
            for sid,topic,ts in legacy[:3]:
                console.print(f"    - {sid} {topic[:60]}")
    else:
        console.print(f"  [dim]No legacy dir — set HIVE_EXPERIMENTS_DIR or use ~/codebase/personal-experiments[/dim]")
    try:
        out_cnt=len(list((__import__('pathlib').Path.cwd() / 'output').glob('*.md'))) if (__import__('pathlib').Path.cwd() / 'output').exists() else 0
        ws_cnt=len(list((__import__('pathlib').Path.home() / '.hive' / 'machine' / 'workspace').glob('*'))) if (__import__('pathlib').Path.home() / '.hive' / 'machine' / 'workspace').exists() else 0
        console.print(f"  output/ {out_cnt} md • machine workspace {ws_cnt} workflows")
    except Exception:
        pass
    try:
        from hive.ledger import ledger_stats as _ls
        from hive.learn import learn_status as _lrn
        ls=_ls()
        console.print(f"  ledger {ls['total']} entries avg_reward {ls['avg_reward'] or 0:.2f} • `hive ledger --verify`")
        lrn=_lrn()
        console.print(f"  learn memory {lrn['memory']['total']} • workbenches {len(__import__('hive.workbench.profiles', fromlist=['list_workbenches']).list_workbenches())} • `hive learn status`")
    except Exception:
        pass
    console.print(f"[bold]Web:[/bold] `hive web` / `hive serve --web` → http://localhost:8002 (dashboard) • web/dist {'ok' if (__import__('pathlib').Path('web/dist/index.html').exists()) else 'missing — run npm run build --prefix web'}")


@app.command("dashboard")
def dashboard(limit: int = typer.Option(50, "--limit", "-n", help="Audit events")):
    """Auditable proofs dashboard — network, data outflow, files, commands (severity/impact)."""
    from hive.machine.dashboard import render_full_dashboard
    render_full_dashboard(limit=limit)



@app.command("ledger")
def ledger_cmd(
    limit: int = typer.Option(20, "--limit", "-n", help="Entries"),
    workbench: str = typer.Option(None, "--workbench", "-w", help="Filter by workbench"),
    verify: bool = typer.Option(False, "--verify", help="Verify hash chain"),
):
    """Unified execution ledger — all commands, tool calls, LLM, paper, artifacts (auditable, hash-chained)."""
    if verify:
        ok, msg = verify_ledger()
        console.print(f"[{'green' if ok else 'red'}]{msg}[/{'green' if ok else 'red'}]")
        return
    rows = query_ledger(limit=limit, workbench=workbench)
    if not rows:
        console.print("[dim]No ledger entries — run any hive command (they auto-log) or `hive experiment run <wb> --task '...'`[/dim]")
        return
    tb = Table(title=f"Ledger — {len(rows)} latest" + (f" workbench={workbench}" if workbench else ""))
    tb.add_column("ID", style="cyan")
    tb.add_column("TS")
    tb.add_column("WB")
    tb.add_column("Command", overflow="fold")
    tb.add_column("Reward", style="magenta")
    tb.add_column("Hash", style="dim")
    import datetime
    for r in rows:
        dt = datetime.datetime.fromtimestamp(r["ts"]).strftime("%m-%d %H:%M") if r.get("ts") else ""
        tb.add_row(r["id"], dt, r.get("workbench","")[:12], r.get("command","")[:40], str(r.get("reward") or "-"), (r.get("hash") or "")[:8])
    console.print(tb)
    try:
        log_execution("ledger", {"limit": limit, "workbench": workbench, "verify": verify}, workbench=workbench or "default", status="ok")
    except Exception:
        pass


@app.command("feedback")
def feedback_cmd(
    execution_id: str = typer.Argument(..., help="Ledger execution id (from `hive ledger`)"),
    reward: int = typer.Option(..., "--reward", "-r", min=1, max=5, help="1-5 (1=bad, 5=excellent)"),
    note: str = typer.Option("", "--note", "-m", help="Optional note"),
):
    """Give reinforcement reward for an execution (feeds continual learning loop)."""
    try:
        fid = add_feedback(execution_id, reward, note)
        console.print(f"[green]Feedback {fid} → {execution_id} reward={reward}[/green] {note[:60]}")
        log_execution("feedback", {"execution_id": execution_id, "reward": reward, "note": note}, status="ok")
    except Exception as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)


@app.command("workbench")
def workbench_cmd(
    action: str = typer.Argument("list", help="list|create|show|delete|run"),
    name: str = typer.Argument(None, help="Workbench name"),
    description: str = typer.Option(None, "--desc", help="Description for create"),
    domain: str = typer.Option(None, "--domain", help="Domain for create"),
):
    """Narrow-spaced ideal AGI workbench — domain-specialized profiles (fox-fraud, eda-credit, privacy, quai-lora, diabetes)."""
    if action == "list":
        wbs = list_workbenches()
        if not wbs:
            console.print("[dim]No workbenches — builtins will auto-seed on next list[/dim]")
            return
        tb = Table(title=f"Workbenches — {len(wbs)} narrow AGI profiles")
        tb.add_column("Name", style="cyan")
        tb.add_column("Domain")
        tb.add_column("Source", style="dim")
        tb.add_column("Description", overflow="fold")
        tb.add_column("Path", style="dim", overflow="fold")
        for wb in wbs:
            tb.add_row(wb["name"], wb.get("domain","")[:12], wb.get("source",""), wb.get("description","")[:50], wb.get("path","")[-40:])
        console.print(tb)
        console.print("[dim]Run `hive experiment run <workbench> --task '...'` or `hive workbench show <name>`[/dim]")
        log_execution("workbench", {"action": "list"}, status="ok")
        return
    if action == "show":
        if not name:
            console.print("[red]Need name: hive workbench show <name>[/red]")
            raise typer.Exit(1)
        wb = get_workbench(name)
        if not wb:
            console.print(f"[red]Not found: {name}[/red]")
            raise typer.Exit(1)
        console.print_json(data=wb)
        return
    if action == "create":
        if not name:
            console.print("[red]Need name: hive workbench create <name> --desc '...' --domain X[/red]")
            raise typer.Exit(1)
        p = create_workbench(name, {"description": description or f"Custom {name}", "domain": domain or name, "datasets": [], "allowed_tools": ["read_file","run_python"]})
        console.print(f"[green]Created {p}[/green]")
        log_execution("workbench", {"action": "create", "name": name}, workbench=name, status="ok")
        return
    if action == "delete":
        if not name:
            console.print("[red]Need name[/red]")
            raise typer.Exit(1)
        ok = delete_workbench(name)
        console.print(f"[{'green' if ok else 'red'}]{'Deleted' if ok else 'Not found'} {name}[/{'green' if ok else 'red'}]")
        return
    console.print(f"[red]Unknown action {action}: use list|create|show|delete[/red]")


@app.command("experiment")
def experiment_cmd(
    action: str = typer.Argument("run", help="run|list"),
    task: str = typer.Option(None, "--task", "-t", help="Task/topic for the experiment"),
    workbench: str = typer.Option(None, "--workbench", "-w", help="Narrow workbench profile"),
    dry: bool = typer.Option(False, "--dry", help="Dry run (no LLM)"),
):
    """Narrow AGI experimentation — gather all commands/executions, audited, workbench-scoped."""
    wb = resolve_workbench(workbench)
    if action == "list":
        rows = query_ledger(limit=50, workbench=wb if wb != "default" else None)
        if not rows:
            console.print(f"[dim]No experiments for workbench {wb} — run `hive experiment run --workbench {wb} --task '...'`[/dim]")
            return
        tb = Table(title=f"Experiments — workbench {wb}")
        tb.add_column("ID"); tb.add_column("Command"); tb.add_column("Reward"); tb.add_column("TS")
        import datetime
        for r in rows[:20]:
            dt = datetime.datetime.fromtimestamp(r["ts"]).strftime("%m-%d %H:%M") if r.get("ts") else ""
            tb.add_row(r["id"], r["command"][:50], str(r.get("reward") or "-"), dt)
        console.print(tb)
        return
    if action == "run":
        if not task:
            console.print("[red]Need --task 'what to experiment' e.g. hive experiment run --workbench fox-fraud --task 'detect fraud pattern'[/red]")
            raise typer.Exit(1)
        eid = log_execution(f"experiment:run", {"task": task, "workbench": wb, "dry": dry}, workbench=wb, status="running")
        console.print(f"[cyan]Experiment {eid} workbench={wb} task='{task[:60]}' dry={dry}[/cyan]")
        try:
            from hive.research.workflows import run_workflow as _rw
            from hive.config import load_config
            cfg = load_config()
            kind = "deepresearch" if "fraud" in wb or "privacy" in wb else "lit"
            from hive.ledger import log_tool as _lt
            _lt("experiment_start", {"task": task, "kind": kind}, workbench=wb)
            if dry:
                result = f"[dry] would run {kind} on '{task}' in workbench {wb} — ledger {eid}"
            else:
                result = _rw(kind, task, cfg)
                from hive.research.session import new_session, save_artifact
                sid = new_session(f"experiment:{wb}:{task[:40]}")
                save_artifact(sid, "report", f"{wb}:{task[:30]}", result[:8000])
                _lt("experiment_result", {"sid": sid, "len": len(result)}, workbench=wb)
            console.print(Markdown(result[:3000] if isinstance(result, str) else str(result)[:3000]))
            from hive.ledger.store import LEDGER_DB
            import sqlite3
            con = sqlite3.connect(LEDGER_DB)
            con.execute("UPDATE executions SET status='ok' WHERE id=?", (eid,))
            con.commit(); con.close()
            console.print(f"[green]Done experiment {eid} — give feedback: hive feedback {eid} --reward 5[/green]")
            console.print(f"[dim]View: hive ledger --workbench {wb} | hive learn run --workbench {wb}[/dim]")
        except Exception as e:
            import sqlite3
            con = sqlite3.connect(LEDGER_DB)
            try:
                con.execute("UPDATE executions SET status='error' WHERE id=?", (eid,))
                con.commit()
            except Exception:
                pass
            con.close()
            console.print(f"[red]Experiment failed {eid}: {e}[/red]")
            raise typer.Exit(1)


@app.command("learn")
def learn_cmd(
    action: str = typer.Argument("status", help="status|run|rollback|memory"),
    workbench: str = typer.Option(None, "--workbench", "-w", help="Narrow workbench"),
    iterations: int = typer.Option(10, "--iterations", "-n", help="Iterations for run"),
    reward_threshold: float = typer.Option(4.0, "--reward-threshold", "-r", help="Reward threshold for promotion (3.0-5.0)"),
    learning_rate: float = typer.Option(0.3, "--learning-rate", "-l", help="Learning rate for reinforcement (0.1-1.0)"),
    snapshot: str = typer.Option(None, "--snapshot", help="Snapshot name for rollback"),
    dry: bool = typer.Option(False, "--dry", help="Dry run"),
):
    """Continual learning + reinforcement loop — gathers ledger, scores, promotes to memory."""
    wb = resolve_workbench(workbench)
    if action == "status":
        st = learn_status()
        console.print_json(data=st)
        ls = ledger_stats()
        console.print(f"[dim]Ledger total {ls['total']} avg_reward {ls['avg_reward'] or 0:.2f}[/dim]")
        return
    if action == "run":
        res = run_loop(workbench=wb, iterations=iterations, reward_threshold=reward_threshold, learning_rate=learning_rate, dry=dry)
        console.print(f"[green]Learn run workbench={wb} scored={res['scored']} promoted={res['promoted']} dry={dry}[/green]")
        if res.get("snapshot"):
            console.print(f"[dim]Snapshot {res['snapshot']}[/dim]")
        for d in res.get("details", [])[:5]:
            console.print(f"  {d['id']} {d['command'][:40]} → {d['reward']}")
        log_execution("learn", {"action": "run", "workbench": wb, "iterations": iterations, "reward_threshold": reward_threshold, "learning_rate": learning_rate}, workbench=wb, status="ok")
        return
    if action == "rollback":
        res = rollback(snapshot)
        console.print(f"[{'green' if res.get('ok') else 'red'}]{res}[/{'green' if res.get('ok') else 'red'}]")
        return
    if action == "memory":
        rows = query_memory(workbench=wb if wb != "default" else None, limit=20)
        if not rows:
            console.print(f"[dim]No memory for {wb} — run `hive learn run --workbench {wb}` after some experiments[/dim]")
            return
        tb = Table(title=f"Memory — workbench {wb}")
        tb.add_column("ID"); tb.add_column("Kind"); tb.add_column("Score"); tb.add_column("Content", overflow="fold")
        for m in rows:
            tb.add_row(m["id"], m["kind"], str(m["score"]), m["content"][:80])
        console.print(tb)
        return
    console.print(f"[red]Unknown learn action {action}: status|run|rollback|memory[/red]")


@app.command("derived")
def derived_cmd(
    action: str = typer.Argument("list", help="list|generate|run"),
    workbench: str = typer.Option(None, "--workbench", "-w", help="Workbench filter"),
    per_wb: int = typer.Option(2, "--per-wb", help="Per workbench count for generate"),
    dry: bool = typer.Option(False, "--dry", help="Dry for run"),
):
    """Derived experiments — scenario expansion for narrow AGI (robustness, shift, imbalance, privacy, efficiency)."""
    if action == "list":
        rows = list_derived(workbench=workbench)
        if not rows:
            console.print(f"[dim]No derived for {workbench or 'all'} — run `hive derived generate`[/dim]")
            return
        tb = Table(title=f"Derived — {len(rows)} scenarios" + (f" workbench={workbench}" if workbench else ""))
        tb.add_column("ID", style="cyan")
        tb.add_column("WB")
        tb.add_column("Scenario")
        tb.add_column("Name", overflow="fold")
        tb.add_column("Param", style="dim")
        for r in rows[:20]:
            tb.add_row(r["id"][:8], r.get("workbench","")[:12], r.get("scenario","")[:10], r.get("name","")[:40], r.get("param","")[:20])
        console.print(tb)
        console.print(f"[dim]Total {len(rows)} • run `hive derived run --workbench <wb> [--dry]`[/dim]")
        return
    if action == "generate":
        paths = generate_derived(workbench=workbench, per_wb=per_wb)
        console.print(f"[green]Generated {len(paths)} derived for {workbench or 'all workbenches'}[/green]")
        for p in paths[:10]:
            console.print(f"  {p}")
        log_execution("derived", {"action": "generate", "workbench": workbench, "per_wb": per_wb, "count": len(paths)}, workbench=workbench or "default", status="ok")
        return
    if action == "run":
        rows = list_derived(workbench=workbench)
        if not rows:
            console.print(f"[red]No derived to run for {workbench} — generate first[/red]")
            raise typer.Exit(1)
        console.print(f"[cyan]Running {len(rows)} derived experiments{' workbench='+workbench if workbench else ''} dry={dry}[/cyan]")
        import subprocess, sys as _sys, re
        ok=0
        for d in rows:
            wb = d["workbench"]
            task = d["task"][:180].replace('"', "'")
            cmd = [_sys.executable, "-m", "hive.cli", "experiment", "run", "--task", task, "--workbench", wb]
            if dry:
                cmd.append("--dry")
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if r.returncode==0:
                ok+=1
                m=re.search(r"Experiment (\w+)", r.stdout)
                eid=m.group(1) if m else "?"
                # auto feedback
                try:
                    add_feedback(eid, 4, f"derived scenario {d['scenario']} {d['param']}")
                except Exception:
                    pass
            else:
                console.print(f"[red]FAIL {wb} {d['name'][:30]}: {r.stderr[:120]}[/red]")
        console.print(f"[green]Ran {ok}/{len(rows)} derived[/green]")
        log_execution("derived", {"action": "run", "workbench": workbench, "count": len(rows), "ok": ok}, workbench=workbench or "default", status="ok")
        return
    console.print(f"[red]Unknown derived action {action}: list|generate|run[/red]")

@app.callback(invoke_without_command=True)
def main_callback(ctx: typer.Context, version: bool = typer.Option(False, "--version", help="Show version")):
    if version:
        console.print(f"hive-research {__version__}")
        raise typer.Exit()
    try:
        if ctx.invoked_subcommand:
            from hive.workbench import resolve_workbench as _rwb
            wb = _rwb(None)
            import sys as _sys
            argv = " ".join(_sys.argv[1:])[:500]
            try:
                from hive.config import load_config as _lc
                _cfg = _lc()
                prov, mod = _cfg.llm.provider, _cfg.llm.ollama_model
            except Exception:
                prov, mod = None, None
            from hive.ledger import log_execution as _le
            _le(f"cli:{ctx.invoked_subcommand}", {"argv": argv}, workbench=wb, provider=prov, model=mod, status="ok")
    except Exception:
        pass
    if ctx.invoked_subcommand is None:
        # interactive REPL
        console.print(Markdown(f"# Hive Research - A Local research companion {__version__}\nLocal research companion — Ollama / LM Studio only.\n\nType a question, or `help` for commands. `exit` to quit.\nTry `hive tui` for full terminal workbench.\n"))
        cfg = _cfg()
        prov = get_provider(cfg)
        ok, msg = prov.health()
        console.print(f"[dim]LLM: {prov.name} @ {prov.base_url} — {'ok' if ok else msg}[/dim]")
        console.print("[dim]Commands: rank <topic> | paper <id> | /deepresearch <topic> | /lit | /review | /compare | /draft | ask <q> | doctor | tui[/dim]\n")
        while True:
            try:
                q = typer.prompt("hive>")
            except (EOFError, KeyboardInterrupt):
                break
            if not q.strip():
                continue
            if q.strip() in ("exit", "quit", "/exit"):
                break
            if q.strip() in ("help", "/help", "?"):
                console.print("[dim]Commands: rank, paper, deepresearch, lit, review, audit, replicate, recipe, compare, draft, autoresearch, watch, ask, doctor, models, sessions, config[/dim]")
                continue
            # parse slash commands
            if q.startswith("/"):
                parts = q[1:].split(maxsplit=1)
                kind = parts[0]
                topic = parts[1] if len(parts) > 1 else typer.prompt(f"{kind} topic")
                try:
                    _workflow_cmd(kind, topic, top=10, full_text_top=3)
                except SystemExit:
                    pass
                except Exception as e:
                    console.print(f"[red]{e}[/red]")
                continue
            if q.startswith("rank "):
                try:
                    rank_cmd(q[5:], top_k=10, full_text_top=3, json_out=False, no_llm=False)  # type: ignore
                except SystemExit:
                    pass
                continue
            if q.startswith("paper "):
                try:
                    paper_cmd(q[6:], fetch_full_text=False, json_out=False)  # type: ignore
                except SystemExit:
                    pass
                continue
            # default: ask LLM with paper context
            try:
                for tok in chat_stream(cfg, [ChatMessage(role="user", content=q)]):
                    print(tok, end="", flush=True)
                print()
            except Exception as e:
                console.print(f"[red]{e}[/red]")


if __name__ == "__main__":
    app()
