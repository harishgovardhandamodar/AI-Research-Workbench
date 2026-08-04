"""Autoresearch loop tests: research-dir bootstrap, code extraction, metric
parsing, and running the template experiment under a budget."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from backend.autoresearch import (DEFAULT_EXPERIMENT, ensure_research_dir,
                                  extract_metric, parse_code_block,
                                  run_research_experiment)


class FakeRT:
    def __init__(self, base: Path):
        self.dir = base


class AutoresearchTests(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.rt = FakeRT(self.tmp)

    def test_ensure_research_dir_creates_files(self):
        files = ensure_research_dir(self.rt)
        for key in ("dir", "program", "experiment", "log"):
            self.assertTrue(files[key].exists(), key)
        self.assertIn("METRIC", DEFAULT_EXPERIMENT)

    def test_parse_code_block(self):
        self.assertEqual(parse_code_block("here:\n```python\nx = 1\n```\n"),
                         "x = 1")
        self.assertEqual(parse_code_block("```\nx = 2\n```"), "x = 2")
        self.assertIsNone(parse_code_block("no code here"))

    def test_extract_metric(self):
        out = "stuff\nMETRIC accuracy=0.9123\nmore\nMETRIC accuracy=0.9234\n"
        self.assertEqual(extract_metric(out, "accuracy"), ("accuracy", 0.9234))
        self.assertEqual(extract_metric(out), ("accuracy", 0.9234))
        self.assertIsNone(extract_metric("no metric", "accuracy"))

    def test_run_template_experiment(self):
        files = ensure_research_dir(self.rt)
        res = run_research_experiment(files["experiment"], budget=60)
        self.assertTrue(res["ok"], res)
        self.assertIsNotNone(res["metric"])
        name, val = res["metric"]
        self.assertEqual(name, "accuracy")
        self.assertTrue(0.5 < val <= 1.0, val)

    def test_run_times_out(self):
        slow = self.tmp / "slow.py"
        slow.write_text("import time; time.sleep(30)\n")
        res = run_research_experiment(slow, budget=1)
        self.assertFalse(res["ok"])
        self.assertTrue(res["timed_out"])


if __name__ == "__main__":
    unittest.main()
