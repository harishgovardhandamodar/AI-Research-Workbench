"""Deterministic experiment planner tests: store lifecycle + locking, derived
seeds, execution (determinism / timeout / persistence / failure), restart
recovery, and the incremental suggestion engine."""

from __future__ import annotations

import asyncio
import tempfile
import threading
import time
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

import backend.experiment_registry  # noqa: F401  (registers the catalog + peer)
from backend import experiment_planner as ep


def _make_csv(path: Path, n: int = 400, seed: int = 0, with_pii: bool = True):
    rng = np.random.default_rng(seed)
    df = pd.DataFrame({
        "sender_bank": rng.choice(["HDFC", "SBI", "ICICI"], n),
        "amount (INR)": rng.normal(1000, 300, n).round(2),
        "merchant_category": rng.choice(["retail", "dining"], n),
        "transaction type": rng.choice(["UPI", "IMPS"], n),
    })
    if with_pii:
        df["email"] = [f"u{i}@x.com" for i in range(n)]
        df["phone"] = [f"+91{i:010d}" for i in range(n)]
    df.to_csv(path, index=False)
    return df


def _plan(status="APPROVED", **kw):
    base = {"id": "p1", "experiment_id": "eda", "dataset": "d.csv",
            "seed": 1, "status": status, "steps": [], "updated_at": 0}
    base.update(kw)
    return base


class TestPlanStore(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        _make_csv(self.dir / "d.csv")
        self.store = ep.PlanStore(self.dir)

    def test_lifecycle(self):
        plan = self.store.create("eda", "onboarding", "d.csv")
        self.assertEqual(plan["status"], "DRAFT")
        self.assertEqual(plan["seed_source"], "derived")
        p = self.store.propose(plan["id"])
        self.assertEqual(p["status"], "WAITING_APPROVAL")
        p = self.store.decide(plan["id"], True, by="tester")
        self.assertEqual(p["status"], "APPROVED")
        self.assertTrue(p["approval"]["approved"])
        self.assertEqual(self.store.get(plan["id"])["status"], "APPROVED")

    def test_derived_seed_is_deterministic(self):
        a = self.store.create("eda", "same request", "d.csv")
        b = self.store.create("eda", "same request", "d.csv")
        self.assertEqual(a["seed"], b["seed"])
        c = self.store.create("eda", "other request", "d.csv")
        self.assertNotEqual(a["seed"], c["seed"])
        # explicit seed always wins.
        d = self.store.create("eda", "same request", "d.csv", seed=7)
        self.assertEqual(d["seed"], 7)
        self.assertEqual(d["seed_source"], "explicit")

    def test_validate_experiment(self):
        with self.assertRaises(ValueError):
            self.store.create("nope", "", "d.csv")

    def test_needs_dataset(self):
        with self.assertRaises(ValueError):
            self.store.create("eda", "", "")

    def test_dataset_missing_file(self):
        with self.assertRaises(ValueError):
            self.store.create("eda", "", "missing.csv")

    def test_requires_columns(self):
        self.dir.joinpath("nobank.csv").write_text("a,b\n1,2\n")
        with self.assertRaises(ValueError) as ctx:
            self.store.create("peer", "", "nobank.csv")
        self.assertIn("sender_bank", str(ctx.exception))

    def test_repropose_and_clone(self):
        plan = self.store.create("eda", "onboarding", "d.csv")
        self.store.propose(plan["id"])
        self.store.decide(plan["id"], False, by="u")
        self.assertEqual(self.store.get(plan["id"])["status"], "REJECTED")
        re = self.store.repropose(plan["id"], seed=99)
        self.assertEqual(re["id"], plan["id"])
        self.assertEqual(re["seed"], 99)
        self.assertEqual(re["status"], "WAITING_APPROVAL")
        clone = self.store.clone(plan["id"], seed=5)
        self.assertNotEqual(clone["id"], plan["id"])
        self.assertEqual(clone["seed"], 5)

    def test_repropose_rederives_derived_seed(self):
        plan = self.store.create("eda", "req-A", "d.csv")
        self.assertEqual(plan["seed_source"], "derived")
        orig_seed = plan["seed"]
        self.store.propose(plan["id"])
        self.store.decide(plan["id"], False)
        re = self.store.repropose(plan["id"], request="req-B")
        self.assertNotEqual(re["seed"], orig_seed)
        self.assertEqual(re["seed_source"], "derived")
        # unchanged repropose keeps the (derived) seed -> reproducible retry.
        self.store.decide(re["id"], False)
        re2 = self.store.repropose(re["id"])
        self.assertEqual(re2["seed"], re["seed"])
        # explicit seeds are never re-derived.
        self.store.decide(re2["id"], False)
        re3 = self.store.repropose(re2["id"], seed=42)
        self.assertEqual(re3["seed"], 42)
        self.assertEqual(re3["seed_source"], "explicit")

    def test_dataset_hash_pins_content(self):
        plan = self.store.create("eda", "x", "d.csv")
        self.assertEqual(len(plan["dataset_hash"]), 64)
        same = self.store.create("eda", "x", "d.csv")
        self.assertEqual(same["dataset_hash"], plan["dataset_hash"])
        pd.DataFrame({"a": [1, 2]}).to_csv(self.dir / "d.csv", index=False)
        edited = self.store.create("eda", "x", "d.csv")
        self.assertNotEqual(edited["dataset_hash"], plan["dataset_hash"])

    def test_delete(self):
        plan = self.store.create("eda", "onboarding", "d.csv")
        self.assertTrue(self.store.delete(plan["id"]))
        self.assertFalse(self.store.delete(plan["id"]))
        self.assertIsNone(self.store.get(plan["id"]))

    def test_concurrent_updates_are_not_lost(self):
        """Parallel create/update cycles must not clobber each other."""
        def _creator(i):
            p = self.store.create("eda", f"r{i}", "d.csv")
            self.store.update(p["id"], request=f"updated{i}")
        threads = [threading.Thread(target=_creator, args=(i,)) for i in range(8)]
        for t in threads: t.start()
        for t in threads: t.join()
        plans = self.store.list()
        self.assertEqual(len(plans), 8)
        for p in plans:
            self.assertTrue(p["request"].startswith("updated"))

    def test_two_store_instances_share_lock(self):
        """Separate PlanStore objects (as in the backend + MCP processes) must
        not lose each other's updates thanks to the OS-level file lock."""
        other = ep.PlanStore(self.dir)
        def _creator(i, st):
            for k in range(4):
                p = st.create("eda", f"r{i}-{k}", "d.csv")
                st.update(p["id"], request=f"u{i}-{k}")
        threads = [threading.Thread(target=_creator,
                                    args=(i, self.store if i % 2 == 0 else other))
                   for i in range(6)]
        for t in threads: t.start()
        for t in threads: t.join()
        plans = ep.PlanStore(self.dir).list()
        self.assertEqual(len(plans), 24)
        self.assertTrue(all(p["request"].startswith("u") for p in plans))

    def test_dismissal_persists(self):
        self.assertFalse(self.store.dismissed_suggestions())
        self.assertTrue(self.store.dismiss_suggestion("abc12345"))
        self.assertFalse(self.store.dismiss_suggestion("abc12345"))
        self.assertEqual(self.store.dismissed_suggestions(), {"abc12345"})
        other = ep.PlanStore(self.dir)
        self.assertEqual(other.dismissed_suggestions(), {"abc12345"})

    def test_clone_lineage(self):
        plan = self.store.create("eda", "x", "d.csv")
        c = self.store.clone(plan["id"], seed=99)
        self.assertEqual(c["parent_id"], plan["id"])
        self.assertEqual(c["lineage"], [plan["id"]])
        cc = self.store.clone(c["id"])
        self.assertEqual(cc["lineage"], [plan["id"], c["id"]])
        self.assertEqual(cc["parent_id"], c["id"])

    def test_recover_interrupted(self):
        plan = self.store.create("eda", "onboarding", "d.csv")
        self.store.update(plan["id"], status="RUNNING", started_at=1.0)
        n = self.store.recover_interrupted(grace=0)
        self.assertEqual(n, 1)
        p = self.store.get(plan["id"])
        self.assertEqual(p["status"], "FAILED")
        self.assertIn("restart", p["error"])
        # a young RUNNING plan is left alone.
        self.store.update(plan["id"], status="RUNNING", started_at=time.time())
        self.assertEqual(self.store.recover_interrupted(grace=30), 0)


class TestExecute(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.df = _make_csv(self.dir / "d.csv", n=300)

    def test_deterministic_metrics(self):
        a = ep.execute_plan(
            _plan(experiment_id="dp_privacy", seed=42, id="a"),
            self.df, self.dir)
        b = ep.execute_plan(
            _plan(experiment_id="dp_privacy", seed=42, id="b"),
            self.df, self.dir)
        self.assertEqual(a["status"], "DONE")
        self.assertEqual(a["metrics"]["min_mae"], b["metrics"]["min_mae"])

    def test_persists_report_and_figures(self):
        plan = _plan(experiment_id="eda", seed=1, id="p")
        done = ep.execute_plan(plan, self.df, self.dir)
        self.assertEqual(done["status"], "DONE")
        self.assertTrue((self.dir / "plans/p/report.md").exists())
        self.assertIn("report.md", done["result"]["persisted"])

    def test_requires_approved(self):
        with self.assertRaises(ValueError):
            ep.execute_plan(_plan(status="DRAFT", experiment_id="eda"),
                            self.df, self.dir)

    def test_failure_marks_failed(self):
        def boom(df, seed=0):
            raise RuntimeError("kernel blew up")
        ep.EXPERIMENT_REGISTRY["boom"] = {
            "name": "boom", "needs_dataset": False, "run": boom,
            "render_report": lambda r: "", "render_figures": lambda r: {}}
        try:
            done = ep.execute_plan(_plan(experiment_id="boom", id="b"),
                                   self.df, self.dir)
            self.assertEqual(done["status"], "FAILED")
            self.assertIn("kernel blew up", done["error"])
            self.assertIsNone(done.get("result"))
        finally:
            del ep.EXPERIMENT_REGISTRY["boom"]

    def test_timeout_marks_failed(self):
        def slow(df, seed=0):
            time.sleep(3)
            return {"metrics": {"x": 1}}
        ep.EXPERIMENT_REGISTRY["slow"] = {
            "name": "slow", "needs_dataset": False, "run": slow,
            "render_report": lambda r: "", "render_figures": lambda r: {}}
        try:
            t0 = time.time()
            done = ep.execute_plan(_plan(experiment_id="slow", id="s"),
                                   self.df, self.dir, timeout=0.2)
            self.assertEqual(done["status"], "FAILED")
            self.assertIn("timed out", done["error"])
            self.assertLess(time.time() - t0, 2.0)
        finally:
            del ep.EXPERIMENT_REGISTRY["slow"]

    def test_running_status_accepted(self):
        done = ep.execute_plan(
            _plan(status="RUNNING", experiment_id="eda", id="r"),
            self.df, self.dir)
        self.assertEqual(done["status"], "DONE")

    def test_execute_pins_dataset_hash(self):
        done = ep.execute_plan(
            _plan(experiment_id="eda", seed=1, id="h"),
            self.df, self.dir)
        self.assertEqual(len(done["dataset_hash"]), 64)
        self.assertEqual(done["dataset_hash"],
                         ep.dataset_hash(self.dir / "d.csv"))


class TestDatasetIO(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.df = pd.DataFrame({"sender_bank": ["a", "b", "a"],
                                "amount (INR)": [1.0, 2.0, 3.0]})

    def test_is_dataset_file(self):
        for name in ("a.csv", "b.parquet", "c.xlsx", "d.xls"):
            self.assertTrue(ep.is_dataset_file(name))
        for name in ("a.txt", "b", "c.json"):
            self.assertFalse(ep.is_dataset_file(name))

    def test_parquet_roundtrip(self):
        try:
            self.df.to_parquet(self.dir / "d.parquet")
        except Exception:  # noqa: BLE001  (pyarrow not installed)
            self.skipTest("pyarrow not installed")
        loaded = ep.load_dataset(self.dir / "d.parquet")
        self.assertEqual(list(loaded.columns), list(self.df.columns))
        self.assertEqual(len(ep.peek_dataset(self.dir / "d.parquet", n=2)), 2)

    def test_csv_roundtrip(self):
        self.df.to_csv(self.dir / "d.csv", index=False)
        loaded = ep.load_dataset(self.dir / "d.csv")
        self.assertEqual(len(loaded), 3)

    def test_create_validates_parquet_columns(self):
        try:
            self.df.to_parquet(self.dir / "d.parquet")
        except Exception:  # noqa: BLE001
            self.skipTest("pyarrow not installed")
        st = ep.PlanStore(self.dir)
        plan = st.create("peer", "", "d.parquet")
        self.assertEqual(plan["dataset"], "d.parquet")
        # a parquet without the required column is rejected.
        pd.DataFrame({"a": [1]}).to_parquet(self.dir / "bad.parquet")
        with self.assertRaises(ValueError) as ctx:
            st.create("peer", "", "bad.parquet")
        self.assertIn("sender_bank", str(ctx.exception))


class TestMcpTools(unittest.TestCase):
    """Drive the experiment-planner MCP tool functions directly against a temp
    plan store (same file layout the backend + MCP share)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        _make_csv(self.tmp / "d.csv", n=120)
        self._prev = None
        if "FOX_PLAN_STORE" in __import__("os").environ:
            self._prev = __import__("os").environ["FOX_PLAN_STORE"]
        __import__("os").environ["FOX_PLAN_STORE"] = str(self.tmp)

    def tearDown(self):
        import os
        if self._prev is None:
            os.environ.pop("FOX_PLAN_STORE", None)
        else:
            os.environ["FOX_PLAN_STORE"] = self._prev

    def test_run_experiment_persists_result(self):
        from mcp_servers.experiment_planner import server as mcps
        st = ep.PlanStore(self.tmp)
        plan = st.create("eda", "profile", "d.csv", seed=1)
        st.propose(plan["id"])
        st.decide(plan["id"], True, by="mcp-test")

        out = mcps.run_experiment(plan["id"], project="")
        self.assertIn('"status": "DONE"', out)
        saved = st.get(plan["id"])
        self.assertEqual(saved["status"], "DONE")
        self.assertIsNotNone(saved["metrics"])
        self.assertTrue((self.tmp / "plans" / plan["id"] / "report.md").exists())

    def test_run_experiment_requires_approved(self):
        from mcp_servers.experiment_planner import server as mcps
        st = ep.PlanStore(self.tmp)
        plan = st.create("eda", "profile", "d.csv")
        out = mcps.run_experiment(plan["id"], project="")
        self.assertIn('"ok": false', out)
        self.assertIn("approve", out)

    def test_store_dir_defaults_to_active_project(self):
        """MCP calls without an explicit project must resolve to the project
        whose plan store was touched most recently (shared with the REST host)."""
        from mcp_servers.experiment_planner import server as mcps
        import os
        base = Path(tempfile.mkdtemp())
        (base / "projA").mkdir()
        (base / "projB").mkdir()
        # default: no plan store yet -> falls back to base
        os.environ["FOX_PLAN_STORE"] = str(base)
        try:
            self.assertEqual(mcps._store_dir("").resolve(), base.resolve())
            # explicit project wins
            self.assertEqual(mcps._store_dir("projB").name, "projB")
            # touch projA's store most recently -> active project resolves to it
            (base / "projA" / "experiment_plans.json").write_text('{"plans": {}}')
            time.sleep(0.05)
            (base / "projB" / "experiment_plans.json").write_text('{"plans": {}}')
            self.assertEqual(mcps._store_dir("").name, "projB")
        finally:
            os.environ.pop("FOX_PLAN_STORE", None)


def _sug_plan(ds, eid, status="DONE", metrics=None, t=0, error=None, pid=None):
    return {"dataset": ds, "experiment_id": eid, "status": status, "id": pid or eid,
            "updated_at": t, "metrics": metrics, "error": error}


class TestSuggestions(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(ep.build_suggestions([]), [])
        self.assertEqual(ep.build_suggestions(None), [])

    def test_cold_start_onboarding(self):
        """A dataset present in the project but never planned -> EDA first."""
        sug = ep.build_suggestions([], datasets=["brand-new.csv"])
        self.assertEqual(len(sug), 1)
        self.assertEqual(sug[0]["id"], "eda")
        self.assertIn("brand-new.csv", sug[0]["reason"])

    def test_cold_start_does_not_double_count_planned(self):
        plans = [_sug_plan("d.csv", "pii_scan", metrics={"pii_columns": 1}, t=1)]
        sug = ep.build_suggestions(plans, datasets=["d.csv", "other.csv"])
        ids = {s["id"] for s in sug}
        self.assertIn("eda", ids)  # other.csv onboarding
        self.assertIn("reid_risk", ids)  # d.csv finding-driven

    def test_multi_seed_instability(self):
        plans = [_sug_plan("d.csv", "dp_privacy", metrics={"min_mae": 0.4}, t=1, pid="a"),
                 _sug_plan("d.csv", "dp_privacy", metrics={"min_mae": 2.1}, t=2, pid="b")]
        dp = [s for s in ep.build_suggestions(plans) if s["id"] == "dp_privacy"]
        self.assertEqual(len(dp), 1)
        self.assertIn("unstable", dp[0]["reason"])
        self.assertEqual(dp[0]["score"], 3)

    def test_stable_across_seeds_not_flagged(self):
        plans = [_sug_plan("d.csv", "dp_privacy", metrics={"min_mae": 0.4}, t=1, pid="a"),
                 _sug_plan("d.csv", "dp_privacy", metrics={"min_mae": 0.45}, t=2, pid="b")]
        dp = [s for s in ep.build_suggestions(plans) if s["id"] == "dp_privacy"]
        self.assertEqual(dp, [])

    def test_regression_after_clean_noted(self):
        plans = [_sug_plan("d.csv", "dp_privacy", metrics={"min_mae": 0.4}, t=1, pid="a"),
                 _sug_plan("d.csv", "clean", metrics={"affected_rows": 9}, t=2, pid="c"),
                 _sug_plan("d.csv", "dp_privacy", metrics={"min_mae": 2.1}, t=3, pid="d")]
        dp = [s for s in ep.build_suggestions(plans) if s["id"] == "dp_privacy"]
        self.assertEqual(len(dp), 1)
        self.assertIn("did NOT improve", dp[0]["reason"])

    def test_delta_ignored_when_dataset_edited(self):
        """Metric change between runs is confounded by a data edit -> note it."""
        base = {"dataset": "d.csv", "experiment_id": "dp_privacy",
                "status": "DONE", "metrics": {"min_mae": 2.1},
                "updated_at": 1, "id": "a", "dataset_hash": "H1"}
        plans = [
            {**base, "metrics": {"min_mae": 2.1}, "updated_at": 1, "id": "a", "dataset_hash": "H1"},
            {**_sug_plan("d.csv", "clean", metrics={"affected_rows": 9}, t=2, pid="c"), "dataset_hash": "H1"},
            {**base, "metrics": {"min_mae": 0.4}, "updated_at": 3, "id": "e", "dataset_hash": "H2"},
        ]
        dp = [s for s in ep.build_suggestions(plans) if s["id"] == "dp_privacy"]
        self.assertEqual(len(dp), 1)
        self.assertIn("dataset was edited", dp[0]["reason"])

    def test_direction_aware_delta_higher_better(self):
        """peer's goal metric is higher-better: 0.5 -> 0.7 counts as improved."""
        plans = [
            _sug_plan("d.csv", "peer", metrics={"identification_accuracy": 0.5}, t=1, pid="a"),
            _sug_plan("d.csv", "clean", metrics={"affected_rows": 9}, t=2, pid="c"),
            _sug_plan("d.csv", "peer", metrics={"identification_accuracy": 0.7}, t=3, pid="e"),
        ]
        peer = [s for s in ep.build_suggestions(plans) if s["id"] == "peer"]
        self.assertEqual(len(peer), 1)
        self.assertIn("improved", peer[0]["reason"])

    def test_cross_dataset_comparison(self):
        plans = [_sug_plan("d1.csv", "dp_privacy", metrics={"min_mae": 2.1}, t=1, pid="a"),
                 _sug_plan("d2.csv", "dp_privacy", metrics={"min_mae": 0.3}, t=1, pid="b")]
        x = [s for s in ep.build_suggestions(plans) if "markedly worse" in s["reason"]]
        self.assertEqual(len(x), 1)
        self.assertIn("d1.csv", x[0]["reason"])

    def test_higher_better_metadata(self):
        meta = {e["id"]: e for e in ep.list_experiments()}
        self.assertTrue(meta["peer"]["higher_better"])
        self.assertFalse(meta["dp_privacy"]["higher_better"])
        self.assertFalse(meta["pii_scan"]["higher_better"])
        self.assertEqual(meta["peer"]["goal_metric"], "identification_accuracy")

    def test_suggestion_ids_stable_and_dismissable(self):
        plans = [_sug_plan("d.csv", "pii_scan", metrics={"pii_columns": 2}, t=1)]
        a = ep.build_suggestions(plans)
        b = ep.build_suggestions(plans)
        self.assertTrue(all(x["suggestion_id"] == y["suggestion_id"]
                            for x, y in zip(a, b)))
        reid = next(s for s in a if s["id"] == "reid_risk")
        sid = reid["suggestion_id"]
        filtered = ep.build_suggestions(plans, dismissed={sid})
        self.assertNotIn("reid_risk", {s["id"] for s in filtered})
        self.assertIn("dp_privacy", {s["id"] for s in filtered})

    def test_failure_suggestion_carries_plan_evidence(self):
        plans = [_sug_plan("d.csv", "dp_privacy", status="FAILED", error="x", t=1, pid="f1")]
        sug = {s["id"]: s for s in ep.build_suggestions(plans)}
        dp = sug["dp_privacy"]
        self.assertEqual(dp["action"], "repropose")
        self.assertIn("f1", dp["evidence"])

    def test_onboarding_new_dataset(self):
        plans = [_sug_plan("d.csv", "pii_scan", status="FAILED", error="x", t=1)]
        sug = ep.build_suggestions(plans)
        ids = [s["id"] for s in sug]
        self.assertEqual(ids[0], "eda")  # EDA onboarding scores highest
        # failed experiment is covered but flagged, not promoted as coverage.
        pii = next(s for s in sug if s["id"] == "pii_scan")
        self.assertEqual(pii["score"], 1)
        self.assertEqual(pii["action"], "repropose")

    def test_pii_found_recommends_reid_and_dp(self):
        plans = [_sug_plan("d.csv", "pii_scan",
                       metrics={"pii_columns": 4}, t=1)]
        sug = {s["id"]: s for s in ep.build_suggestions(plans)}
        self.assertEqual(sug["reid_risk"]["score"], 5)
        self.assertEqual(sug["dp_privacy"]["score"], 5)

    def test_reid_risk_high_recommends_dp(self):
        plans = [_sug_plan("d.csv", "reid_risk",
                       metrics={"k_anonymity_1": 0.4}, t=1)]
        sug = {s["id"]: s for s in ep.build_suggestions(plans)}
        self.assertEqual(sug["dp_privacy"]["score"], 4)

    def test_anomalies_recommend_clean(self):
        plans = [_sug_plan("d.csv", "anomaly",
                       metrics={"outlier_cols": 3}, t=1)]
        sug = {s["id"]: s for s in ep.build_suggestions(plans)}
        self.assertEqual(sug["clean"]["score"], 4)

    def test_single_dp_run_recommends_seed_verify(self):
        plans = [_sug_plan("d.csv", "dp_privacy",
                       metrics={"min_mae": 2.0}, t=1)]
        sug = {s["id"]: s for s in ep.build_suggestions(plans)}
        v = sug["dp_privacy"]
        self.assertEqual(v["action"], "clone")
        self.assertIsNotNone(v["suggested_seed"])
        self.assertEqual(v["score"], 3)

    def test_clean_done_recommends_rerun_affected(self):
        plans = [_sug_plan("d.csv", "dp_privacy",
                       metrics={"min_mae": 2.0}, t=1),
                 _sug_plan("d.csv", "clean", metrics={"affected_rows": 9}, t=2)]
        sug = {s["id"]: s for s in ep.build_suggestions(plans)}
        self.assertEqual(sug["dp_privacy"]["action"], "clone")
        self.assertIn("cleaned", sug["dp_privacy"]["reason"])

    def test_improvement_after_clean_noted(self):
        plans = [_sug_plan("d.csv", "dp_privacy", metrics={"min_mae": 2.0}, t=1),
                 _sug_plan("d.csv", "clean", metrics={"affected_rows": 9}, t=2),
                 _sug_plan("d.csv", "dp_privacy", metrics={"min_mae": 0.5}, t=3)]
        sug = {s["id"]: s for s in ep.build_suggestions(plans)}
        self.assertIn("improved", sug["dp_privacy"]["reason"])
        self.assertEqual(sug["dp_privacy"]["score"], 2)

    def test_failed_latest_not_resuggested_for_coverage(self):
        # dp succeeded once, then its latest attempt failed -> it has DONE runs,
        # so it is not re-suggested; the failure is surfaced once at low score.
        plans = [_sug_plan("d.csv", "dp_privacy", metrics={"min_mae": 2.0}, t=1),
                 _sug_plan("d.csv", "dp_privacy", status="FAILED", error="x", t=2)]
        sug = ep.build_suggestions(plans)
        dp = [s for s in sug if s["id"] == "dp_privacy"]
        self.assertEqual(len(dp), 1)
        self.assertEqual(dp[0]["action"], "repropose")
        self.assertEqual(dp[0]["score"], 1)


class TestPlannerRoutes(unittest.IsolatedAsyncioTestCase):
    """End-to-end through the REST route handlers (propose -> approve -> run ->
    DONE -> suggestions) using a real ProjectRuntime in a temp dir."""

    async def asyncSetUp(self):
        import backend.project_runtime as pr
        from backend.state import runtimes

        self.tmp = Path(tempfile.mkdtemp())
        self._orig_dir = pr.PROJECTS_DIR
        pr.PROJECTS_DIR = self.tmp
        self.rt = pr.ProjectRuntime("pltest")
        runtimes["pltest"] = self.rt
        _make_csv(self.rt.dir / "d.csv", n=250)

    async def asyncTearDown(self):
        import backend.project_runtime as pr
        from backend.state import runtimes
        pr.PROJECTS_DIR = self._orig_dir
        runtimes.pop("pltest", None)
        await self.rt.stop()

    def _handlers(self):
        from backend.routers import experiment_planner as er
        return er

    async def test_full_flow(self):
        er = self._handlers()
        res = await er.create_plan("pltest",
                                   {"experiment_id": "eda",
                                    "dataset": "d.csv", "request": "profile"})
        self.assertEqual(res["plan"]["status"], "WAITING_APPROVAL")
        plan_id = res["plan"]["plan_id"]
        self.assertIn("dataset_info", res["plan"])
        self.assertEqual(res["plan"]["dataset_info"]["shape"][1], 6)

        d = await er.decide_plan("pltest", plan_id, {"approve": True})
        self.assertTrue(d["approved"])
        r = await er.run_plan("pltest", plan_id)
        self.assertTrue(r["running"])

        deadline = time.time() + 20
        plan = None
        while time.time() < deadline:
            plan = await er.get_plan("pltest", plan_id)
            if plan["plan"]["status"] in ("DONE", "FAILED"):
                break
            await asyncio.sleep(0.2)
        self.assertEqual(plan["plan"]["status"], "DONE", plan)
        self.assertIsNotNone(plan["plan"]["metrics"])
        # The Experiments-tab experiment inherits the registry direction
        # (eda goal=duplicates is lower-better, not higher-better).
        exps = self.rt.store.list_experiments()
        exp = next(e for e in exps if e["name"].startswith("🧪 EDA — dataset overview"))
        self.assertEqual(exp.get("goal_metric"), "duplicates")
        self.assertIs(exp.get("higher_better"), False)

        sug = await er.plan_suggestions("pltest")
        ids = [s["id"] for s in sug["suggestions"]]
        self.assertIn("pii_scan", ids)  # coverage for the still-missing scenarios

    async def test_run_rejects_unapproved(self):
        er = self._handlers()
        res = await er.create_plan("pltest",
                                   {"experiment_id": "eda", "dataset": "d.csv"})
        plan_id = res["plan"]["plan_id"]
        r = await er.run_plan("pltest", plan_id)
        if isinstance(r, dict):
            self.assertFalse(r.get("ok"))
        else:
            self.assertEqual(r.status_code, 400)

    async def test_dismiss_suggestion_route(self):
        er = self._handlers()
        res = await er.create_plan("pltest",
                                   {"experiment_id": "eda", "dataset": "d.csv"})
        sug = await er.plan_suggestions("pltest")
        sid = sug["suggestions"][0]["suggestion_id"]
        d = await er.dismiss_suggestion("pltest", sid)
        self.assertTrue(d["ok"])
        sug2 = await er.plan_suggestions("pltest")
        self.assertNotIn(sid, {s["suggestion_id"] for s in sug2["suggestions"]})


if __name__ == "__main__":
    unittest.main()
