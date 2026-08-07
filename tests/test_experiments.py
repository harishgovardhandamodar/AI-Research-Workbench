"""Tests for experiment traceability: normalization, metrics/findings
extraction, similarity/overlap, comparison, and graph building over records."""

from __future__ import annotations

import unittest

import backend.experiments as experiments


def _agent_run(rid: int, metrics: dict, label: str = "") -> dict:
    return {"id": rid, "kind": "agent_run", "prompt": "do the thing",
            "started_at": 1000.0, "finished_at": 1100.0,
            "metrics": metrics, "artifact_ids": [], "label": label}


def _legacy_privacy(rid: str, verdict: str = "plausible",
                    reid: str = "low", linkage: float = 0.6) -> dict:
    return {
        "id": rid, "seed": 42, "fresh": False,
        "timestamp": "2026-01-01T00:00:00+00:00",
        "stage1": [{"linkage_success": linkage,
                    "attack_plausibility": 0.3,
                    "plausibility_verdict": verdict}],
        "stage2": {"unique_pct": 5.0, "reid_risk": reid},
        "stage3": [{"attacker_pred_rmse": 1.2}],
        "artifacts": ["audit_trail"],
    }


class TestUnifyRecord(unittest.TestCase):
    def test_agent_run_normalization(self):
        u = experiments.unify_record(_agent_run(3, {"acc": 0.8}, "baseline"))
        self.assertEqual(u["id"], 3)
        self.assertEqual(u["kind"], "agent_run")
        self.assertEqual(u["label"], "baseline")
        self.assertEqual(u["metrics"], {"acc": 0.8})
        self.assertEqual(u["artifacts"], [])
        # started_at is converted to an ISO timestamp
        self.assertTrue(u["timestamp"].startswith("2026") or "1970" in u["timestamp"])

    def test_legacy_privacy_workflow_flattening(self):
        u = experiments.unify_record(_legacy_privacy("a"))
        self.assertEqual(u["kind"], "privacy_workflow")
        self.assertEqual(u["seed"], 42)
        self.assertEqual(u["metrics"], {
            "linkage50": 0.6, "plausibility": 0.3,
            "unique_pct": 5.0, "rmse_eps0_1": 1.2})
        self.assertIn("plausibility: plausible", u["findings"])
        self.assertIn("re-id: low", u["findings"])
        # legacy records carry plain artifact ids
        self.assertEqual(u["artifacts"], ["audit_trail"])

    def test_artifact_ids_resolved_via_store(self):
        class FakeArtifacts:
            def get(self, aid):
                return type("A", (), {"name": "report.md", "data_type": "text"})()

        u = experiments.unify_record(
            _agent_run(1, {"x": 1.0}, "l"), artifact_store=FakeArtifacts())
        self.assertEqual(u["artifacts"], [])

        rec = {"id": 5, "kind": "agent_run", "artifact_ids": ["abc"], "metrics": {}}
        u = experiments.unify_record(rec, artifact_store=FakeArtifacts())
        self.assertEqual(u["artifacts"],
                         [{"id": "abc", "name": "report.md", "data_type": "text"}])


class TestMetricsFindings(unittest.TestCase):
    def test_metrics_from_plain_dict(self):
        m = experiments.metrics_from_run({"metrics": {"acc": 0.9, "loss": 0.1}})
        self.assertEqual(m, {"acc": 0.9, "loss": 0.1})

    def test_metrics_ignore_non_numeric(self):
        m = experiments.metrics_from_run({"metrics": {"acc": 0.9, "note": "x"}})
        self.assertEqual(m, {"acc": 0.9})

    def test_findings_from_config(self):
        f = experiments.findings_from_run(
            {"kind": "privacy_workflow", "config": {"findings": ["re-id: high"]}})
        self.assertEqual(f, ["re-id: high"])


class TestSimilarityOverlap(unittest.TestCase):
    def test_similarity_shared_metrics(self):
        a = _agent_run(1, {"acc": 0.5})
        b = _agent_run(2, {"acc": 0.6})
        # 0.1 diff vs 0.6 magnitude -> ~0.83
        self.assertAlmostEqual(experiments.similarity(a, b), 1 - 0.1 / 0.6, places=6)

    def test_similarity_identical(self):
        a = _agent_run(1, {"acc": 0.5})
        b = _agent_run(2, {"acc": 0.5})
        self.assertEqual(experiments.similarity(a, b), 1.0)

    def test_similarity_no_shared_metrics(self):
        a = _agent_run(1, {"acc": 0.5})
        b = _agent_run(2, {"loss": 0.5})
        self.assertEqual(experiments.similarity(a, b), 0.0)

    def test_overlap_shared_findings(self):
        a = _legacy_privacy("a", verdict="plausible", reid="low")
        b = _legacy_privacy("b", verdict="plausible", reid="low")
        self.assertAlmostEqual(experiments.overlap(a, b), 1.0)

    def test_overlap_disjoint_findings(self):
        a = _legacy_privacy("a", verdict="plausible", reid="low")
        b = _agent_run(2, {"acc": 0.5})
        self.assertEqual(experiments.overlap(a, b), 0.0)


class TestCompareRuns(unittest.TestCase):
    def test_compare_table(self):
        a = _agent_run(1, {"acc": 0.8, "loss": 0.2}, "baseline")
        b = _agent_run(2, {"acc": 0.85, "loss": 0.15}, "eps=1.0")
        c = experiments.compare_runs(a, b)
        self.assertEqual(c["a"], "baseline")
        self.assertEqual(c["b"], "eps=1.0")
        self.assertEqual(c["summary"]["shared"], 2)
        self.assertEqual(c["summary"]["increased"], 1)
        self.assertEqual(c["summary"]["decreased"], 1)
        rows = {r["metric"]: r for r in c["rows"]}
        self.assertAlmostEqual(rows["acc"]["delta"], 0.05)

    def test_compare_label_fallback(self):
        a = _agent_run(1, {"acc": 0.8})
        c = experiments.compare_runs(a, _agent_run(2, {"acc": 0.9}))
        self.assertEqual(c["a"], "run 1")


class TestBuildGraph(unittest.TestCase):
    def test_graph_from_records(self):
        records = [
            _agent_run(1, {"acc": 0.8, "loss": 0.2}),
            _agent_run(2, {"acc": 0.85, "loss": 0.15}),
            _legacy_privacy("a"),
        ]
        graph = experiments.build_graph(records)
        self.assertEqual(len(graph["nodes"]), 3)
        ids = {n["id"] for n in graph["nodes"]}
        self.assertEqual(ids, {1, 2, "a"})
        # both agent runs share metrics -> similarity edge
        self.assertTrue(any(e["source"] == 1 and e["target"] == 2
                            for e in graph["edges"]))
        node0 = graph["nodes"][0]
        self.assertEqual(node0["index"], 0)
        self.assertEqual(node0["metrics"]["acc"], 0.8)

    def test_graph_edges_include_overlap_only(self):
        records = [
            _legacy_privacy("a", verdict="plausible", reid="low"),
            _legacy_privacy("b", verdict="plausible", reid="high"),
        ]
        graph = experiments.build_graph(records)
        # shared "plausibility: plausible" finding -> overlap edge
        self.assertTrue(graph["edges"])

    def test_graph_empty(self):
        self.assertEqual(experiments.build_graph([]),
                         {"nodes": [], "edges": []})


if __name__ == "__main__":
    unittest.main()


class TestBranchGraph(unittest.TestCase):
    def test_branch_graph_chains_and_branches(self):
        from backend.experiments import build_branch_graph

        exps = [{"id": 1, "name": "eps sweep", "goal_metric": "acc",
                 "goal_target": 0.9, "higher_better": True, "status": "active"}]
        # e1: baseline run, then improve iteration explicitly branching off it
        runs = [
            {"id": 1, "experiment_id": 1, "kind": "agent_run", "label": "baseline",
             "config": {"eps": 0.1}, "metrics": {"acc": 0.80},
             "started_at": 100.0, "parent_run_id": None},
            {"id": 2, "experiment_id": 1, "kind": "agent_run", "label": "improve-1",
             "config": {"eps": 0.3}, "metrics": {"acc": 0.86},
             "started_at": 200.0, "parent_run_id": 1},
            {"id": 3, "experiment_id": 1, "kind": "agent_run", "label": "improve-2",
             "config": {"eps": 0.5}, "metrics": {"acc": 0.92},
             "started_at": 300.0, "parent_run_id": 2},
        ]
        g = build_branch_graph(runs, exps)
        self.assertEqual(len(g["nodes"]), 3)
        parents = {e["child"]: e["parent"] for e in g["edges"]}
        self.assertEqual(parents, {2: 1, 3: 2})
        self.assertEqual(g["tips"], [3])
        n1 = g["nodes"][0]
        self.assertEqual(n1["experiment_name"], "eps sweep")
        self.assertEqual(n1["config"]["eps"], 0.1)
        self.assertEqual(n1["goal_value"], 0.80)

    def test_branch_graph_infers_parents_chronologically(self):
        from backend.experiments import build_branch_graph

        exps = [{"id": 7, "name": "exp", "goal_metric": "", "goal_target": None,
                 "higher_better": True, "status": "active"}]
        runs = [
            {"id": 10, "experiment_id": 7, "kind": "agent_run", "label": None,
             "config": {}, "metrics": {}, "started_at": 10.0, "parent_run_id": None},
            {"id": 11, "experiment_id": 7, "kind": "agent_run", "label": None,
             "config": {}, "metrics": {}, "started_at": 20.0, "parent_run_id": None},
        ]
        g = build_branch_graph(runs, exps)
        parents = {e["child"]: e["parent"] for e in g["edges"]}
        self.assertEqual(parents, {11: 10})
        # the inferred parent is written back onto the node
        n11 = next(n for n in g["nodes"] if n["id"] == 11)
        self.assertEqual(n11["parent_run_id"], 10)

    def test_branch_graph_chains_same_kind_standalone(self):
        from backend.experiments import build_branch_graph

        # privacy fresh reruns have no experiment; they chain by kind
        runs = [
            {"id": 1, "experiment_id": None, "kind": "privacy_workflow",
             "label": "privacy workflow", "config": {"fresh": False}, "metrics": {},
             "started_at": 1.0, "parent_run_id": None},
            {"id": 2, "experiment_id": None, "kind": "privacy_workflow",
             "label": "privacy workflow (fresh)", "config": {"fresh": True},
             "metrics": {}, "started_at": 2.0, "parent_run_id": None},
            {"id": 3, "experiment_id": None, "kind": "notebook",
             "label": "nb", "config": {}, "metrics": {},
             "started_at": 3.0, "parent_run_id": None},
        ]
        g = build_branch_graph(runs, [])
        parents = {e["child"]: e["parent"] for e in g["edges"]}
        self.assertEqual(parents, {2: 1})
        self.assertNotIn(3, parents)  # different kind -> separate root
