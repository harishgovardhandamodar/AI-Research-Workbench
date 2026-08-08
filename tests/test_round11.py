"""Round-11 tests: literature grounding via the RKG (best-effort helper, report
integration, graceful degradation when RKG is unavailable)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend.literature import literature_context, project_question
from backend.store import ProjectStore


class LiteratureContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_returns_block_with_sources(self):
        def fake_query(question):
            return {"answer": "Graph neural networks help on tabular data.",
                    "sources": [{"id": "arXiv:2201.1234", "title": "A paper"},
                                {"id": "arXiv:2105.99", "title": "B paper"}]}
        out = await literature_context("how do GNNs help tabular data?", query_rag=fake_query)
        self.assertIn("Related work", out)
        self.assertIn("Graph neural networks", out)
        self.assertIn("arXiv:2201.1234", out)

    async def test_empty_when_no_sources(self):
        def empty(_q):
            return {"answer": "", "sources": []}
        out = await literature_context("q", query_rag=empty)
        self.assertEqual(out, "")

    async def test_empty_on_exception(self):
        def boom(_q):
            raise RuntimeError("RKG down")
        out = await literature_context("q", query_rag=boom)
        self.assertEqual(out, "")

    async def test_empty_question(self):
        self.assertEqual(await literature_context("  "), "")

    async def test_limits_sources(self):
        def many(_q):
            return {"answer": "x", "sources": [{"id": str(i), "title": str(i)}
                                               for i in range(10)]}
        out = await literature_context("q", limit=3, query_rag=many)
        self.assertEqual(out.count("["), 3)


class ProjectQuestionTests(unittest.TestCase):
    def test_prefers_campaign_question(self):
        store = ProjectStore(Path(tempfile.mkdtemp()))
        store.create_campaign("c", "Which method wins?", "acc", True)
        store.create_experiment("e", "a hypothesis", "", None, True)

        class _Rt:
            pass
        rt = _Rt()
        rt.store = store
        self.assertEqual(project_question(rt), "Which method wins?")

    def test_falls_back_to_hypothesis(self):
        store = ProjectStore(Path(tempfile.mkdtemp()))
        store.create_experiment("e", "a hypothesis", "", None, True)

        class _Rt:
            pass
        rt = _Rt()
        rt.store = store
        self.assertEqual(project_question(rt), "a hypothesis")


class ReviewerDegradationTests(unittest.IsolatedAsyncioTestCase):
    """The reviewer must still work when the RKG is unavailable (best-effort)."""

    async def test_review_without_rkg(self):
        from backend.agents.reviewer import Reviewer
        store = ProjectStore(Path(tempfile.mkdtemp()))
        eid = store.create_experiment("e", "Does X help?", "acc", 0.9, True)
        store.add_message("user", "run the experiment", {"experiment_id": eid})

        class LLM:
            async def complete(self, messages, tools=None, temperature=None, model=None):
                self.prompt = messages[0]["content"]
                return {"content": '{"findings": [], "suggestions": []}'}

        llm = LLM()
        review = await Reviewer(llm, store).review()
        self.assertEqual(review, {"findings": [], "suggestions": []})


class ReportLiteratureTests(unittest.IsolatedAsyncioTestCase):
    async def test_report_gains_related_work(self):
        import backend.literature as lit
        from backend.report import build_project_report

        store = ProjectStore(Path(tempfile.mkdtemp()))
        store.create_campaign("c", "A research question?", "acc", True)

        class _Rt:
            pass

        rt = _Rt()
        rt.name = "proj"
        rt.store = store
        rt.llm = None

        async def _fake_lit(*a, **k):
            return "- Related work: found something"
        lit.literature_context = _fake_lit  # lazy import picks this up

        class _AS:
            def summary(self): return {}
            def count_open_deviations(self): return 0
            def verify_chain(self): return {}
        rt.audit_store = _AS()

        from backend.artifacts.store import ArtifactStore
        rt.artifacts = ArtifactStore(Path(tempfile.mkdtemp()))

        report = await build_project_report(rt, include_summary=False)
        # The report calls literature_context via lazy import; a literal "x"
        # means the section header path exercised. Since literature_context is
        # monkeypatched to return "x" (truthy), the section is added.
        self.assertIn("## Related work", report)


if __name__ == "__main__":
    unittest.main()
