"""Reviewer suggestion parsing: structured {title, action, prompt} suggestions,
with backward compatibility for plain-string suggestions."""

from __future__ import annotations

import unittest

from backend.agents.reviewer import _normalize_suggestion, _parse_review


class TestReviewerParse(unittest.TestCase):
    def test_parses_structured_suggestions(self):
        text = """{"findings": [{"severity": "warning", "message": "no seed set"}],
                  "suggestions": [{"title": "Try eps=1.0",
                                   "action": "Rerun with epsilon 1.0",
                                   "prompt": "Start variant run 'eps=1.0' with config {\\"eps\\": 1.0} and rerun the experiment."}]}"""
        review = _parse_review(text)
        self.assertEqual(len(review["findings"]), 1)
        self.assertEqual(len(review["suggestions"]), 1)
        s = review["suggestions"][0]
        self.assertEqual(s["title"], "Try eps=1.0")
        self.assertEqual(s["action"], "Rerun with epsilon 1.0")
        self.assertIn("eps", s["prompt"])

    def test_legacy_string_suggestions_normalize(self):
        review = _parse_review('{"findings": [], "suggestions": ["Try a bigger batch size."]}')
        s = review["suggestions"][0]
        self.assertEqual(s["title"], "Try a bigger batch size.")
        self.assertEqual(s["action"], "Try a bigger batch size.")
        self.assertEqual(s["prompt"], "Try a bigger batch size.")

    def test_missing_prompt_falls_back_to_action(self):
        s = _normalize_suggestion({"title": "t", "action": "do the thing"})
        self.assertEqual(s["prompt"], "do the thing")

    def test_empty_suggestions(self):
        self.assertEqual(_parse_review('{"findings": [], "suggestions": []}'),
                         {"findings": [], "suggestions": []})

    def test_garbage_returns_empty(self):
        self.assertEqual(_parse_review("no json here"),
                         {"findings": [], "suggestions": []})


if __name__ == "__main__":
    unittest.main()
