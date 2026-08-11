"""Loop-guard tests: near-duplicate assistant replies are detected so the chat
breaks the "model keeps re-planning" loop instead of spinning turns."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend.main import (_agent_looping, _is_continue_request,
                          _text_similarity, _is_privacy_experiment_request,
                          _experiment_from_text)


class TestLoopGuard(unittest.TestCase):
    def test_similarity(self):
        self.assertGreater(_text_similarity(
            "I've been stuck in a loop. Let me run the regeneration attack "
            "simulation now with completely different code:",
            "I've been stuck in a loop for many turns. Let me run the "
            "regeneration attack simulation now with completely different "
            "code:"), 0.85)
        self.assertLess(_text_similarity(
            "load the data", "plot the correlation heatmap"), 0.5)

    def test_continue_detection(self):
        self.assertTrue(_is_continue_request(
            "Continue from where you left off and finish the task. Take as "
            "many tool steps as you need — don't stop early."))
        self.assertTrue(_is_continue_request("keep going"))
        self.assertFalse(_is_continue_request(
            "run the regeneration attack on transaction_type and show results"))


class TestAgentLooping(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import backend.project_runtime as pr
        from backend.state import runtimes
        self.tmp = Path(tempfile.mkdtemp())
        self._orig = pr.PROJECTS_DIR
        pr.PROJECTS_DIR = self.tmp
        self.rt = pr.ProjectRuntime("loopguard")
        runtimes["loopguard"] = self.rt

    async def asyncTearDown(self):
        import backend.project_runtime as pr
        from backend.state import runtimes
        pr.PROJECTS_DIR = self._orig
        runtimes.pop("loopguard", None)
        await self.rt.stop()

    async def test_near_duplicate_replies_flagged(self):
        text = ("I've been stuck in a loop running the same quasi-identifier "
                "analysis. Let me break out and run the regeneration attack "
                "simulation now with completely different code:")
        self.rt.store.add_message("assistant", text, {})
        self.assertFalse(_agent_looping(self.rt))
        self.rt.store.add_message("assistant", text + " Step 1", {})
        self.assertTrue(_agent_looping(self.rt))

    async def test_distinct_replies_not_flagged(self):
        self.rt.store.add_message("assistant", "loaded the dataset, 100k rows", {})
        self.rt.store.add_message("assistant", "the distribution of transaction "
                                              "type is UPI 40%, IMPS 35%...", {})
        self.assertFalse(_agent_looping(self.rt))


class TestPrivacyRouting(unittest.TestCase):
    """Privacy / re-identification requests route to the deterministic planner
    instead of the tool-light LLM loop (which tends to describe work, not do it)."""

    def test_reidentification_routes_to_reid_risk(self):
        text = ("run population reidentification and plausibility of privacy "
                "exploits by banks and financial institution")
        self.assertTrue(_is_privacy_experiment_request(text))
        self.assertEqual(_experiment_from_text(text), "reid_risk")

    def test_k_anonymity_routes(self):
        self.assertEqual(
            _experiment_from_text("assess re-identification risk with k-anonymity"),
            "reid_risk")

    def test_pii_routes_to_pii_scan(self):
        self.assertTrue(_is_privacy_experiment_request("is there any PII in the data?"))
        self.assertEqual(_experiment_from_text("scan for PII"), "pii_scan")

    def test_dp_routes_to_dp_privacy(self):
        self.assertEqual(
            _experiment_from_text("evaluate differential privacy on the amounts"),
            "dp_privacy")

    def test_does_not_hijack_other_intents(self):
        for text in ("show a distribution of transaction type",
                     "improve the experiment toward the goal",
                     "plot the correlation heatmap"):
            self.assertFalse(_is_privacy_experiment_request(text), text)


if __name__ == "__main__":
    unittest.main()
