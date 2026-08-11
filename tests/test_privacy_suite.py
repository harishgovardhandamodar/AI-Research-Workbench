"""Privacy exploit suite tests: deterministic detection + running all privacy
experiments across datasets with an aggregated report."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend.main import _is_privacy_suite_request


class TestPrivacySuiteDetection(unittest.TestCase):
    def test_suite_request_detected(self):
        for text in (
            "run all privacy exploits on these datasets and prepare a detailed report",
            "Run all privacy exploits on these datasets and prepare a detailed report",
            "run the full privacy attack suite and summarize",
            "run every privacy scenario on both datasets",
        ):
            self.assertTrue(_is_privacy_suite_request(text), text)

    def test_narrow_requests_not_detected_as_suite(self):
        for text in (
            "assess the plausibility of privacy exploits by banks",
            "make a distribution of transaction type",
            "improve the experiment toward the goal",
        ):
            self.assertFalse(_is_privacy_suite_request(text), text)


def _upi_df():
    import pandas as pd
    return pd.DataFrame({
        "sender_bank": ["HDFC"] * 40 + ["SBI"] * 40 + ["ICICI"] * 20,
        "transaction type": ["UPI"] * 60 + ["IMPS"] * 40,
        "amount (INR)": list(range(100)),
        "merchant_category": ["retail"] * 50 + ["dining"] * 50,
        "sender_age_group": ["18-25"] * 30 + ["26-35"] * 40 + ["36-45"] * 30,
        "email": [f"u{i}@x.com" for i in range(100)],
        "phone": [f"+91{i:010d}" for i in range(100)],
    })


class TestPrivacySuiteRun(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import backend.project_runtime as pr
        from backend.state import runtimes
        self.tmp = Path(tempfile.mkdtemp())
        self._orig = pr.PROJECTS_DIR
        pr.PROJECTS_DIR = self.tmp
        self.rt = pr.ProjectRuntime("suite")
        runtimes["suite"] = self.rt
        _upi_df().to_csv(self.rt.dir / "upi_transactions.csv", index=False)

    async def asyncTearDown(self):
        import backend.project_runtime as pr
        from backend.state import runtimes
        pr.PROJECTS_DIR = self._orig
        runtimes.pop("suite", None)
        await self.rt.stop()

    async def test_runs_all_experiments_and_builds_report(self):
        from backend.privacy_suite import (SUITE_EXPERIMENTS, run_privacy_suite)
        steps = []

        async def progress(done, total, msg):
            steps.append((done, total, msg))

        out = await run_privacy_suite(self.rt, progress=progress)
        self.assertEqual(out["datasets"], ["upi_transactions.csv"])
        self.assertTrue(out["report_id"])
        self.assertGreater(len(out["figure_ids"]), 0)
        self.assertIn("| Dataset |", out["report"])
        for exp in SUITE_EXPERIMENTS:
            self.assertIn(f"### {exp}", out["report"])
        self.assertEqual(len(steps), len(SUITE_EXPERIMENTS))

    async def test_handler_emits_report_message(self):
        # Exercise the full suite end-to-end and confirm report + figures land
        # as artifacts (the chat handler wraps this same call).
        from backend.privacy_suite import run_privacy_suite
        out = await run_privacy_suite(self.rt)
        self.assertIn("privacy", out["report"].lower())
        self.assertTrue(any(a.kind == "report"
                            for a in self.rt.artifacts.list()))


if __name__ == "__main__":
    unittest.main()
