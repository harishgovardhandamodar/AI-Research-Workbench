"""Agent↔RKG bridge: the rkg__* tools the chat agent uses to ground itself in
the shared knowledge graph.

The tools must (a) be present in the tool schemas and build_tools, (b) resolve
the SAME lazily-built Organizer/Workbench singleton as the /api/rkg router, and
(c) degrade to a clear message instead of crashing the turn when RKG is
unavailable.
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


if __name__ == "__main__":
    unittest.main()
