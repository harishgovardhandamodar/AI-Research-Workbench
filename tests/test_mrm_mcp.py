"""MRM Simulation MCP server tests.

Covers the full Model Risk Management framework for banking simulations:
inventory + tiering with maker-checker RBAC, deterministic synthetic generators,
Monte Carlo / scenario / stress simulation, fidelity gates, mandatory TSTR,
drift + challenger, audit-ready reporting, banking-domain profiles, the
append-only audit log, MCP-host registration and an end-to-end Tier-1
credit-risk workflow.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from mcp_servers.mrm import core, server

ROLES = ("developer", "validator", "auditor", "admin")


class _MRMBase(unittest.TestCase):
    """Isolate the SQLite store per test via FOX_MRM_STORE."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._prev = os.environ.get(core.MRM_STORE_ENV)
        os.environ[core.MRM_STORE_ENV] = str(self.tmp / "mrm")
        core.close_conn()

    def tearDown(self):
        core.close_conn()
        if self._prev is None:
            os.environ.pop(core.MRM_STORE_ENV, None)
        else:
            os.environ[core.MRM_STORE_ENV] = self._prev

    def load(self, out: str) -> dict:
        return json.loads(out)

    def register(self, **kw) -> dict:
        base = dict(name="PD Model v1", category="credit_risk", synthetic_used=True)
        base.update(kw)
        return self.load(server.register_model(**base))

    def make_portfolio(self, seed=7, n=500) -> str:
        g = self.load(server.generate_synthetic_portfolio(n_loans=n, seed=seed))
        return g["output_file"]

    def make_real(self, portfolio_path: str, seed=8) -> str:
        import pandas as pd
        real = portfolio_path.replace(f"seed{7}", f"seed{seed}")
        pd.read_csv(portfolio_path).to_csv(real, index=False)
        return real


class TestInventory(_MRMBase):
    def test_register_and_list(self):
        m = self.register()
        self.assertEqual(m["status"], "registered")
        mid = m["model_id"]
        self.assertTrue(mid.startswith("mdl_"))
        listed = self.load(server.list_models(category="credit_risk"))["models"]
        self.assertEqual([x["id"] for x in listed], [mid])
        self.assertEqual(listed[0]["tier"], 3)
        meta = self.load(server.get_model_metadata(mid))
        self.assertEqual(meta["name"], "PD Model v1")
        self.assertEqual(meta["synthetic_used"], 1)

    def test_invalid_status_and_tier_rejected(self):
        with self.assertRaises(ValueError):
            server.register_model("x", "credit_risk", status="bogus")
        with self.assertRaises(ValueError):
            server.register_model("x", "credit_risk", tier=9)
        with self.assertRaises(ValueError):
            server.get_model_metadata("ghost")

    def test_simulation_and_dataset_registration(self):
        m = self.register()
        s = self.load(server.register_simulation(
            model_id=m["model_id"], name="sim-1", generator="loan_portfolio",
            seed=1, parameters={"n": 100}, status="registered"))["simulation"]
        sims = self.load(server.list_simulations(m["model_id"]))["simulations"]
        self.assertEqual([x["id"] for x in sims], [s["id"]])
        d = self.load(server.register_dataset(
            "real-loans", path="/x.csv", kind="real", rows=10))["dataset"]
        self.assertEqual(d["kind"], "real")
        self.assertEqual(len(self.load(server.list_datasets("real"))["datasets"]), 1)
        with self.assertRaises(ValueError):
            server.register_dataset("bad", kind="nope")


class TestTieringMakerChecker(_MRMBase):
    def test_developer_cannot_tier_without_approval(self):
        m = self.register()
        with self.assertRaises(PermissionError):
            server.tier_model(m["model_id"], 1, role="developer")

    def test_validator_can_tier_directly(self):
        m = self.register()
        out = self.load(server.tier_model(m["model_id"], 2, role="validator"))
        self.assertEqual(out["tier"], 2)

    def test_approval_flow_allows_developer_tier(self):
        m = self.register()
        apv = self.load(server.request_approval(
            m["model_id"], "tier", "elevate for CECL",
            requested_by="dev-agent", requested_role="developer"))
        aid = apv["approval"]["id"]
        decided = self.load(server.decide_approval(aid, "approve",
                                                   decided_role="validator"))
        self.assertEqual(decided["approval"]["status"], "approved")
        out = self.load(server.tier_model(m["model_id"], 1, role="developer"))
        self.assertEqual(out["tier"], 1)
        self.assertIn("Tier 1", out["tier_description"])

    def test_only_checker_roles_may_decide(self):
        m = self.register()
        apv = self.load(server.request_approval(m["model_id"], "status",
                                                "deploy"))["approval"]
        with self.assertRaises(PermissionError):
            server.decide_approval(apv["id"], "approve", decided_role="developer")
        # double-decide rejected
        server.decide_approval(apv["id"], "reject", decided_role="validator")
        with self.assertRaises(ValueError):
            server.decide_approval(apv["id"], "approve", decided_role="validator")

    def test_status_and_retire_are_gated(self):
        m = self.register()
        with self.assertRaises(PermissionError):
            server.update_model_status(m["model_id"], "approved",
                                       role="developer")
        with self.assertRaises(PermissionError):
            server.retire_model(m["model_id"], role="developer")
        # validator can advance lifecycle
        out = self.load(server.update_model_status(
            m["model_id"], "approved", role="validator"))
        self.assertEqual(out["model_status"], "approved")
        out = self.load(server.retire_model(m["model_id"], role="validator"))
        self.assertEqual(out["status"], "retired")
        self.assertEqual(self.load(server.get_model_metadata(
            m["model_id"]))["status"], "retired")

    def test_pending_approvals_listed(self):
        m = self.register()
        self.load(server.request_approval(m["model_id"], "tier", "x"))
        pending = self.load(server.pending_approvals())
        self.assertEqual(pending["count"], 1)
        self.assertEqual(pending["pending"][0]["action"], "tier")


class TestGenerators(_MRMBase):
    def test_loan_portfolio_schema_and_determinism(self):
        g = self.load(server.generate_synthetic_portfolio(n_loans=200, seed=7))
        path = Path(g["output_file"])
        self.assertTrue(path.exists())
        import pandas as pd
        df = pd.read_csv(path)
        self.assertEqual(len(df), 200)
        for col in ("borrower_id", "credit_score", "pd", "lgd", "ead",
                    "rating", "default", "industry"):
            self.assertIn(col, df.columns)
        self.assertTrue(df["pd"].between(0, 1).all())
        self.assertTrue(df["lgd"].between(0, 1).all())
        self.assertTrue(df["default"].isin([0, 1]).all())
        self.assertTrue((df["ead"] > 0).all())
        # deterministic: same seed -> identical bytes
        g2 = self.load(server.generate_synthetic_portfolio(n_loans=200, seed=7))
        self.assertEqual(Path(g2["output_file"]).read_bytes(),
                         path.read_bytes())
        self.assertIn("dataset_id", g)
        self.assertIn("simulation_id", g)

    def test_generator_assumptions_documented(self):
        out = self.load(server.extract_generator_assumptions(
            "loan_portfolio", 42, {"correlation": 0.12}))
        aspects = [a["aspect"] for a in out["assumptions"]]
        self.assertIn("correlation_structure", aspects)
        self.assertIn("tail_behavior", aspects)
        self.assertIn("bias_sources", aspects)
        with self.assertRaises(ValueError):
            server.extract_generator_assumptions("nope", 1)

    def test_transaction_stream(self):
        g = self.load(server.generate_transaction_stream(n_tx=2000, seed=3,
                                                         fraud_rate=0.01))
        import pandas as pd
        df = pd.read_csv(g["output_file"])
        self.assertEqual(len(df), 2000)
        for col in ("customer_id", "amount", "fraud_flag", "channel",
                    "risk_score", "merchant_category"):
            self.assertIn(col, df.columns)
        self.assertGreaterEqual(df["fraud_flag"].sum(), 10)
        self.assertGreater(df["amount"].min(), 0)

    def test_privacy_budget(self):
        out = self.load(server.apply_privacy_budget("syn-loans", 0.5, rows=100))
        self.assertEqual(out["dataset"]["privacy_epsilon"], 0.5)
        with self.assertRaises(ValueError):
            server.apply_privacy_budget("bad", 0.0)


class TestSimulation(_MRMBase):
    def test_monte_carlo_deterministic_and_bounds(self):
        pf = self.make_portfolio(seed=7, n=300)
        a = self.load(server.run_monte_carlo(pf, n_paths=400, seed=1))
        b = self.load(server.run_monte_carlo(pf, n_paths=400, seed=1))
        self.assertEqual(a["expected_loss"], b["expected_loss"])
        self.assertEqual(a["var_99"], b["var_99"])
        self.assertGreater(a["var_99"], a["expected_loss"])
        self.assertGreaterEqual(a["es_97_5"], a["expected_loss"])
        self.assertLessEqual(a["mean_default_rate"], 1.0)
        self.assertEqual(len(a["loss_histogram"]["bins"]), 30)
        # different seed -> different distribution
        c = self.load(server.run_monte_carlo(pf, n_paths=400, seed=2))
        self.assertNotEqual(a["expected_loss"], c["expected_loss"])

    def test_stress_increases_loss(self):
        pf = self.make_portfolio(seed=7, n=300)
        out = self.load(server.stress_test_portfolio(pf, severity=3.0,
                                                     n_paths=300))
        self.assertGreater(out["stressed"]["expected_loss"],
                           out["baseline"]["expected_loss"])
        self.assertGreater(out["stressed"]["var_99"], out["baseline"]["var_99"])
        self.assertIn("read_out", out["impact"])

    def test_scenario_ordering(self):
        pf = self.make_portfolio(seed=7, n=300)
        out = self.load(server.run_scenario_set(
            pf, scenarios=["upside", "baseline", "severe_recession"],
            n_paths=300))
        self.assertEqual(out["ordering"], ["upside", "baseline",
                                           "severe_recession"])
        with self.assertRaises(ValueError):
            server.run_scenario_set(pf, scenarios=["nope"])

    def test_sensitivity_monotonic(self):
        pf = self.make_portfolio(seed=7, n=200)
        out = self.load(server.sensitivity_analysis(
            pf, values=[0.5, 1.0, 2.0], n_paths=300))
        self.assertTrue(out["monotonic_in_pd"])
        losses = [p["expected_loss"] for p in out["points"]]
        self.assertTrue(losses == sorted(losses))

    def test_compare_versions(self):
        pf = self.make_portfolio(seed=7, n=300)
        base = self.load(server.run_monte_carlo(pf, n_paths=300, seed=1))
        stress = self.load(server.run_monte_carlo(pf, n_paths=300, seed=2,
                                                  pd_mult=3.0))
        out = self.load(server.compare_simulation_versions(base, stress))
        self.assertTrue(out["material_difference"])
        self.assertGreater(out["deltas"]["expected_loss"], 0)

    def test_bad_portfolio_path(self):
        with self.assertRaises(ValueError):
            server.run_monte_carlo("/does/not/exist.csv")


class TestValidation(_MRMBase):
    def test_fidelity_passes_on_consistent_data(self):
        pf = self.make_portfolio(seed=7, n=400)
        real = self.make_real(pf, seed=7)  # identical copy
        out = self.load(server.evaluate_fidelity(real, pf))
        self.assertEqual(out["verdict"], "PASS")

    def test_fidelity_fails_business_rule(self):
        pf = self.make_portfolio(seed=7, n=400)
        import pandas as pd
        df = pd.read_csv(pf)
        df.loc[0, "pd"] = -0.5  # violate pd in [0,1]
        bad = pf.replace("seed7", "seedBAD")
        df.to_csv(bad, index=False)
        out = self.load(server.evaluate_fidelity(pf, bad))
        self.assertEqual(out["verdict"], "FAIL")
        self.assertIn("pd in [0,1]", out["remediation"][0])

    def test_tstr_protocol(self):
        syn = self.make_portfolio(seed=7, n=400)
        real = self.make_portfolio(seed=8, n=400)
        m = self.register()
        out = self.load(server.tstr_evaluate(syn, real, "default",
                                             model_id=m["model_id"]))
        self.assertEqual(out["protocol"], "TSTR")
        self.assertEqual(out["status"], "compliant")
        self.assertGreater(out["metrics"]["roc_auc"], 0.5)
        self.assertEqual(set(out["metrics"]["confusion"]),
                         {"tp", "fp", "fn", "tn"})
        # model flag + report persisted
        meta = self.load(server.get_model_metadata(m["model_id"]))
        self.assertEqual(meta["tstr_completed"], 1)
        reports = self.load(server.list_validation_reports(m["model_id"]))
        self.assertEqual(len(reports["validation_reports"]), 1)

    def test_performance_metrics_requires_both_classes(self):
        with self.assertRaises(ValueError):
            server.compute_performance_metrics([0, 0, 0], [0.2, 0.3, 0.4])
        out = self.load(server.compute_performance_metrics(
            [0, 0, 1, 1, 1], [0.1, 0.2, 0.6, 0.7, 0.9]))
        self.assertGreater(out["roc_auc"], 0.8)

    def test_drift_detection(self):
        ref = self.make_portfolio(seed=7, n=400)
        import pandas as pd
        df = pd.read_csv(ref)
        df["pd"] = df["pd"] * 3.0  # clear population shift
        cur = ref.replace("seed7", "seedShift")
        df.to_csv(cur, index=False)
        out = self.load(server.detect_drift(ref, cur))
        self.assertEqual(out["verdict"], "DRIFT DETECTED")
        self.assertIn("pd", out["shifted_columns"])

    def test_challenger(self):
        pf = self.make_portfolio(seed=7, n=400)
        out = self.load(server.run_challenger(pf, "default",
                                              baseline="logistic",
                                              challenger="decision_tree",
                                              seed=1))
        self.assertIn("baseline_metrics", out)
        self.assertIn("challenger_metrics", out)
        self.assertIn(out["challenger_wins"], (True, False))
        with self.assertRaises(ValueError):
            server.run_challenger(pf, "default", challenger="nope")


class TestGovernanceAndAudit(_MRMBase):
    def test_effective_challenge_flags_report(self):
        m = self.register()
        pf = self.make_portfolio(seed=7, n=300)
        real = self.make_real(pf)
        self.load(server.tstr_evaluate(pf, real, "default",
                                       model_id=m["model_id"]))
        self.load(server.log_effective_challenge(
            m["model_id"], "tail risk underestimated", severity="high",
            disposition="open"))
        challenges = self.load(server.list_challenges(m["model_id"]))["challenges"]
        self.assertEqual(len(challenges), 1)
        reports = self.load(server.list_validation_reports(m["model_id"]))
        self.assertEqual(reports["validation_reports"][0]["status"], "challenged")

    def test_validation_report_and_evidence(self):
        m = self.register()
        out = self.load(server.generate_validation_report(
            m["model_id"], profile="credit_risk",
            validation_data={"var_99": 1e6}))
        self.assertEqual(out["status"], "success")
        self.assertTrue(Path(out["report_path"]).exists())
        self.assertIn("## 1. Intended use", out["report_markdown"])
        self.assertIn("credit_risk", out["report_markdown"])
        ev = self.load(server.list_evidence(m["model_id"]))["evidence"]
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["kind"], "report")

    def test_audit_log_records_calls_and_is_role_gated(self):
        mid = self.register()["model_id"]
        pf = self.make_portfolio(seed=7, n=100)
        self.load(server.run_monte_carlo(pf, n_paths=200))
        log = self.load(server.audit_log(limit=20, role="validator"))
        tools = {e["tool"] for e in log["events"]}
        self.assertIn("register_model", tools)
        self.assertIn("generate_synthetic_portfolio", tools)
        self.assertIn("run_monte_carlo", tools)
        self.assertTrue(all(e["params_hash"] for e in log["events"]))
        with self.assertRaises(PermissionError):
            server.audit_log(role="developer")
        # model-filtered trail picks up events attributed to the model
        model_trail = self.load(server.audit_log(
            limit=20, model_id=mid, role="auditor"))["events"]
        self.assertTrue(all(e["model_id"] == mid for e in model_trail))
        self.assertIn("register_model", {e["tool"] for e in model_trail})
        self.assertEqual(len(self.load(server.audit_log(
            tool="register_model", role="validator"))["events"]), 1)

    def test_cross_file_consistency(self):
        a = self.make_portfolio(seed=7, n=200)
        b = a.replace("seed7", "seed7b")
        c = a.replace("seed7", "seed7c")
        for p, seed in ((b, 7), (c, 8)):
            import pandas as pd
            pd.read_csv(a).to_csv(p, index=False)
        out = self.load(server.check_cross_file_consistency([a, b, c]))
        self.assertIn(out["verdict"], ("PASS", "REVIEW"))
        self.assertEqual(len(out["checks"]), 2)


class TestProfiles(_MRMBase):
    def test_banking_profiles(self):
        out = self.load(server.list_profiles())["profiles"]
        cats = {p["category"] for p in out}
        self.assertTrue({"credit_risk", "market_risk", "stress_testing",
                         "cecl", "fraud_aml", "pricing"} <= cats)
        cr = self.load(server.get_profile("credit_risk"))
        self.assertEqual(cr["tier_default"], 1)
        self.assertIn("tstr", cr["validation"])
        self.assertIn("SR 11-7", cr["doc_refs"])
        with self.assertRaises(ValueError):
            server.get_profile("nope")

    def test_tool_read_only_hints(self):
        """Writable tools are approval-gated by the host; simulation/analysis
        tools run read-only."""
        ro = server.mcp._tool_manager.get_tool("run_monte_carlo")
        self.assertTrue(ro.annotations.read_only_hint)
        wr = server.mcp._tool_manager.get_tool("register_model")
        self.assertFalse(getattr(wr.annotations, "read_only_hint", False))
        self.assertGreater(server._TOOL_COUNT, 20)


class TestHostRegistration(unittest.TestCase):
    def test_mrm_in_default_servers(self):
        from backend.mcp import DEFAULT_SERVERS
        entry = next((s for s in DEFAULT_SERVERS if s["name"] == "mrm"), None)
        self.assertIsNotNone(entry)
        self.assertEqual(entry["transport"], "stdio")
        self.assertEqual(entry["args"], ["-m", "mcp_servers.mrm.server"])

    def test_state_merges_mrm_server(self):
        from backend.state import load_config
        cfg = load_config()
        names = [s.get("name") for s in cfg["mcp"]["servers"]]
        self.assertIn("mrm", names)


class TestTier1EndToEndWorkflow(_MRMBase):
    """The full acceptance scenario: an external agent drives the whole MRM
    lifecycle purely through tools."""

    def test_full_workflow(self):
        # 1. inventory the model (maker, 1st line)
        m = self.register(description="Tier-1 credit PD model")
        mid = m["model_id"]
        # 2. maker-checker to reach Tier 1
        apv = self.load(server.request_approval(
            mid, "tier", "material exposure + synthetic reliance",
            requested_role="developer"))["approval"]
        self.load(server.decide_approval(apv["id"], "approve",
                                         decided_role="validator"))
        self.load(server.tier_model(mid, 1, role="developer"))
        self.assertEqual(self.load(server.get_model_metadata(mid))["tier"], 1)
        # 3. controlled synthetic generation (registers dataset + simulation)
        syn = self.make_portfolio(seed=42, n=500)
        real = self.make_portfolio(seed=43, n=500)
        # 4. simulation + stress
        mc = self.load(server.run_monte_carlo(syn, n_paths=400, seed=1))
        self.assertGreater(mc["var_99"], 0)
        stress = self.load(server.stress_test_portfolio(syn, n_paths=300))
        self.assertIn("read_out", stress["impact"])
        # 5. fidelity + mandatory TSTR on hold-out real data
        fid = self.load(server.evaluate_fidelity(real, syn))
        self.assertIn(fid["verdict"], ("PASS", "FAIL"))
        tstr = self.load(server.tstr_evaluate(syn, real, "default",
                                              model_id=mid))
        self.assertEqual(tstr["protocol"], "TSTR")
        # 6. challenger + drift monitoring
        self.load(server.run_challenger(syn, "default", seed=1))
        self.load(server.detect_drift(real, syn))
        # 7. effective challenge + audit report + evidence
        self.load(server.log_effective_challenge(
            mid, "LGD sensitivity on distressed loans", severity="medium"))
        report = self.load(server.generate_validation_report(
            mid, profile="credit_risk", validation_data={"mc": mc}))
        self.assertTrue(Path(report["report_path"]).exists())
        ev = self.load(server.list_evidence(mid))["evidence"]
        self.assertEqual(len(ev), 1)
        # 8. full audit trail, every step recorded
        log = self.load(server.audit_log(limit=100, role="auditor"))["events"]
        tools = {e["tool"] for e in log}
        for expected in ("register_model", "request_approval",
                         "decide_approval", "tier_model",
                         "generate_synthetic_portfolio", "run_monte_carlo",
                         "stress_test_portfolio", "evaluate_fidelity",
                         "tstr_evaluate", "run_challenger", "detect_drift",
                         "log_effective_challenge", "generate_validation_report"):
            self.assertIn(expected, tools)
        # and the model-attributed trail covers the model-level steps
        trail = self.load(server.audit_log(
            limit=100, role="auditor", model_id=mid))["events"]
        self.assertEqual({e["tool"] for e in trail},
                         {"register_model", "request_approval",
                          "decide_approval", "tier_model", "tstr_evaluate",
                          "log_effective_challenge",
                          "generate_validation_report"})


if __name__ == "__main__":
    unittest.main()
