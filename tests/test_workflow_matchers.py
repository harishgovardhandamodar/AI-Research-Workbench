"""Chat intent matchers for the privacy-workflow quick actions."""

from __future__ import annotations

import unittest

from backend.main import (compare_requested, fresh_requested, match_workflow,
                          rerun_compare_requested)


class WorkflowMatcherTests(unittest.TestCase):

    def test_rerun_compare_command_detected(self):
        # The user's combined command — previously fell through to the agent.
        self.assertTrue(rerun_compare_requested(
            "rerun with new seed and compare with last run"))
        self.assertTrue(rerun_compare_requested(
            "Run it again with a fresh seed and compare with the previous run"))
        self.assertTrue(rerun_compare_requested(
            "fresh rerun and comparing with last run"))

    def test_rerun_compare_needs_both(self):
        self.assertFalse(rerun_compare_requested("rerun it with a new seed"))
        self.assertFalse(rerun_compare_requested("compare run A and run B"))
        self.assertFalse(rerun_compare_requested("compare how DP protects data"))
        self.assertFalse(rerun_compare_requested(""))

    def test_combined_command_also_fresh_and_compare(self):
        text = "rerun with new seed and compare with last run"
        self.assertTrue(fresh_requested(text))
        self.assertFalse(compare_requested(text))  # no privacy context phrase
        self.assertIsNone(match_workflow(text))    # no privacy/red-team/exploit word
        self.assertTrue(rerun_compare_requested(text))


if __name__ == "__main__":
    unittest.main()
