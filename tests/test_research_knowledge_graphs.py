"""Smoke tests for the vendored Research Knowledge Graphs router.

The router serves the knowledge-graph REST API (namespaced /api/rkg), the
dashboard/landscape views, and returns empty graphs before any papers are
ingested. These tests avoid touching Ollama / GPU / arXiv: they point the data
root at a temp workbench dir and only exercise read-only endpoints.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient


class RkgRouterTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        os.environ["FOX_WORKBENCH_DIR"] = cls._tmp.name
        from backend.main import app

        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_dashboard_view_served(self):
        r = self.client.get("/rkg/dashboard")
        self.assertEqual(r.status_code, 200)
        self.assertIn("<!DOCTYPE html>", r.text)
        # The vendored SPA talks to the namespaced API prefix.
        self.assertIn("/api/rkg/", r.text)

    def test_landscape_view_served(self):
        r = self.client.get("/rkg/landscape")
        self.assertEqual(r.status_code, 200)
        self.assertIn("<!DOCTYPE html>", r.text)

    def test_graph_empty_before_ingestion(self):
        r = self.client.get("/api/rkg/graph")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertIn("nodes", data)
        self.assertIn("links", data)
        self.assertEqual(data["nodes"], [])

    def test_stats_empty(self):
        r = self.client.get("/api/rkg/stats")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["papers"], 0)
        self.assertEqual(data["concepts"], 0)

    def test_papers_empty(self):
        r = self.client.get("/api/rkg/papers")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), [])

    def test_pool_topics_seeded(self):
        r = self.client.get("/api/rkg/pool/topics")
        self.assertEqual(r.status_code, 200)
        topics = r.json().get("topics", [])
        self.assertGreater(len(topics), 0)

    def test_read_rejects_missing_path(self):
        r = self.client.get("/api/rkg/read")
        self.assertEqual(r.status_code, 400)

    def test_raw_rejects_missing_path(self):
        r = self.client.get("/api/rkg/raw")
        self.assertEqual(r.status_code, 400)

    def test_post_requires_body_fields(self):
        r = self.client.post("/api/rkg/query", json={})
        self.assertEqual(r.status_code, 400)

    def test_openapi_lists_rkg_routes(self):
        paths = self.client.get("/openapi.json").json()["paths"]
        self.assertIn("/api/rkg/graph", paths)
        self.assertIn("/rkg/dashboard", paths)

    # ------------------------------------------------------------ scenarios ---

    def test_scenarios_seed_two_sample_domains(self):
        r = self.client.get("/api/rkg/scenarios")
        self.assertEqual(r.status_code, 200)
        scs = r.json().get("scenarios", [])
        self.assertEqual(len(scs), 2)
        ids = {s["id"] for s in scs}
        self.assertEqual(ids, {"autonomous-agents-security", "enterprise-ai-security"})
        for s in scs:
            self.assertGreater(len(s["topics"]), 0)

    def test_scenario_topics_seeded_into_pool(self):
        self.client.get("/api/rkg/scenarios")
        r = self.client.get("/api/rkg/pool/topics")
        names = {t["name"] for t in r.json().get("topics", [])}
        self.assertIn("Agents-Security: LLM agent security", names)
        self.assertIn("Enterprise-AI: adoption", names)

    def test_scenario_detail_and_status(self):
        r = self.client.get("/api/rkg/scenarios/autonomous-agents-security")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["name"], "Autonomous Agents & Security Lapses")
        st = self.client.get("/api/rkg/scenarios/autonomous-agents-security/status").json()
        self.assertEqual(st["status"]["phase"], "idle")
        self.assertEqual(st["corpus_size"], 0)

    def test_scenario_report_empty_before_loop(self):
        r = self.client.get("/api/rkg/scenarios/enterprise-ai-security/report")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["report"], "")

    def test_scenario_gaps_return_suggestions(self):
        r = self.client.get("/api/rkg/scenarios/autonomous-agents-security/gaps")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["scenario"], "autonomous-agents-security")
        self.assertIn("count", data)
        self.assertIn("suggestions", data)
        for s in data["suggestions"]:
            self.assertIn("type", s)
            self.assertIn("evidence", s)
            self.assertIn("hypothesis", s)
            self.assertIn("arxiv_query", s)
        # With an empty corpus every seeded topic is untouched.
        self.assertIn("untouched_topic",
                      {s["type"] for s in data["suggestions"]})

    def test_scenario_gaps_unknown_404(self):
        r = self.client.get("/api/rkg/scenarios/does-not-exist/gaps")
        self.assertEqual(r.status_code, 404)

    def test_scheduler_status_reports_disabled_when_off(self):
        r = self.client.get("/api/rkg/scheduler/status")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["enabled"], False)
        self.assertIn("configured_check_minutes", data)
        self.assertIn("synthesize", data)
        self.assertIn("active", data)
        self.assertIn("due_scenarios", data)

    def test_scheduler_tick_409_when_disabled(self):
        r = self.client.post("/api/rkg/scheduler/tick")
        self.assertEqual(r.status_code, 409)
        self.assertIn("error", r.json())

    def test_unknown_scenario_404(self):
        r = self.client.get("/api/rkg/scenarios/does-not-exist")
        self.assertEqual(r.status_code, 404)
