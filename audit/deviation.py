"""Deviation / anomaly detector.

Maintains per-agent baselines (tool frequency, tool sequences, data classes,
filesystem roots, network destinations) and flags events that deviate:

  * novel tool call
  * tool-call frequency spike (mean + 2 stddev over the baseline window)
  * novel tool sequence (bigram not previously observed)
  * access to previously unseen data classes
  * filesystem access outside the baseline roots
  * network destination not previously seen
  * permission denial/ask immediately followed by an override of the same tool
  * high-risk tool used outside the agent's normal working hours

Detection is intentionally statistical-and-simple (the spec's "leave room for
later ML"): thresholds are configurable and baselines are recomputed from the
store with :meth:`DeviationDetector.compute_baseline`.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from statistics import mean, pstdev
from typing import Any

from .models import DeviationRecord
from .store import LocalAuditStore


@dataclass
class AgentBaseline:
    agent_id: str
    tool_counts: dict[str, int] = field(default_factory=dict)
    tool_times: dict[str, list[float]] = field(default_factory=dict)  # ms
    sequences: set[tuple[str, str]] = field(default_factory=set)      # bigrams
    data_classes: set[str] = field(default_factory=set)
    fs_roots: set[str] = field(default_factory=set)
    fs_paths: set[str] = field(default_factory=set)
    network_destinations: set[str] = field(default_factory=set)
    working_hours: tuple[int, int] = (0, 24)  # (start_hour, end_hour) most active
    hour_histogram: dict[int, int] = field(default_factory=dict)
    samples: int = 0
    _last_tool: str | None = None

    def is_novel_tool(self, tool: str) -> bool:
        return bool(tool) and tool not in self.tool_counts

    def is_novel_bigram(self, prev: str | None, tool: str) -> bool:
        return bool(prev) and bool(tool) and (prev, tool) not in self.sequences

    def frequency_std(self, tool: str) -> tuple[float, float]:
        """(mean, stddev) of inter-call gaps for a tool, or (0, 0) if unseen."""
        times = self.tool_times.get(tool, [])
        if len(times) < 3:
            return 0.0, 0.0
        gaps = [b - a for a, b in zip(times, times[1:]) if b > a]
        if len(gaps) < 2:
            return 0.0, 0.0
        return mean(gaps), (pstdev(gaps) if len(gaps) > 1 else 0.0)

    def normal_hours(self, hour: int, tolerance: int = 2) -> bool:
        lo, hi = self.working_hours
        return lo - tolerance <= hour <= hi + tolerance

    def absorb(self, ev: dict) -> None:
        """Fold one (already-checked) event into the baseline so the next check
        treats it as history rather than novelty."""
        tool = ev.get("tool_name")
        ts = _num(ev.get("timestamp")) or time.time()
        if tool:
            self.tool_counts[tool] = self.tool_counts.get(tool, 0) + 1
            self.tool_times.setdefault(tool, []).append(ts)
            prev = self._last_tool
            if prev:
                self.sequences.add((prev, tool))
            self._last_tool = tool
        for c in (ev.get("result_summary") or {}).get("data_classes") or []:
            self.data_classes.add(str(c))
        fs = ev.get("filesystem") or {}
        if fs.get("path"):
            p = str(fs["path"])
            self.fs_paths.add(p)
            root = _fs_root(p)
            if root:
                self.fs_roots.add(root)
        nw = ev.get("network") or {}
        if nw.get("destination"):
            self.network_destinations.add(str(nw["destination"]))


class DeviationDetector:
    def __init__(self, freq_z: float = 2.0, hours: tuple[int, int] = (8, 22),
                 overrides_window_s: float = 300.0):
        self.freq_z = freq_z
        self.hours = hours
        self.overrides_window_s = overrides_window_s
        self.baselines: dict[str, AgentBaseline] = {}
        self._recent_denials: list[dict] = []  # {ts, agent, tool}
        self._last_seq: dict[str, str] = {}    # agent_id -> previous tool

    # ------------------------------------------------------------ baselines ---
    def compute_baseline(self, store: LocalAuditStore, agent_id: str | None = None,
                         max_events: int = 5000,
                         before_ts: float | None = None) -> dict[str, AgentBaseline]:
        """Rebuild baselines from stored events (per agent, or all agents).

        ``before_ts`` optionally limits the baseline to events strictly older
        than a timestamp, so a scan can compare *new* events against a snapshot
        that excludes them (correct novelty detection).
        """
        bl: dict[str, AgentBaseline] = {}
        agents = [agent_id] if agent_id else store.active_agents()
        for aid in agents:
            if not aid:
                continue
            baseline = AgentBaseline(agent_id=aid)
            rows = store.query(agent_id=aid, limit=max_events)
            for d in reversed(rows):  # store returns newest-first
                ev = d
                ts = _num(ev.get("timestamp"))
                if before_ts is not None and ts is not None and ts >= before_ts:
                    continue
                baseline.absorb(ev)
                if ts:
                    hour = datetime.fromtimestamp(ts).hour
                    baseline.hour_histogram[hour] = baseline.hour_histogram.get(hour, 0) + 1
            if baseline.hour_histogram:
                peak_hour = max(baseline.hour_histogram, key=baseline.hour_histogram.get)
                baseline.working_hours = (
                    max(0, peak_hour - 4), min(23, peak_hour + 4))
            baseline.samples = len(rows)
            bl[aid] = baseline
        self.baselines.update(bl)
        return bl

    def baseline_for(self, agent_id: str, store: LocalAuditStore) -> AgentBaseline:
        if agent_id not in self.baselines:
            self.compute_baseline(store, agent_id)
        return self.baselines[agent_id]

    # --------------------------------------------------------------- checks ---
    def check_event(self, store: LocalAuditStore, event: dict,
                    compute_missing: bool = True) -> list[DeviationRecord]:
        """Inspect a single (already persisted) event dict for deviations.

        Returns zero or more DeviationRecords; each record is persisted by the
        caller via :meth:`LocalAuditStore.record_deviation`.
        """
        agent = event.get("agent_id") or ""
        if not agent:
            return []
        baseline = self.baseline_for(agent, store) if compute_missing else \
            self.baselines.get(agent)
        if baseline is None or baseline.samples < 3:
            return []  # not enough history to call anything a deviation
        tool = event.get("tool_name")
        ts = _num(event.get("timestamp")) or time.time()
        findings: list[DeviationRecord] = []

        def dev(rule: str, severity: str, explanation: str, detail: dict) -> DeviationRecord:
            return DeviationRecord(
                agent_id=agent, rule=rule, severity=severity,
                explanation=explanation, event_ids=[event.get("event_id", "")],
                detail=detail)

        # 1) novel tool
        if tool and baseline.is_novel_tool(tool):
            findings.append(dev(
                "novel_tool", "warning",
                f"Agent {agent} called tool {tool!r} for the first time.",
                {"tool": tool, "first_seen": True}))

        # 2) novel bigram sequence
        prev = self._last_seq.get(agent)
        if tool and baseline.is_novel_bigram(prev, tool):
            findings.append(dev(
                "novel_sequence", "info",
                f"New tool sequence {prev!r} → {tool!r} not seen in the agent's history.",
                {"prev": prev, "tool": tool}))
        if tool:
            self._last_seq[agent] = tool

        # 3) tool frequency spike
        if tool and baseline.tool_times.get(tool):
            mean_gap, std_gap = baseline.frequency_std(tool)
            last_call = baseline.tool_times[tool][-1]
            if mean_gap > 0 and std_gap > 0 and ts is not None and last_call:
                gap = ts - last_call
                if gap > 0 and gap < mean_gap - self.freq_z * std_gap:
                    findings.append(dev(
                        "tool_frequency_spike", "warning",
                        f"Agent {agent} called {tool!r} {max(0, round(gap))}s after "
                        f"the previous call (baseline mean {mean_gap:.0f}s, "
                        f"σ {std_gap:.0f}s) — unusual burst.",
                        {"tool": tool, "gap_s": round(gap, 2),
                         "baseline_mean_s": round(mean_gap, 2),
                         "baseline_std_s": round(std_gap, 2)}))

        # 4) unseen data class
        rs = event.get("result_summary") or {}
        for c in rs.get("data_classes") or []:
            if str(c) not in baseline.data_classes:
                findings.append(dev(
                    "unseen_data_class", "warning",
                    f"Agent {agent} touched previously unseen data class {str(c)!r}.",
                    {"data_class": str(c)}))
                baseline.data_classes.add(str(c))

        # 5) filesystem outside baseline roots
        fs = event.get("filesystem") or {}
        path = fs.get("path")
        if path:
            root = _fs_root(str(path))
            if root and baseline.fs_roots and root not in baseline.fs_roots \
                    and str(path) not in baseline.fs_paths:
                findings.append(dev(
                    "unseen_path", "warning",
                    f"Filesystem access outside the agent's baseline roots: {str(path)!r}.",
                    {"path": str(path), "root": root,
                     "baseline_roots": sorted(baseline.fs_roots)[:20]}))

        # 6) unseen network destination
        nw = event.get("network") or {}
        dst = nw.get("destination")
        if dst and baseline.network_destinations and \
                str(dst) not in baseline.network_destinations:
            findings.append(dev(
                "unseen_network_destination", "warning",
                f"Agent {agent} contacted a network destination it has never used: {dst}.",
                {"destination": str(dst)}))

        # 7) high-risk tool outside normal hours
        hour = datetime.fromtimestamp(ts).hour
        if tool and hour not in range(*baseline.working_hours):
            if _high_risk(tool):
                findings.append(dev(
                    "high_risk_outside_hours", "critical",
                    f"High-risk tool {tool!r} used at {hour:02d}:00 — outside the "
                    f"agent's typical working window "
                    f"({baseline.working_hours[0]:02d}–{baseline.working_hours[1]:02d}).",
                    {"tool": tool, "hour": hour,
                     "working_hours": list(baseline.working_hours)}))

        # 8) denial/ask followed by override
        policy = event.get("policy_decision") or {}
        outcome = policy.get("outcome")
        if outcome == "DENY" or outcome == "ASK":
            self._recent_denials.append({"ts": ts, "agent": agent,
                                         "tool": tool or "", "event": event})
        elif outcome == "OVERRIDE" and tool:
            for d in self._recent_denials:
                if d["agent"] == agent and d["tool"] == tool and \
                        ts - d["ts"] <= self.overrides_window_s:
                    findings.append(dev(
                        "denial_then_override", "critical",
                        f"Tool {tool!r} was {d['event'].get('policy_decision', {}).get('outcome', 'restricted')} "
                        f"then overridden by {agent} within "
                        f"{self.overrides_window_s:.0f}s — review the reason.",
                        {"tool": tool, "within_s": round(ts - d["ts"], 1)}))
                    break
        return findings

    def run(self, store: LocalAuditStore, agent_id: str | None = None,
            limit: int = 500, since: float | None = None) -> list[DeviationRecord]:
        """Scan recent events and persist every deviation found.

        ``since`` (unix seconds) splits the store: events older than ``since``
        seed the baseline; events at/after ``since`` are checked against it and
        folded into the baseline as they are inspected (so a run correctly
        flags tools/paths/destinations the agent has never used before).
        """
        self.compute_baseline(store, agent_id, max_events=5000, before_ts=since)
        out: list[DeviationRecord] = []
        rows = store.query(agent_id=agent_id, limit=limit)
        for d in reversed(rows):
            ts = _num(d.get("timestamp"))
            if since is not None and (ts is None or ts < since):
                continue
            for rec in self.check_event(store, d, compute_missing=False):
                store.record_deviation(rec)
                out.append(rec)
            baseline = self.baselines.get(d.get("agent_id"))
            if baseline is not None:
                baseline.absorb(d)
        return out


def _num(v) -> float | None:
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v)
        except ValueError:
            try:
                from datetime import datetime

                return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
            except ValueError:
                return None
    return None


def _fs_root(path: str) -> str | None:
    """Reduce a filesystem path to a coarse root for baseline comparison."""
    p = path.replace("\\", "/")
    if p.startswith(("/", "./", "../")):
        parts = [x for x in p.split("/") if x]
        if not parts:
            return "/"
        return "/" + parts[0] if len(parts) == 1 else "/" + "/".join(parts[:2])
    return None


def _high_risk(tool: str) -> bool:
    from .policy import HIGH_RISK_TOOLS
    if tool in HIGH_RISK_TOOLS:
        return True
    return tool.startswith(("privacy__", "autoresearch__", "mcp__", "github__"))
