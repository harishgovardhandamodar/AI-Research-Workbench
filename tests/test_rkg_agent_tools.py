"""Agent↔RKG bridge: the rkg__* tools the chat agent uses to ground itself in
the shared knowledge graph.

The tools must (a) be present in the tool schemas and build_tools, (b) resolve
the SAME lazily-built Organizer/Workbench singleton as the /api/rkg router, and
(c) degrade to a clear message instead of crashing the turn when RKG is
unavailable.

The integration tests in RkgAgentIntegrationTests go further: they install a
real workbench into the router singletons and call the tools through the real
`_rkg_runtime()` wiring (no mocked runtime), proving the agent turn actually
sees the shared graph.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.agents.tools import (
    ToolContext,
    build_tools,
    get_tool_schemas,
    _rkg_paper_notes,
    _rkg_query_rag,
    _rkg_scenario_report,
    _rkg_scenario_status,
)
from backend.research_knowledge_graphs.config import Config
from backend.research_knowledge_graphs.graph import KnowledgeGraph
from backend.research_knowledge_graphs.research_loop import ResearchWorkbench
from backend.research_knowledge_graphs import router as rkg_router


class _FakeOrg:
    def query_rag(self, question):
        return {"answer": f"Grounded answer to {question!r}.",
                "sources": [{"id": "2401.00001", "title": "A Grounding Paper"}]}


class _FakeWB:
    def paper_notes(self, paper_id):
        return {"found": True, "id": paper_id, "title": "Paper Title",
                "published": "2024-01-01", "abstract": "Abstract text",
                "tags": ["tag1"], "concepts": ["Concept A"], "notes": "Notes."}

    def status(self, scenario_id):
        return {"id": scenario_id, "status": {"phase": "done",
                                              "phase_label": "Done",
                                              "progress": 1.0},
                "corpus_size": 5, "best_score": 82.0, "report_exists": True}

    def report(self, scenario_id):
        return f"# Report for {scenario_id}\n\nBody."


class RkgAgentToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def ctx(self):
        return ToolContext(kernels=None, artifacts=None, store=None,
                           permissions=None)

    def test_rkg_tools_registered_in_schemas(self):
        names = {t["function"]["name"] for t in get_tool_schemas()}
        for name in ("rkg__query_rag", "rkg__paper_notes",
                     "rkg__scenario_status", "rkg__scenario_report"):
            self.assertIn(name, names)

    def test_rkg_tools_wired_into_build_tools(self):
        tools = build_tools(self.ctx())
        for name in ("rkg__query_rag", "rkg__paper_notes",
                     "rkg__scenario_status", "rkg__scenario_report"):
            self.assertIn(name, tools)

    async def test_query_rag_returns_answer_and_sources(self):
        with patch("backend.agents.tools._rkg_runtime",
                   return_value=(_FakeOrg(), _FakeWB())):
            out = await _rkg_query_rag(self.ctx(), "what is grounding?")
        self.assertIn("Grounded answer to 'what is grounding?'", out)
        self.assertIn("2401.00001", out)
        self.assertIn("A Grounding Paper", out)

    async def test_paper_notes_returns_metadata(self):
        with patch("backend.agents.tools._rkg_runtime",
                   return_value=(_FakeOrg(), _FakeWB())):
            out = await _rkg_paper_notes(self.ctx(), "2401.00001")
        self.assertIn("Paper Title", out)
        self.assertIn("[arXiv:2401.00001]", out)
        self.assertIn("Concept A", out)

    async def test_scenario_status_returns_phase_and_score(self):
        with patch("backend.agents.tools._rkg_runtime",
                   return_value=(_FakeOrg(), _FakeWB())):
            out = await _rkg_scenario_status(self.ctx(), "autonomous-agents-security")
        self.assertIn("phase=Done", out)
        self.assertIn("best_score=82.0", out)
        self.assertIn("corpus_size=5", out)

    async def test_scenario_report_returns_report(self):
        with patch("backend.agents.tools._rkg_runtime",
                   return_value=(_FakeOrg(), _FakeWB())):
            out = await _rkg_scenario_report(self.ctx(), "autonomous-agents-security")
        self.assertIn("# Report for autonomous-agents-security", out)

    async def test_rkg_unavailable_returns_message_not_crash(self):
        with patch("backend.agents.tools._rkg_runtime",
                   side_effect=RuntimeError("ollama down")):
            out = await _rkg_query_rag(self.ctx(), "x")
        self.assertTrue(out.startswith("[error] RKG unavailable"))

    async def test_missing_args_are_rejected(self):
        self.assertTrue((await _rkg_query_rag(self.ctx(), "")).startswith("[error]"))
        self.assertTrue((await _rkg_paper_notes(self.ctx(), "")).startswith("[error]"))
        self.assertTrue((await _rkg_scenario_status(self.ctx(), "")).startswith("[error]"))
        self.assertTrue((await _rkg_scenario_report(self.ctx(), "")).startswith("[error]"))


class _IntegrationPool:
    def get_topics(self):
        return []

    def add_topic(self, name, query):
        pass

    def refresh(self):
        return {}

    def get_observed_papers(self):
        return []

    def mark_imported(self, arxiv_id):
        pass


class _IntegrationOrg:
    """Real-enough Organizer for the workbench + a scripted RAG answer."""

    def __init__(self, config, kg):
        self.config = config
        self.kg = kg
        self.pool = _IntegrationPool()
        self.llm = _IntegrationLLM()

    def query_rag(self, question):
        return {"answer": f"Grounded answer to {question!r}.",
                "sources": [{"id": "2401.00001", "title": "A Grounding Paper"}]}


class _IntegrationLLM:
    def generate(self, prompt, model=None):
        return "# Report\n\nBody.\n"

    def extract_structured(self, prompt, model=None):
        return {"score": 80, "feedback": [], "improvements": []}

    def embed(self, text):
        return [0.0] * 8

    def embed_parallel(self, texts):
        return [[0.0] * 8 for _ in texts]


class RkgAgentIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """Tools called through the real _rkg_runtime() singletons, no mocks."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        config = Config()
        config.data = {"directories": {"root": str(self.tmp)}}
        kg = KnowledgeGraph(config)
        org = _IntegrationOrg(config, kg)
        kg.add_paper(paper_id="2401.00001", title="A Grounding Paper",
                     authors="A", published="2024-01-01",
                     abstract="Abstract about grounding.")
        wb = ResearchWorkbench(org)
        sc = wb.get("autonomous-agents-security")
        sc["corpus"] = ["2401.00001"]
        sc["best_score"] = 82.0
        wb._save(sc)
        kg.save()
        self.org = org
        self.wb = wb
        # Install into the router singletons so _rkg_runtime() finds them.
        rkg_router._org = org
        rkg_router._wb = wb

    def tearDown(self):
        rkg_router._org = None
        rkg_router._wb = None

    def ctx(self):
        return ToolContext(kernels=None, artifacts=None, store=None,
                           permissions=None)

    def test_tools_resolve_the_router_singletons(self):
        from backend.agents.tools import _rkg_runtime
        org, wb = _rkg_runtime()
        self.assertIs(org, self.org)
        self.assertIs(wb, self.wb)

    async def test_query_rag_through_real_runtime(self):
        out = await _rkg_query_rag(self.ctx(), "what is grounding?")
        self.assertIn("Grounded answer", out)
        self.assertIn("2401.00001", out)

    async def test_paper_notes_through_real_runtime(self):
        out = await _rkg_paper_notes(self.ctx(), "2401.00001")
        self.assertIn("A Grounding Paper", out)
        self.assertIn("[arXiv:2401.00001]", out)

    async def test_paper_notes_missing_paper_is_clear(self):
        out = await _rkg_paper_notes(self.ctx(), "9999.99999")
        self.assertIn("not in the knowledge graph", out)

    async def test_scenario_status_through_real_runtime(self):
        out = await _rkg_scenario_status(self.ctx(), "autonomous-agents-security")
        self.assertIn("phase=", out)
        self.assertIn("corpus_size=1", out)
        self.assertIn("best_score=82.0", out)

    async def test_scenario_report_through_real_runtime(self):
        sc = self.wb.get("autonomous-agents-security")
        report_path = self.wb._report_path("autonomous-agents-security")
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text("# Scenario Report\n\nBody.", encoding="utf-8")
        sc["report_exists"] = True
        self.wb._save(sc)
        out = await _rkg_scenario_report(self.ctx(), "autonomous-agents-security")
        self.assertIn("# Scenario Report", out)

    async def test_tools_share_graph_with_dashboard(self):
        """Adding a paper through the RKG router path is visible to the tools."""
        org, wb = rkg_router.get_org(), rkg_router.get_workbench()
        self.assertIs(org, self.org)
        self.assertIs(wb, self.wb)
        out = await _rkg_paper_notes(self.ctx(), "2401.00001")
        self.assertIn("A Grounding Paper", out)


if __name__ == "__main__":
    unittest.main()
