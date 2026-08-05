"""Mocked-LLM tests for the Research Workbench loop phases.

Drive run_synthesis / run_experiments / run_full_loop with a scripted fake LLM
(no Ollama, no arXiv, no subprocesses) to verify the loop-quality behaviours:

- C1: plateau early-stopping + citation audit (ungrounded [arXiv:xxx] stripped)
- C2: multi-run mean keep/revert + delta vs paper-reported metric
- C3: replication results table fed into the fold-back synthesis
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend.research_knowledge_graphs.config import Config
from backend.research_knowledge_graphs.graph import KnowledgeGraph
from backend.research_knowledge_graphs.research_loop import ResearchWorkbench


class _FakeLLM:
    """Scripted LLM: generate() returns queued texts, extract_structured()
    returns queued review dicts. Both fall back to a stable default."""

    def __init__(self, reports=None, reviews=None):
        self.reports = list(reports or [])
        self.reviews = list(reviews or [])
        self.calls = []

    def generate(self, prompt, model=None):
        self.calls.append(("generate", model))
        if self.reports:
            return self.reports.pop(0)
        return "# Report\n\nSection one.\n\n## References"

    def extract_structured(self, prompt, model=None):
        self.calls.append(("review", model))
        if self.reviews:
            return self.reviews.pop(0)
        return {"score": 50, "feedback": ["ok"], "improvements": ["none"]}

    def embed(self, text):
        return [0.0] * 8

    def embed_parallel(self, texts):
        return [[0.0] * 8 for _ in texts]


class _FakePool:
    def __init__(self):
        self.topics = []
        self.observed = []

    def get_topics(self):
        return list(self.topics)

    def add_topic(self, name, query):
        self.topics.append({"name": name, "query": query})

    def refresh(self):
        return {}

    def get_observed_papers(self):
        return list(self.observed)

    def mark_imported(self, arxiv_id):
        for p in self.observed:
            if p["arxiv_id"] == arxiv_id:
                p["imported"] = True


class _FakeOrg:
    def __init__(self, config, llm):
        self.config = config
        self.kg = KnowledgeGraph(config)
        self.llm = llm
        self.pool = _FakePool()

    def add_by_id(self, arxiv_id, model=None):
        return {"status": "added", "paper_id": arxiv_id}


def _make_wb(tmp: Path, llm, corpus_ids=("2401.00001", "2401.00002")):
    config = Config()
    # Force a per-test data root: Config reads config.yaml by default, so build
    # one fresh pointing at the temp dir.
    config.data = {"directories": {"root": str(tmp)}}
    org = _FakeOrg(config, llm)
    wb = ResearchWorkbench(org)
    for cid in corpus_ids:
        org.kg.add_paper(paper_id=cid, title=f"Paper {cid}", authors="A",
                         published="2024-01-01", abstract="An abstract.")
    sc = wb.get("autonomous-agents-security")
    sc["corpus"] = list(corpus_ids)
    sc["loop"] = {"import_top": 5, "max_iters": 5, "per_iter_budget": 45,
                  "top_experiments": 2, "review_target": 95,
                  "plateau_iters": 2, "num_runs": 3}
    wb._save(sc)
    org.kg.save()
    return wb


REPORT_WITH_CITATIONS = (
    "# Autonomous Agents\n\n"
    "Claim grounded in [arXiv:2401.00001] and the shaky [arXiv:9999.00000].\n\n"
    "## References\n"
    "- [arXiv:2401.00001] A paper\n"
    "- [arXiv:9999.00000] Not in corpus\n"
)


class CitationAuditTests(unittest.TestCase):
    def test_extract_citations_dedupes(self):
        report = "See [arXiv:2401.00001] and [arXiv:2401.00001] and [arXiv:2401.00002]."
        self.assertEqual(ResearchWorkbench._extract_citations(report),
                         ["2401.00001", "2401.00002"])

    def test_audit_strips_ungrounded_and_keeps_verified(self):
        tmp = Path(tempfile.mkdtemp())
        wb = _make_wb(tmp, _FakeLLM())
        report, audit = wb._audit_citations("autonomous-agents-security",
                                            REPORT_WITH_CITATIONS)
        self.assertEqual(audit["cited"], 2)
        self.assertEqual(audit["verified"], 1)
        self.assertEqual(audit["removed"], 1)
        self.assertEqual(audit["missing"], ["9999.00000"])
        self.assertNotIn("9999.00000", report)
        self.assertIn("2401.00001", report)

    def test_audit_empty_report(self):
        tmp = Path(tempfile.mkdtemp())
        wb = _make_wb(tmp, _FakeLLM())
        report, audit = wb._audit_citations("autonomous-agents-security",
                                            "no citations here")
        self.assertEqual(audit["cited"], 0)
        self.assertEqual(report, "no citations here")


class ReplicationQualityTests(unittest.TestCase):
    """C2: keep/revert on multi-run means + delta vs paper-reported metric."""

    def test_measure_metric_averages_multiple_runs(self):
        tmp = Path(tempfile.mkdtemp())
        exp = tmp / "experiment.py"
        exp.write_text(
            "import random\n"
            "print('METRIC acc=%.4f' % (0.8 + random.random()*0.1))\n",
            encoding="utf-8")
        with mock.patch("backend.autoresearch.run_research_experiment") as runner:
            runner.side_effect = [
                {"ok": True, "output": "METRIC acc=0.8", "metric": ("acc", 0.8)},
                {"ok": True, "output": "METRIC acc=0.9", "metric": ("acc", 0.9)},
                {"ok": True, "output": "METRIC acc=0.7", "metric": ("acc", 0.7)},
            ]
            res = ResearchWorkbench._measure_metric(exp, 30, num_runs=3)
        self.assertTrue(res["ok"])
        self.assertEqual(res["metric"], "acc")
        self.assertAlmostEqual(res["mean"], 0.8)
        self.assertEqual(runner.call_count, 3)

    def test_measure_metric_empty_outputs(self):
        tmp = Path(tempfile.mkdtemp())
        exp = tmp / "experiment.py"
        exp.write_text("print('nope')\n", encoding="utf-8")
        with mock.patch("backend.autoresearch.run_research_experiment",
                        return_value={"ok": True, "output": "no metric"}):
            res = ResearchWorkbench._measure_metric(exp, 30, num_runs=3)
        self.assertFalse(res["ok"])
        self.assertIsNone(res["mean"])

    def test_extract_paper_metric_from_spec(self):
        spec = {"goal": "Replicate accuracy; the paper reports 0.86 accuracy",
                "dataset": "synthetic"}
        self.assertAlmostEqual(ResearchWorkbench._extract_paper_metric(spec), 0.86)

    def test_extract_paper_metric_none_when_absent(self):
        self.assertIsNone(ResearchWorkbench._extract_paper_metric({"setup": "plain"}))

    def test_improve_experiment_keeps_only_better_mean_and_records_delta(self):
        tmp = Path(tempfile.mkdtemp())
        exp_dir = tmp / "experiments" / "2401_00001"
        exp_dir.mkdir(parents=True)
        spec = {"goal": "accuracy", "baselines": "paper reports 0.80 accuracy"}
        llm = _FakeLLM(reports=[
            "```python\nprint('METRIC acc=0.85')\n```",   # candidate: better mean
            "```python\nprint('METRIC acc=0.70')\n```",   # candidate: worse mean
        ])
        wb = _make_wb(tmp / "wb", llm, corpus_ids=("2401.00001",))
        cfg = {"per_iter_budget": 30, "experiment_iters": 2, "num_runs": 3}

        means = {"base": 0.80, "cand1": 0.85, "cand2": 0.70}

        def fake_runner(exp_file, budget):
            text = exp_file.read_text()
            val = means["base"]
            for key, v in means.items():
                if key != "base" and f"METRIC acc={v}" in text:
                    val = v
            return {"ok": True, "output": f"METRIC acc={val}", "metric": ("acc", val)}

        with mock.patch("backend.autoresearch.run_research_experiment",
                        side_effect=fake_runner):
            result = wb._improve_experiment(
                "autonomous-agents-security", "2401.00001", exp_dir,
                "print('METRIC acc=0.80')", spec, cfg, model=None)

        self.assertEqual(result["metric"], "acc")
        self.assertEqual(result["best_value"], 0.85)  # cand1 kept (better mean)
        self.assertEqual(result["kept_improvements"], 1)
        self.assertEqual(result["num_runs"], 3)
        self.assertAlmostEqual(result["paper_reported"], 0.80)
        self.assertAlmostEqual(result["delta_vs_paper"], 0.05)
        results_md = (exp_dir / "results.md").read_text(encoding="utf-8")
        self.assertIn("Δ vs paper", results_md)
        # The final experiment.py on disk is the kept (better) candidate.
        final_code = (exp_dir / "experiment.py").read_text(encoding="utf-8")
        self.assertIn("0.85", final_code)


class FoldBackTests(unittest.TestCase):
    """C3: quantitative replication table rendered and folded into the report."""

    @staticmethod
    def _reviews(*scores):
        return [{"score": s, "feedback": [], "improvements": []} for s in scores]

    def _sample_results(self):
        return [
            {"paper_id": "2401.00001", "metric": "acc", "best_value": 0.85,
             "paper_reported": 0.80, "delta_vs_paper": 0.05, "num_runs": 3},
            {"paper_id": "2402.00002", "metric": "acc", "best_value": None,
             "paper_reported": None, "delta_vs_paper": None, "num_runs": 3},
        ]

    def test_replication_table_rows(self):
        table = ResearchWorkbench._replication_table(self._sample_results())
        self.assertIn("| Paper | Metric | Workbench best | Paper-reported |", table)
        self.assertIn("| [2401.00001] | acc | 0.85 | 0.8 | +0.05 | 3 |", table)
        self.assertIn("| [2402.00002] | acc | — | — | — | 3 |", table)

    def test_replication_table_empty(self):
        self.assertEqual(ResearchWorkbench._replication_table([]), "")

    def test_embed_replication_section_appends(self):
        report = "# Synthesis\n\nBody text.\n"
        out = ResearchWorkbench._embed_replication_section(report, "TBL")
        self.assertIn("## Replication Results (Workbench)", out)
        self.assertIn("TBL", out)
        self.assertTrue(out.endswith("\n"))

    def test_embed_replication_section_refreshes(self):
        report = ("# Synthesis\n\nBody.\n"
                  "## Replication Results (Workbench)\n\nSTALE\n\n## Old\n")
        out = ResearchWorkbench._embed_replication_section(report, "FRESH")
        self.assertNotIn("STALE", out)
        self.assertIn("FRESH", out)
        self.assertIn("## Old", out)

    def test_fold_back_embeds_table_into_report(self):
        tmp = Path(tempfile.mkdtemp())
        llm = _FakeLLM(reports=["# Draft\n[arXiv:2401.00001]\n", "keep"],
                       reviews=self._reviews(90, 91))
        wb = _make_wb(tmp / "wb", llm, corpus_ids=("2401.00001",))
        sc = wb.get("autonomous-agents-security")
        sc["replication"] = {"results": self._sample_results(), "ran_at": "now"}
        wb._save(sc)
        result = wb.run_synthesis(
            "autonomous-agents-security", include_experiments=True, model=None)
        report = (Path(result["report_path"])).read_text(encoding="utf-8")
        self.assertIn("## Replication Results (Workbench)", report)
        self.assertIn("| [2401.00001] | acc | 0.85 | 0.8 | +0.05 | 3 |", report)
        self.assertIn("replication_table", result)
        self.assertIsNotNone(result["replication_table"])


class SynthesisLoopTests(unittest.IsolatedAsyncioTestCase):
    def _reviews(self, *scores):
        return [{"score": s, "feedback": [], "improvements": []} for s in scores]

    def test_plateau_early_stop_and_citation_audit(self):
        """Scores 98 → 97 → 96 must stop after 2 no-improve iterations, keep 98,
        and strip the ungrounded citation from the persisted report."""
        tmp = Path(tempfile.mkdtemp())
        llm = _FakeLLM(
            reports=["# Draft\n\n[arXiv:9999.00000]\n", "one", "two"],
            reviews=self._reviews(98, 97, 96),
        )
        wb = _make_wb(tmp, llm, corpus_ids=("2401.00001", "2401.00002"))
        # Raise review_target so the plateau check (not the target) triggers.
        sc = wb.get("autonomous-agents-security")
        sc["loop"]["review_target"] = 100
        wb._save(sc)
        result = wb.run_synthesis("autonomous-agents-security")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["best_score"], 98.0)
        self.assertIsNotNone(result["early_stopped"])
        self.assertIn("no improvement", result["early_stopped"])
        # Only the initial + 2 improvement iterations ran.
        self.assertEqual(len(result["iterations"]), 2)
        self.assertEqual([h["score"] for h in result["iterations"]], [97.0, 96.0])
        # The persisted report no longer contains the fake citation.
        report = wb.report("autonomous-agents-security")
        self.assertNotIn("9999.00000", report)
        audit = result["citation_audit"]
        self.assertEqual(audit["removed"], 1)

    def test_stops_at_review_target(self):
        tmp = Path(tempfile.mkdtemp())
        llm = _FakeLLM(reviews=self._reviews(96, 97))
        wb = _make_wb(tmp, llm, corpus_ids=("2401.00001", "2401.00002"))
        result = wb.run_synthesis("autonomous-agents-security")
        self.assertEqual(result["best_score"], 97.0)
        self.assertIn("review_target", result["early_stopped"])
        self.assertEqual(len(result["iterations"]), 1)

    def test_empty_corpus_errors(self):
        tmp = Path(tempfile.mkdtemp())
        wb = _make_wb(tmp, _FakeLLM(), corpus_ids=())
        result = wb.run_synthesis("autonomous-agents-security")
        self.assertEqual(result["status"], "error")
        self.assertIn("corpus", result["reason"])


class GapDiscoveryTests(unittest.TestCase):
    """B2: gaps() surfaces untouched topics, unlinked papers, orphan concepts,
    and papers lacking experiments."""

    def test_gaps_covers_all_four_types(self):
        tmp = Path(tempfile.mkdtemp())
        wb = _make_wb(tmp, _FakeLLM(), corpus_ids=("2401.00001",))
        wb.pool.observed = [
            {"arxiv_id": "2401.00001", "title": "Paper", "topics": ["Agents-Security: agent safety"],
             "imported": True},
            {"arxiv_id": "2403.00003", "title": "Fresh", "topics": ["Agents-Security: agent safety"],
             "imported": False},
        ]
        wb.kg.add_concept("orphan", "Orphan Concept", "unlinked")
        result = wb.gaps("autonomous-agents-security")
        self.assertEqual(result["status"], "ok")
        types = {s["type"] for s in result["suggestions"]}
        self.assertIn("untouched_topic", types)
        self.assertIn("orphan_concept", types)
        self.assertIn("unlinked_paper", types)
        self.assertIn("no_experiment", types)
        # Untouched topics are those with zero *imported* corpus papers.
        untouched = [s for s in result["suggestions"]
                     if s["type"] == "untouched_topic"]
        self.assertTrue(any("agent jailbreak" in s["arxiv_query"]
                            for s in untouched))
        # Every suggestion carries an arXiv query to act on.
        for s in result["suggestions"]:
            self.assertIn("arxiv_query", s)
            self.assertTrue(s["arxiv_query"])

    def test_gaps_unknown_scenario(self):
        tmp = Path(tempfile.mkdtemp())
        wb = _make_wb(tmp, _FakeLLM())
        result = wb.gaps("does-not-exist")
        self.assertEqual(result["status"], "error")

    def test_gaps_no_orphan_when_concept_linked(self):
        tmp = Path(tempfile.mkdtemp())
        wb = _make_wb(tmp, _FakeLLM(), corpus_ids=("2401.00001",))
        wb.kg.add_concept("linked", "Linked", "def")
        wb.kg.add_edge("linked", "2401.00001")
        result = wb.gaps("autonomous-agents-security")
        self.assertNotIn(
            "orphan_concept",
            {s["type"] for s in result["suggestions"]})


class ExperimentLoopTests(unittest.TestCase):
    """D1: end-to-end run_experiments with a mocked LLM + subprocess runner."""

    def _mk_wb(self):
        tmp = Path(tempfile.mkdtemp())
        llm = _FakeLLM(reports=["```python\nprint('METRIC acc=0.82')\n```"])
        wb = _make_wb(tmp, llm, corpus_ids=("2401.00001", "2401.00002"))
        return tmp, llm, wb

    def test_run_experiments_replicates_ranked_papers(self):
        tmp, _llm, wb = self._mk_wb()
        # Create the vault note so the replication block can be appended.
        note = wb._replication_note_path("2401.00001")
        self.assertIsNone(note)
        from backend.research_knowledge_graphs.pipeline import _sanitize_id
        n = wb.kg.get_paper("2401.00001")
        safe = _sanitize_id(n.label)
        note_dir = Path(wb.config.vault_dir) / safe
        note_dir.mkdir(parents=True, exist_ok=True)
        (note_dir / "00_notes.md").write_text("# Notes\n", encoding="utf-8")
        with mock.patch.object(ResearchWorkbench, "_experiment_specs") as specs, \
             mock.patch.object(ResearchWorkbench, "_generate_experiment_code") as gen, \
             mock.patch("backend.autoresearch.run_research_experiment",
                        return_value={"ok": True, "output": "METRIC acc=0.82",
                                      "metric": ("acc", 0.82)}):
            specs.return_value = [{"goal": "accuracy", "baselines": "0.80"}]
            gen.return_value = "print('METRIC acc=0.82')"
            result = wb.run_experiments("autonomous-agents-security")
        outcomes = result["results"]
        self.assertEqual(result["status"], "ok")
        self.assertGreaterEqual(len(outcomes), 1)
        self.assertEqual(outcomes[0]["paper_id"], "2401.00001")
        self.assertAlmostEqual(outcomes[0]["best_value"], 0.82)
        self.assertEqual(outcomes[0]["num_runs"], 3)
        # Replication results were persisted for fold-back.
        sc = wb.get("autonomous-agents-security")
        self.assertIn("replication", sc)
        self.assertEqual(sc["replication"]["results"], outcomes)
        # The vault experiment note got the replication block appended.
        note = wb._replication_note_path("2401.00001")
        self.assertIsNotNone(note)
        text = note.read_text(encoding="utf-8")
        self.assertIn("Replication (Research Workbench)", text)

    def test_run_experiments_skips_papers_without_specs(self):
        tmp, _llm, wb = self._mk_wb()
        with mock.patch.object(ResearchWorkbench, "_experiment_specs",
                               return_value=[]):
            result = wb.run_experiments("autonomous-agents-security")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["results"], [])


class FullLoopTests(unittest.TestCase):
    """D1: run_full_loop chains build → synth → experiments → fold back."""

    def test_full_loop_chains_phases_and_records_last_loop(self):
        tmp = Path(tempfile.mkdtemp())
        llm = _FakeLLM(reports=["# Draft\n\nBody.\n", "# Improved\n\nBody.\n"])
        wb = _make_wb(tmp, llm, corpus_ids=("2401.00001", "2401.00002"))
        called = []
        with mock.patch.object(ResearchWorkbench, "_experiment_specs") as specs, \
             mock.patch.object(ResearchWorkbench, "_generate_experiment_code") as gen, \
             mock.patch("backend.autoresearch.run_research_experiment",
                        return_value={"ok": True, "output": "METRIC acc=0.8",
                                      "metric": ("acc", 0.8)}), \
             mock.patch.object(ResearchWorkbench, "run_synthesis",
                               side_effect=_wrap_chain(called)):
            specs.return_value = [{"goal": "accuracy"}]
            gen.return_value = "print('METRIC acc=0.8')"
            result = wb.run_full_loop("autonomous-agents-security")

        self.assertEqual(result["status"], "ok")
        self.assertEqual(called, ["synthesis", "foldback"])
        self.assertIn("last_loop", wb.get("autonomous-agents-security"))
        last = wb.get("autonomous-agents-security")["last_loop"]
        self.assertEqual(last["experiments"], 2)
        self.assertEqual(last["imported"], 0)
        # Fold-back synthesis ran with include_experiments=True.
        self.assertTrue(result["update"]["foldback"])

    def test_full_loop_fails_on_empty_corpus(self):
        tmp = Path(tempfile.mkdtemp())
        llm = _FakeLLM(reports=["# Draft\n\nBody.\n"])
        wb = _make_wb(tmp, llm, corpus_ids=())
        result = wb.run_full_loop("autonomous-agents-security")
        self.assertEqual(result["status"], "error")
        self.assertIn("corpus", result["reason"])


def _wrap_chain(called):
    """Side effect that records run_synthesis invocations and returns canned
    results matching what run_full_loop consumes (foldback flag preserved)."""
    def _run(sid, include_experiments=False, model=None):
        called.append("foldback" if include_experiments else "synthesis")
        return {"status": "ok", "best_score": 90.0, "foldback": include_experiments}
    return _run


if __name__ == "__main__":
    unittest.main()
