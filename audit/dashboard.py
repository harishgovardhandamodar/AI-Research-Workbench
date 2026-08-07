"""Optional Streamlit dashboard for the audit store.

Kept import-light: ``streamlit`` is only imported when the dashboard is
launched (``agent-audit dashboard``), so the core system has zero extra
runtime dependencies.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .store import LocalAuditStore


def run_dashboard(dir_path: str = "~/.agent-audit") -> int:
    try:
        import streamlit as st
    except ImportError:
        print("Streamlit is required for the dashboard:\n"
              "  pip install streamlit", file=__import__("sys").stderr)
        return 1

    st.set_page_config(page_title="Agent Audit Trail", layout="wide")
    store = LocalAuditStore(Path(dir_path).expanduser())

    with st.sidebar:
        st.header("🔍 Audit Trail")
        days = st.slider("Lookback (days)", 1, 90, 7)
        agents = store.active_agents()
        agent = st.selectbox("Agent", ["(all)"] + agents)
        sev = st.selectbox("Severity", ["(all)", "info", "warning", "critical"])

    since = datetime.now(timezone.utc) - timedelta(days=days)
    kw = {"since": since}
    if agent != "(all)":
        kw["agent_id"] = agent
    if sev != "(all)":
        kw["severity"] = sev

    # ---------------------------------------------------------- overview ---
    st.title("Agent Audit Trail")
    summ = store.summary(since)
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Events", summ["total"])
    c2.metric("Critical", summ["critical"])
    c3.metric("Overrides", summ["overrides"])
    c4.metric("Denials", summ["denials"])
    c5.metric("Open deviations", summ["open_deviations"])

    # --------------------------------------------------------- deviations ---
    devs = store.list_deviations(agent_id=(None if agent == "(all)" else agent))
    open_devs = [d for d in devs if not d["reviewed"]]
    st.subheader(f"Open deviations ({len(open_devs)})")
    for d in open_devs[:20]:
        with st.expander(f"[{d['severity'].upper()}] {d['rule']} — {d['explanation']}"):
            st.write(d["detail"])
            if st.button("Mark reviewed", key=d["deviation_id"]):
                store.mark_deviation_reviewed(d["deviation_id"], reviewed=True,
                                              reviewed_by="dashboard")
                st.rerun()

    # ------------------------------------------------------------- events ---
    st.subheader("Recent events")
    events = store.query(limit=200, **kw)
    rows = [{
        "time": e.get("timestamp"),
        "agent": e.get("agent_id"),
        "tool": e.get("tool_name") or e.get("method"),
        "source": e.get("source"),
        "severity": e.get("severity"),
        "duration_ms": round(e.get("duration_ms") or 0, 1),
        "policy": (e.get("policy_decision") or {}).get("outcome", ""),
    } for e in events]
    st.dataframe(rows, use_container_width=True)

    # -------------------------------------------------------------- chain ---
    with st.expander("Hash-chain integrity"):
        st.json(store.verify_chain())

    return 0
