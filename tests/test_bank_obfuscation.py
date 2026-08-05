"""Bank-transaction obfuscation: generator integrity, scenario suite, and the
`run-obfuscation` experiment endpoint (CLI -> app flow)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import pandas as pd
from fastapi.testclient import TestClient

from examples.obfuscation import bank_experiments as bexp
from examples.obfuscation.bank_transactions_data import (
    FIELDNAMES,
    QUASI_IDENTIFIER_COLUMNS,
    SENSITIVE_COLUMNS,
    _iban_mod97,
    generate_bank_transactions,
)


class BankTransactionsDataTests(unittest.TestCase):

    def test_generator_deterministic(self):
        a = generate_bank_transactions(n_rows=200, seed=42)
        b = generate_bank_transactions(n_rows=200, seed=42)
        pd.testing.assert_frame_equal(a, b)
        self.assertEqual(len(a), 200)

    def test_schema(self):
        df = generate_bank_transactions(n_rows=50, seed=1)
        self.assertEqual(list(df.columns), FIELDNAMES)
        self.assertTrue(set(SENSITIVE_COLUMNS) <= set(FIELDNAMES))
        self.assertTrue(set(QUASI_IDENTIFIER_COLUMNS) <= set(FIELDNAMES))

    def test_ibans_are_mod97_valid(self):
        df = generate_bank_transactions(n_rows=500, seed=42)
        ibans = [v for v in df["iban"] if str(v).strip()]
        self.assertGreater(len(ibans), 0)
        for iban in ibans:
            self.assertEqual(_iban_mod97(str(iban)), 1, f"invalid IBAN {iban}")

    def test_no_empty_sensitive_fields(self):
        df = generate_bank_transactions(n_rows=100, seed=3)
        for col in ["account_holder_name", "account_number", "swift_bic"]:
            self.assertEqual(df[col].isna().sum(), 0)


class BankExperimentsTests(unittest.TestCase):

    def setUp(self):
        self.df = generate_bank_transactions(n_rows=300, seed=42)

    def test_run_all_returns_nine_results(self):
        results = bexp.run_all(self.df)
        self.assertEqual(len(results), 9)

    def test_every_result_has_metrics_and_technique(self):
        for r in bexp.run_all(self.df):
            self.assertIn("title", r)
            self.assertIn("technique", r)
            self.assertIsInstance(r.get("metrics"), dict)
            self.assertNotEqual(r.get("error"), "")

    def test_figures_render_to_png(self):
        for r in bexp.run_all(self.df):
            fig = r.get("fig")
            if fig is not None:
                png = bexp.fig_to_png(fig)
                self.assertTrue(png.startswith(b"\x89PNG"))
                self.assertGreater(len(png), 500)

    def test_tables_are_markdown(self):
        for r in bexp.run_all(self.df):
            md = r.get("table_md") or ""
            if md:
                self.assertIn("|", md)

    def test_reduction_metrics_present_for_mask_scenarios(self):
        df = generate_bank_transactions(n_rows=300, seed=7)
        results = {r["title"]: r for r in bexp.run_all(df)}
        bec = results["1. Targeted BEC / Wire Fraud"]
        self.assertIn("reduction_pct", bec["metrics"])
        self.assertGreater(bec["metrics"]["reduction_pct"], 0)

    def test_sanitize_blurs_cities_to_region(self):
        df = generate_bank_transactions(n_rows=100, seed=5)
        sanitized = bexp._bank_sanitize(df)
        self.assertTrue((sanitized["account_holder_city"] == "REDACTED").sum() == 0)
        cities = set(sanitized["account_holder_city"].dropna())
        self.assertTrue(cities <= {"Europe", "Americas", "Asia",
                                   "Middle East", "Africa", "Oceania",
                                   "Unknown"})


class RunObfuscationEndpointTests(unittest.TestCase):
    """Exercise the real HTTP endpoint with a temp workbench dir."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls._patched = []
        from backend import paths as backend_paths
        from backend import project_runtime, routers, state as backend_state

        # Several backend modules bind PROJECTS_DIR / WORKBENCH_DIR at import
        # time (before this suite may have set FOX_WORKBENCH_DIR), so patch
        # every module-level reference directly to keep tests hermetic.
        tmp = Path(cls._tmp.name)
        for mod in (backend_paths, project_runtime, routers.projects):
            for attr in ("WORKBENCH_DIR", "PROJECTS_DIR"):
                if not hasattr(mod, attr):
                    continue
                cls._patched.append((mod, attr, getattr(mod, attr)))
                setattr(mod, attr, (tmp if attr == "WORKBENCH_DIR"
                                    else tmp / "projects"))
        backend_state.runtimes.clear()
        from backend.main import app

        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        from backend import state as backend_state

        for mod, attr, old in cls._patched:
            setattr(mod, attr, old)
        backend_state.runtimes.clear()
        cls._tmp.cleanup()

    def test_run_obfuscation_records_runs_and_artifacts(self):
        name = "obf-test"
        r = self.client.post("/api/projects", json={"name": name,
                                                    "description": "obf"})
        self.assertEqual(r.status_code, 200)
        resp = self.client.post(
            f"/api/projects/{name}/experiments/run-obfuscation",
            json={"dataset": "bank", "n_rows": 200, "seed": 42})
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()
        self.assertEqual(data["count"], 9)
        self.assertEqual(data["experiment"]["name"], "obfuscation (bank)")
        # each run has metrics + at least one artifact
        for run in data["runs"]:
            self.assertTrue(run["metrics"])
            self.assertTrue(run["artifact_ids"])

        # the experiment shows up in the app's list with runs attached
        exps = self.client.get(f"/api/projects/{name}/experiments").json()["experiments"]
        mine = [e for e in exps if e["name"] == "obfuscation (bank)"]
        self.assertEqual(len(mine), 1)
        self.assertEqual(mine[0]["runs"], 9)

    def test_run_obfuscation_reuses_experiment(self):
        name = "obf-reuse"
        self.client.post("/api/projects", json={"name": name})
        for _ in range(2):
            r = self.client.post(
                f"/api/projects/{name}/experiments/run-obfuscation",
                json={"n_rows": 60, "seed": 42})
            self.assertEqual(r.status_code, 200, r.text)
        exps = self.client.get(
            f"/api/projects/{name}/experiments").json()["experiments"]
        mine = [e for e in exps if e["name"] == "obfuscation (bank)"]
        self.assertEqual(len(mine), 1)
        self.assertEqual(mine[0]["runs"], 18)


if __name__ == "__main__":
    unittest.main()
