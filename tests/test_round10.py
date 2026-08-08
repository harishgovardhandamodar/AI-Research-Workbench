"""Round-10 tests: comprehensive project report + portable export bundle."""

from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from backend.store import ProjectStore


class _Rt:
    """Minimal runtime with store + artifacts + audit for report/export."""

    def __init__(self, name="proj"):
        from backend.artifacts.store import ArtifactStore
        self.name = name
        self.dir = Path(tempfile.mkdtemp())
        self.store = ProjectStore(self.dir)
        self.artifacts = ArtifactStore(self.dir)
        self.llm = None

        class _AuditStub:
            def summary(self):
                return {"total_events": 5, "events": 5}

            def count_open_deviations(self):
                return 1

            def verify_chain(self):
                return {"verified": True}

        self.audit_store = _AuditStub()


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.rt = _Rt()
        self.eid = self.rt.store.create_experiment("A", "hyp", "acc", 0.9, True)
        self.rid = self.rt.store.add_run("p", "r", "done", 1.0, 2.0,
                                         metrics={"acc": 0.8},
                                         experiment_id=self.eid, message_id=7)

    def test_report_contains_sections(self):
        from backend.report import build_project_report
        report = build_project_report(self.rt, include_summary=False)
        for section in ("# Research report", "## Experiments", "## Learnings",
                        "## Campaigns", "## Model benchmarks", "## Recent runs",
                        "## Audit"):
            self.assertIn(section, report)
        self.assertIn("A", report)
        self.assertIn("0.8", report)

    def test_report_integrity_flag(self):
        from backend.report import build_project_report
        report = build_project_report(self.rt, include_summary=False)
        self.assertIn("✓", report)  # the run's integrity chip


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.rt = _Rt()
        self.eid = self.rt.store.create_experiment("A", "h", "acc", 0.9, True)
        self.rid = self.rt.store.add_run("p", "r", "done", 1.0, 2.0,
                                         metrics={"acc": 0.8},
                                         experiment_id=self.eid)
        self.rt.store.add_learning(self.eid, self.rid, "acc", 0.5, 0.8, 0.3, 1,
                                   "Tried X: acc improved.", "suggestion")

    def test_export_zip_contains_payloads(self):
        from backend.export import export_project
        path = export_project(self.rt)
        self.assertTrue(path.exists())
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            self.assertIn("report.md", names)
            self.assertIn("experiments.json", names)
            self.assertIn("learnings.json", names)
            self.assertIn("campaigns.json", names)
            self.assertIn("evals.json", names)
            self.assertIn("suggestions.json", names)
            self.assertIn("audit-summary.json", names)
            self.assertIn("provenance.json", names)
            self.assertIn(f"runs/{self.rid}.json", names)
            exps = json.loads(z.read("experiments.json"))
            self.assertEqual(exps[0]["id"], self.eid)
            learnings = json.loads(z.read("learnings.json"))
            self.assertEqual(learnings[0]["experiment_id"], self.eid)
        path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
