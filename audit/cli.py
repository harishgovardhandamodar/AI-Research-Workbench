"""``agent-audit`` command-line interface.

Commands:
    proxy       start the transparent MCP proxy (stdio or HTTP)
    dashboard   launch the local Streamlit audit dashboard
    query       search the audit log
    verify      check the hash-chain integrity of the JSONL audit log
    baseline    recompute per-agent deviation baselines
    export      dump events as JSON / CSV
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from . import __version__
from .models import AuditEvent
from .store import LocalAuditStore


def _store(ns: argparse.Namespace) -> LocalAuditStore:
    return LocalAuditStore(ns.dir, jsonl_chain=not getattr(ns, "no_jsonl", False))


def _now_ts() -> float:
    import time
    return time.time()


# ------------------------------------------------------------------ commands ---
def cmd_proxy(ns: argparse.Namespace) -> int:
    from .proxy import AuditMCPProxy

    proxy = AuditMCPProxy(
        server_cmd=ns.server, server_args=ns.arg,
        http_url=ns.http, http_headers=_parse_headers(ns.headers),
        agent_id=ns.agent_id, store=_store(ns))

    async def _run():
        if ns.http_server:
            await proxy.run_http(host=ns.host, port=ns.port)
        else:
            await proxy.run_stdio()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        return 0
    return 0


def cmd_query(ns: argparse.Namespace) -> int:
    store = _store(ns)
    events = store.query(
        agent_id=ns.agent, source=ns.source, tool_name=ns.tool,
        severity=ns.severity, session_id=ns.session,
        limit=ns.limit,
        since=_time_arg(ns.since),
        until=_time_arg(ns.until))
    for ev in events:
        print(json.dumps(ev, indent=2, default=str))
    print(f"# {len(events)} event(s)", file=sys.stderr)
    return 0


def cmd_verify(ns: argparse.Namespace) -> int:
    store = _store(ns)
    result = store.verify_chain()
    print(json.dumps(result, indent=2, default=str))
    return 0 if result.get("ok") else 1


def cmd_baseline(ns: argparse.Namespace) -> int:
    from .deviation import DeviationDetector

    store = _store(ns)
    det = DeviationDetector()
    bl = det.compute_baseline(store, ns.agent)
    out = {}
    for agent, baseline in bl.items():
        out[agent] = {
            "samples": baseline.samples,
            "tools": len(baseline.tool_counts),
            "bigrams": len(baseline.sequences),
            "data_classes": len(baseline.data_classes),
            "fs_roots": len(baseline.fs_roots),
            "network_destinations": len(baseline.network_destinations),
            "working_hours": list(baseline.working_hours),
            "top_tools": sorted(baseline.tool_counts.items(),
                                key=lambda kv: kv[1], reverse=True)[:10],
        }
    print(json.dumps(out, indent=2, default=str))
    return 0


def cmd_export(ns: argparse.Namespace) -> int:
    store = _store(ns)
    events = store.export_events(limit=ns.limit, agent_id=ns.agent,
                                 severity=ns.severity, source=ns.source)
    if ns.format == "csv":
        import csv
        import io

        buf = io.StringIO()
        fields = ["event_id", "timestamp", "agent_id", "source", "method",
                  "tool_name", "severity", "duration_ms", "policy_decision",
                  "event_hash"]
        writer = csv.DictWriter(sys.stdout, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for ev in events:
            writer.writerow(ev)
        return 0
    print(json.dumps(events, indent=2, default=str))
    return 0


def cmd_dashboard(ns: argparse.Namespace) -> int:
    from .dashboard import run_dashboard

    return run_dashboard(ns.dir)


# ---------------------------------------------------------------- argparse ---
def _default_dir() -> str:
    return os.environ.get("AUDIT_DIR", "~/.agent-audit")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-audit",
        description="Local agent audit trail: MCP proxy, dashboard, query, verify.")
    parser.add_argument("--version", action="version", version=f"agent-audit {__version__}")
    parser.add_argument("--dir", default=_default_dir(),
                        help="audit store directory (default $AUDIT_DIR or ~/.agent-audit)")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    p = sub.add_parser("proxy", help="start the transparent MCP proxy")
    p.add_argument("--server", default=None,
                   help="downstream server command, e.g. \"python -m my_mcp_server\"")
    p.add_argument("--arg", action="append", default=[],
                   help="extra server args (repeatable)")
    p.add_argument("--http", default=None,
                   help="downstream streamable-HTTP MCP URL")
    p.add_argument("--headers", default="",
                   help="JSON object of headers for --http downstream")
    p.add_argument("--agent-id", default="proxy-client")
    p.add_argument("--http-server", action="store_true",
                   help="also serve the proxy itself over HTTP on --port")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8010)
    p.add_argument("--no-jsonl", action="store_true")

    p = sub.add_parser("query", help="search events")
    p.add_argument("--agent")
    p.add_argument("--source")
    p.add_argument("--tool")
    p.add_argument("--severity", choices=["info", "warning", "critical"])
    p.add_argument("--session")
    p.add_argument("--since", help="ISO timestamp or unix seconds")
    p.add_argument("--until", help="ISO timestamp or unix seconds")
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--no-jsonl", action="store_true")

    sub.add_parser("verify", help="verify JSONL hash-chain integrity")
    p = sub.add_parser("baseline", help="recompute deviation baselines")
    p.add_argument("--agent")
    p.add_argument("--no-jsonl", action="store_true")

    p = sub.add_parser("export", help="export events")
    p.add_argument("--format", choices=["json", "csv"], default="json")
    p.add_argument("--agent")
    p.add_argument("--severity", choices=["info", "warning", "critical"])
    p.add_argument("--source")
    p.add_argument("--limit", type=int, default=5000)
    p.add_argument("--no-jsonl", action="store_true")

    p = sub.add_parser("dashboard", help="launch the Streamlit dashboard")
    p.add_argument("--no-jsonl", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    ns = parser.parse_args(argv)
    ns.dir = str(Path(ns.dir).expanduser())
    handlers = {
        "proxy": cmd_proxy,
        "query": cmd_query,
        "verify": cmd_verify,
        "baseline": cmd_baseline,
        "export": cmd_export,
        "dashboard": cmd_dashboard,
    }
    handler = handlers.get(ns.command)
    if handler is None:
        parser.print_help()
        return 2
    return handler(ns)


def _parse_headers(raw: str) -> dict:
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def _time_arg(value: str | None):
    if not value:
        return None
    if value.isdigit():
        return float(value)
    from datetime import datetime

    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


if __name__ == "__main__":
    raise SystemExit(main())
