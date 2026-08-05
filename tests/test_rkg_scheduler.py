"""B1: per-scenario scheduler cadence, guard interaction, and run_one chaining."""

from __future__ import annotations

import time
import unittest
from datetime import datetime, timedelta, timezone

from backend.research_knowledge_graphs.scheduler import (
    ScenarioScheduler,
    _last_activity_epoch,
    _scenario_due,
)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _scenario(sid, *, enabled=True, interval_hours=24, last_built=None, last_loop=None):
    sc = {
        "id": sid,
        "schedule": {"enabled": enabled, "interval_hours": interval_hours},
        "last_built_at": _iso(last_built) if last_built else None,
        "last_loop": last_loop,
    }
    if sc["last_loop"] is None:
        sc.pop("last_loop", None)
    return sc


class DueTests(unittest.TestCase):
    def test_due_when_never_run(self):
        sc = _scenario("a", last_built=None)
        self.assertTrue(_scenario_due(sc, time.time()))

    def test_due_when_older_than_interval(self):
        old = datetime.now(timezone.utc) - timedelta(hours=48)
        sc = _scenario("a", last_built=old)
        self.assertTrue(_scenario_due(sc, time.time()))

    def test_not_due_when_fresh(self):
        fresh = datetime.now(timezone.utc) - timedelta(hours=1)
        sc = _scenario("a", last_built=fresh)
        self.assertFalse(_scenario_due(sc, time.time()))

    def test_disabled_never_due(self):
        old = datetime.now(timezone.utc) - timedelta(hours=999)
        sc = _scenario("a", enabled=False, last_built=old)
        self.assertFalse(_scenario_due(sc, time.time()))

    def test_uses_newest_of_last_built_and_last_loop(self):
        old = datetime.now(timezone.utc) - timedelta(hours=48)
        fresh = datetime.now(timezone.utc) - timedelta(hours=1)
        sc = _scenario("a", last_built=old, last_loop={"finished_at": _iso(fresh)})
        self.assertFalse(_scenario_due(sc, time.time()))

    def test_no_interval_never_due(self):
        sc = {"id": "a", "schedule": {"enabled": True}}
        self.assertFalse(_scenario_due(sc, time.time()))

    def test_last_activity_epoch_parses_iso(self):
        dt = datetime.now(timezone.utc) - timedelta(hours=2)
        self.assertIsNotNone(_last_activity_epoch(_scenario("a", last_built=dt)))
        self.assertIsNone(_last_activity_epoch({"id": "a"}))


class _FakeWB:
    def __init__(self, scenarios):
        self._scenarios = scenarios
        self.builds = []
        self.synths = []
        self.busy = None

    def list(self):
        return self._scenarios

    def status(self, sid):
        if sid == self.busy:
            return {"status": {"phase": "building"}}
        return {"status": {"phase": "idle"}}

    def build_corpus(self, sid, max_papers=None, model=None):
        self.builds.append(sid)
        return {"status": "ok", "corpus_size": 3}

    def run_synthesis(self, sid, include_experiments=False, model=None):
        self.synths.append(sid)
        return {"status": "ok", "best_score": 80.0}


class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    def test_due_scenarios_filters_list(self):
        now = datetime.now(timezone.utc)
        old = now - timedelta(hours=48)
        wb = _FakeWB([
            _scenario("stale", last_built=old),
            _scenario("fresh", last_built=now - timedelta(hours=1)),
            _scenario("off", enabled=False, last_built=old),
        ])
        sched = ScenarioScheduler(wb, check_minutes=5)
        self.assertEqual(sched.due_scenarios(), ["stale"])

    async def test_run_one_builds_and_synthesizes(self):
        old = datetime.now(timezone.utc) - timedelta(hours=48)
        wb = _FakeWB([_scenario("stale", last_built=old)])
        sched = ScenarioScheduler(wb, check_minutes=5)
        out = await sched.run_one("stale")
        self.assertEqual(out["status"], "ok")
        self.assertEqual(wb.builds, ["stale"])
        self.assertEqual(wb.synths, ["stale"])

    async def test_run_one_skips_busy_scenario(self):
        old = datetime.now(timezone.utc) - timedelta(hours=48)
        wb = _FakeWB([_scenario("stale", last_built=old)])
        wb.busy = "stale"
        sched = ScenarioScheduler(wb, check_minutes=5)
        out = await sched.run_one("stale")
        self.assertEqual(out["status"], "skipped")
        self.assertEqual(wb.builds, [])
        self.assertEqual(wb.synths, [])

    async def test_tick_respects_guard(self):
        now = datetime.now(timezone.utc)
        old = now - timedelta(hours=48)
        wb = _FakeWB([
            _scenario("stale1", last_built=old),
            _scenario("stale2", last_built=old),
        ])
        wb.busy = "stale2"
        sched = ScenarioScheduler(wb, check_minutes=5)
        out = await sched.tick()
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["status"], "ok")
        self.assertEqual(out[1]["status"], "skipped")
        self.assertEqual(wb.builds, ["stale1"])

    async def test_synthesize_can_be_disabled(self):
        old = datetime.now(timezone.utc) - timedelta(hours=48)
        wb = _FakeWB([_scenario("stale", last_built=old)])
        sched = ScenarioScheduler(wb, check_minutes=5, synthesize=False)
        await sched.run_one("stale")
        self.assertEqual(wb.builds, ["stale"])
        self.assertEqual(wb.synths, [])


if __name__ == "__main__":
    unittest.main()
