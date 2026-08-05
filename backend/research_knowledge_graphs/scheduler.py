"""Scenario scheduler: per-scenario freshness cadence for the Research Workbench.

A background task started with the app lifespan periodically checks every
scenario's ``last_built_at`` / ``last_loop``. When a scenario is configured with
a ``schedule`` block and its corpus is older than ``interval_hours``, it
triggers ``build_corpus`` (and, unless disabled, ``run_synthesis``) so the
knowledge graph and its domain reports stay fresh without manual pokes.

The scheduler is deliberately conservative:

- opt-in: global ``schedule.enabled`` (config.yaml) AND per-scenario
  ``schedule: {interval_hours, enabled}`` must both be on;
- it never queues onto a scenario that already has a running job (same
  per-scenario guard as the router's POST endpoints);
- cadence is configurable globally (``schedule.check_minutes``) and per
  scenario (``interval_hours``).
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable

from .research_loop import ResearchWorkbench

logger = logging.getLogger(__name__)


def _iso_to_epoch(value: str | None) -> float | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (ValueError, TypeError):
        return None


def _last_activity_epoch(sc: dict[str, Any]) -> float | None:
    """Newest of last_built_at / last_loop.finished_at / last_loop timestamp."""
    cands = [_iso_to_epoch(sc.get("last_built_at"))]
    last_loop = sc.get("last_loop") or {}
    cands.append(_iso_to_epoch(last_loop.get("finished_at") if isinstance(last_loop, dict)
                               else last_loop))
    fresh = [c for c in cands if c is not None]
    return max(fresh) if fresh else None


def _scenario_due(sc: dict[str, Any], now: float) -> bool:
    """A scenario is due when it is enabled with an interval and its newest
    corpus/loop activity is older than the interval (or it has never run)."""
    sched = (sc.get("schedule") or {})
    if not sched.get("enabled", False):
        return False
    interval_hours = sched.get("interval_hours")
    try:
        interval_hours = float(interval_hours)
    except (TypeError, ValueError):
        return False
    if not interval_hours or interval_hours <= 0:
        return False
    last = _last_activity_epoch(sc)
    if last is None:
        return True  # never built — due
    return (now - last) >= interval_hours * 3600


class ScenarioScheduler:
    def __init__(
        self,
        workbench: ResearchWorkbench | Callable[[], ResearchWorkbench],
        check_minutes: int | None = None,
        synthesize: bool | None = None,
    ) -> None:
        self._wb = workbench
        self._check_interval = max(1, int(check_minutes or 60)) * 60
        self._synthesize = synthesize
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    def _get_wb(self) -> ResearchWorkbench:
        return self._wb() if callable(self._wb) else self._wb

    # ----------------------------------------------------------- cadence ----

    def due_scenarios(self, scenarios: list[dict[str, Any]] | None = None,
                      now: float | None = None) -> list[str]:
        wb = self._get_wb()
        if scenarios is None:
            scenarios = wb.list()
        now = now if now is not None else time.time()
        return [sc["id"] for sc in scenarios if _scenario_due(sc, now)]

    async def run_one(self, sid: str) -> dict[str, Any]:
        """Build (and synthesize) one due scenario, honoring the per-scenario
        guard: skip when the scenario already has a running job."""
        wb = self._get_wb()
        st = wb.status(sid).get("status") or {}
        if st.get("phase") not in ("idle", "done", "error", "interrupted"):
            logger.info("scheduler: %s busy (%s) — skipping", sid, st.get("phase"))
            return {"status": "skipped", "reason": "busy"}
        logger.info("scheduler: running scheduled refresh for %s", sid)
        try:
            build = await asyncio.to_thread(wb.build_corpus, sid)
            out: dict[str, Any] = {"build": build}
            if self._synthesize if self._synthesize is not None else True:
                synth = await asyncio.to_thread(wb.run_synthesis, sid)
                out["synthesis"] = synth
            return {"status": "ok", **out}
        except Exception as exc:  # noqa: BLE001
            logger.warning("scheduler: refresh of %s failed: %s", sid, exc)
            return {"status": "error", "error": f"{type(exc).__name__}: {exc}"}

    async def tick(self, scenarios: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
        async with self._lock:
            out: list[dict[str, Any]] = []
            for sid in self.due_scenarios(scenarios):
                out.append(await self.run_one(sid))
            return out

    # -------------------------------------------------------- task loop ----

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception as exc:  # noqa: BLE001
                logger.warning("scheduler tick failed: %s", exc)
            await asyncio.sleep(self._check_interval)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())
            logger.info("scenario scheduler started (check every %ss)", self._check_interval)

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
