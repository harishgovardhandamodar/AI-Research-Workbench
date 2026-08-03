"""Tests for the experiment history: record, load, and graph building."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import backend.experiments as experiments


class TestExperiments(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.old_runs_file = experiments.RUNS_FILE
        experiments.RUNS_FILE = self.tmp / "privacy_runs.json"

    def tearDown(self):
        experiments.RUNS_FILE = self.old_runs_file

    def test_record_and_load(self):
        experiments.record_experiment({"id": "a", "kind": "notebook",
                                       "label": "n1", "metrics": {"acc": 0.8}})
        experiments.record_experiment({"id": "b", "kind": "notebook",
                                       "label": "n2", "metrics": {"acc": 0.9}})
        runs = experiments.load_experiments()
        self.assertEqual(len(runs), 2)
        self.assertEqual(runs[-1]["label"], "n2")

    def test_build_graph(self):
        experiments.record_experiment({"id": "a", "kind": "notebook",
                                       "label": "n1",
                                       "metrics": {"acc": 0.8, "loss": 0.2}})
        experiments.record_experiment({"id": "b", "kind": "notebook",
                                       "label": "n2",
                                       "metrics": {"acc": 0.85, "loss": 0.15}})
        graph = experiments.build_graph()
        self.assertEqual(len(graph["nodes"]), 2)
        node_ids = {n["id"] for n in graph["nodes"]}
        self.assertEqual(node_ids, {"a", "b"})
        self.assertTrue(any(n["id"] == "a" and n["metrics"]["acc"] == 0.8
                            for n in graph["nodes"]))
        # shared numeric metrics make the two runs similar enough for an edge
        self.assertTrue(graph["edges"], "expected at least one similarity edge")
        self.assertEqual(graph["edges"][0]["source"], "a")
        self.assertEqual(graph["edges"][0]["target"], "b")

    def test_missing_file_loads_empty(self):
        self.assertEqual(experiments.load_experiments(), [])


if __name__ == "__main__":
    unittest.main()
