"""B3 ranking tests: the pure rank_runs leaderboard helper."""

from __future__ import annotations

import unittest

from backend.experiments import rank_runs


def run(rid, metrics, label=""):
    return {"id": rid, "label": label or f"run {rid}",
            "metrics": metrics, "config": {}, "prompt": ""}


class RankRunsTests(unittest.TestCase):

    def test_ranks_desc_higher_better(self):
        out = rank_runs([run(1, {"acc": 0.5}), run(2, {"acc": 0.9}),
                         run(3, {"acc": 0.7})], "acc")
        self.assertEqual([r["run_id"] for r in out["rows"]], [2, 3, 1])
        self.assertEqual(out["best"], 0.9)
        self.assertEqual(out["rows"][0]["rank"], 1)

    def test_deltas_vs_best(self):
        out = rank_runs([run(1, {"acc": 0.5}), run(2, {"acc": 0.9})], "acc")
        top, bot = out["rows"]
        self.assertEqual(top["delta_best"], 0.0)
        self.assertEqual(bot["delta_best"], -0.4)
        self.assertAlmostEqual(bot["pct_best"], -44.444, places=2)

    def test_lower_better(self):
        out = rank_runs([run(1, {"loss": 0.9}), run(2, {"loss": 0.2})],
                        "loss", higher_better=False)
        self.assertEqual([r["run_id"] for r in out["rows"]], [2, 1])
        self.assertEqual(out["best"], 0.2)

    def test_skips_runs_without_metric(self):
        out = rank_runs([run(1, {"acc": 0.5}), run(2, {}),
                         run(3, {"acc": "bad"})], "acc")
        self.assertEqual(len(out["rows"]), 1)
        self.assertEqual(out["rows"][0]["run_id"], 1)

    def test_empty(self):
        out = rank_runs([], "acc")
        self.assertEqual(out["rows"], [])
        self.assertIsNone(out["best"])


if __name__ == "__main__":
    unittest.main()
