"""MRM Simulation MCP server — Model Risk Management for banking simulations.

Implements the SR 11-7 / 2026-interagency-aligned MRM framework as an MCP
server: governed inventory (models / simulations / datasets), controlled
synthetic-data generation with fidelity + privacy gates, Monte Carlo / scenario
/ stress simulation, mandatory TSTR validation, drift monitoring, challenger
models, maker-checker approvals, effective-challenge logging and an append-only
audit log for every tool invocation.

Any MCP host (Fox chat, Claude, Cursor, LangChain, ...) can connect over stdio
and call tools as ``mrm__<tool>``. Writable tools (register / generate / TSTR /
governance) are approval-gated by the host; pure simulation and analysis tools
run read-only.

Run standalone (stdio):

    .venv/bin/python -m mcp_servers.mrm.server
"""

from __future__ import annotations

import functools
import inspect
import json
from pathlib import Path

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from . import __version__
from . import core, generators, governance, profiles, simulators, validation

mcp = MCPServer(
    "mrm-simulation-mcp",
    version=__version__,
    instructions=(
        "Model Risk Management (MRM) for banking data simulations. Workflow: "
        "1) list_profiles + register_model to inventory a model; 2) for Tier-1 "
        "changes, request_approval then have a validator decide_approval "
        "(maker-checker); 3) generate_synthetic_portfolio; 4) run_monte_carlo / "
        "run_scenario_set / stress_test_portfolio; 5) evaluate_fidelity + "
        "tstr_evaluate against hold-out REAL data; 6) detect_drift + "
        "run_challenger; 7) generate_validation_report for audit evidence. "
        "Every call is written to the append-only audit log. Writable tools are "
        "approval-gated by the host."
    ),
)

RO = ToolAnnotations(read_only_hint=True)


def _json(data) -> str:
    return json.dumps(data, indent=2, default=str)


def _summary(result: str) -> str:
    """Short scalar summary of a JSON tool result for the audit log."""
    try:
        data = json.loads(result)
        if isinstance(data, dict):
            keys = ("status", "verdict", "model_id", "dataset_id",
                    "simulation_id", "evidence_id", "report_path",
                    "expected_loss", "var_99", "roc_auc", "pr_auc", "auc",
                    "protocol", "tool", "error")
            parts = [f"{k}={data[k]}" for k in keys if k in data]
            if parts:
                return ", ".join(str(p) for p in parts)[:200]
    except Exception:  # noqa: BLE001
        pass
    return str(result)[:200]


def _result_model_id(result: str) -> str:
    """Best-effort model attribution from a JSON tool result, so results that
    carry or create a model (register/decide/approve) land on its audit trail."""
    try:
        data = json.loads(result)
        if isinstance(data, dict):
            if data.get("model_id"):
                return str(data["model_id"])
            if isinstance(data.get("approval"), dict) and data["approval"].get("model_id"):
                return str(data["approval"]["model_id"])
            if isinstance(data.get("model"), dict) and data["model"].get("id"):
                return str(data["model"]["id"])
    except Exception:  # noqa: BLE001
        pass
    return ""


def _audited(fn):
    """Record an append-only audit event around every tool invocation."""
    sig = inspect.signature(fn)
    defaults = {p.name: p.default for p in sig.parameters.values()
                if p.default is not inspect._empty}

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        tool = fn.__name__
        actor = kwargs.get("actor", defaults.get("actor", "agent"))
        role = kwargs.get("role", defaults.get("role", "developer"))
        purpose = kwargs.get("purpose", defaults.get("purpose", ""))
        model_id = kwargs.get("model_id", "")
        try:
            result = fn(*args, **kwargs)
            rm = _result_model_id(result)
            core.audit(actor, role, purpose, tool, core._hash(kwargs),
                       _summary(result), "ok", rm or model_id)
            return result
        except Exception as e:  # noqa: BLE001
            core.audit(actor, role, purpose, tool, core._hash(kwargs),
                       f"{type(e).__name__}: {e}", "error", model_id)
            raise

    return wrapper


# ------------------------------------------------------------- inventory tools --

@mcp.tool(annotations=RO)
@_audited
def health() -> str:
    """Liveness + tool-count probe for the MRM server."""
    return _json({"status": "ok", "server": "mrm-simulation-mcp",
                  "version": __version__, "tools": _TOOL_COUNT})


@mcp.tool(annotations=RO)
@_audited
def list_profiles() -> str:
    """List the banking-domain MRM profiles (credit risk, market risk, CECL,
    stress testing, fraud/AML, pricing)."""
    return _json({"profiles": profiles.list_profiles()})


@mcp.tool(annotations=RO)
@_audited
def get_profile(category: str) -> str:
    """Get a banking-domain profile's generators, validation suite, metrics and
    SR 11-7 pillars by category."""
    return _json(profiles.get_profile(category))


@mcp.tool(annotations=RO)
@_audited
def list_models(status: str = "", tier: int | None = None,
                category: str = "") -> str:
    """List the authoritative model inventory, optionally filtered by lifecycle
    status, risk tier (1/2/3) or category."""
    return _json({"models": core.list_models(status, tier, category)})


@mcp.tool(annotations=RO)
@_audited
def get_model_metadata(model_id: str) -> str:
    """Fetch full metadata (tier, status, assumptions, TSTR flag) for one model."""
    m = core.require_model(model_id)
    return _json(m)


@mcp.tool()
@_audited
def register_model(name: str, category: str, tier: int = 3,
                   status: str = "development", description: str = "",
                   owner: str = "1st-line", validator: str = "",
                   synthetic_used: bool = False, use_limitations: str = "",
                   assumptions: list | None = None,
                   actor: str = "agent", role: str = "developer",
                   purpose: str = "") -> str:
    """Register a model in the MRM inventory (Pillar 3). First step of any
    workflow — the model must exist before it can be tiered, simulated or
    validated."""
    m = core.register_model(name, category, tier, status, description, owner,
                            validator, synthetic_used, use_limitations,
                            assumptions)
    return _json({"status": "registered", "model_id": m["id"],
                  "model": m, "note": "register_simulation to attach sims; "
                                      "tier_model to re-tier."})


@mcp.tool(annotations=RO)
@_audited
def list_simulations(model_id: str = "") -> str:
    """List registered simulation configurations (optionally for one model)."""
    return _json({"simulations": core.list_simulations(model_id)})


@mcp.tool()
@_audited
def register_simulation(model_id: str = "", name: str = "",
                        generator: str = "", dataset_id: str = "",
                        seed: int | None = None, version: str = "1.0",
                        parameters: dict | None = None, status: str = "proposed",
                        actor: str = "agent", role: str = "developer",
                        purpose: str = "") -> str:
    """Register a simulation configuration against a model (versioned, seeded,
    parameterised) so simulation runs have lineage."""
    s = core.register_simulation(model_id, name, generator, dataset_id, seed,
                                 version, parameters, status, created_by=actor)
    return _json({"status": "registered", "simulation": s})


@mcp.tool(annotations=RO)
@_audited
def list_datasets(kind: str = "") -> str:
    """List registered datasets (real or synthetic) with privacy budget."""
    return _json({"datasets": core.list_datasets(kind)})


@mcp.tool()
@_audited
def register_dataset(name: str, path: str = "", kind: str = "synthetic",
                     source: str = "", privacy_epsilon: float | None = None,
                     rows: int = 0, actor: str = "agent",
                     role: str = "developer", purpose: str = "") -> str:
    """Register a real or synthetic dataset with lineage + privacy metadata."""
    d = core.register_dataset(name, path, kind, source, privacy_epsilon, rows)
    return _json({"status": "registered", "dataset": d})


# ------------------------------------------------------- synthetic data tools --

def _store_dir() -> Path:
    return core.store_root() / "datasets"


@mcp.tool()
@_audited
def generate_synthetic_portfolio(n_loans: int = 5000, seed: int = 42,
                                 correlation: float = 0.12, pd_mult: float = 1.0,
                                 output_dir: str = "", actor: str = "agent",
                                 role: str = "developer", purpose: str = "") -> str:
    """Generate a deterministic PD-LGD loan portfolio (one-factor Vasicek model)
    as a versioned CSV. Registers the dataset + simulation for lineage."""
    df = generators.generate_loan_portfolio(n_loans, seed, correlation, pd_mult)
    out = Path(output_dir) if output_dir else _store_dir()
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"loan_portfolio_n{n_loans}_seed{seed}.csv"
    df.to_csv(path, index=False)
    ds = core.register_dataset(name=path.stem, path=str(path),
                               kind="synthetic", source="mrm:loan_portfolio",
                               rows=len(df))
    sim = core.register_simulation(
        name=f"loan portfolio gen (seed {seed})", generator="loan_portfolio",
        dataset_id=ds["id"], seed=seed,
        parameters={"n_loans": n_loans, "correlation": correlation,
                    "pd_mult": pd_mult},
        status="registered", created_by=actor)
    return _json({
        "status": "success",
        "output_file": str(path.resolve()),
        "rows": int(len(df)),
        "columns": list(df.columns),
        "dataset_id": ds["id"],
        "simulation_id": sim["id"],
        "assumptions": generators.extract_generator_assumptions(
            "loan_portfolio", seed,
            {"correlation": correlation, "pd_mult": pd_mult}),
    })


@mcp.tool()
@_audited
def generate_transaction_stream(n_tx: int = 10000, seed: int = 42,
                                n_customers: int = 500, fraud_rate: float = 0.008,
                                output_dir: str = "", actor: str = "agent",
                                role: str = "developer", purpose: str = "") -> str:
    """Generate a deterministic transaction stream with fraud injection as a
    versioned CSV. Registers the dataset + simulation for lineage."""
    df = generators.generate_transaction_stream(n_tx, seed, n_customers,
                                                fraud_rate)
    out = Path(output_dir) if output_dir else _store_dir()
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"transaction_stream_n{n_tx}_seed{seed}.csv"
    df.to_csv(path, index=False)
    ds = core.register_dataset(name=path.stem, path=str(path),
                               kind="synthetic", source="mrm:transaction_stream",
                               rows=len(df))
    sim = core.register_simulation(
        name=f"tx stream gen (seed {seed})", generator="transaction_stream",
        dataset_id=ds["id"], seed=seed,
        parameters={"n_tx": n_tx, "n_customers": n_customers,
                    "fraud_rate": fraud_rate},
        status="registered", created_by=actor)
    return _json({
        "status": "success",
        "output_file": str(path.resolve()),
        "rows": int(len(df)),
        "columns": list(df.columns),
        "dataset_id": ds["id"],
        "simulation_id": sim["id"],
        "assumptions": generators.extract_generator_assumptions(
            "transaction_stream", seed, {"fraud_rate": fraud_rate}),
    })


@mcp.tool()
@_audited
def apply_privacy_budget(dataset_name: str, epsilon: float,
                         source: str = "differential_privacy",
                         path: str = "", rows: int = 0,
                         actor: str = "agent", role: str = "developer",
                         purpose: str = "") -> str:
    """Register a synthetic dataset with its differential-privacy epsilon budget
    (privacy guarantee + composition tracking for the generation pipeline)."""
    if epsilon <= 0:
        raise ValueError("epsilon must be > 0")
    d = core.register_dataset(name=dataset_name, path=path, kind="synthetic",
                              source=source, privacy_epsilon=float(epsilon),
                              rows=rows)
    return _json({
        "status": "registered",
        "dataset": d,
        "privacy": generators.evaluate_generator_privacy(rows, epsilon, source),
    })


@mcp.tool(annotations=RO)
@_audited
def evaluate_fidelity(real_path: str, synthetic_path: str) -> str:
    """Fidelity gates (distributional match, correlation distance, business-rule
    coverage) between real reference and synthetic data — PASS/FAIL before any
    material use."""
    return _json(validation.evaluate_fidelity(real_path, synthetic_path))


@mcp.tool(annotations=RO)
@_audited
def extract_generator_assumptions(generator: str, seed: int,
                                  parameters: dict | None = None) -> str:
    """Documented conceptual-soundness assumptions of a data generator
    (statistical properties, correlation, tail behaviour, bias sources)."""
    return _json({"generator": generator, "seed": seed,
                  "assumptions": generators.extract_generator_assumptions(
                      generator, seed, parameters)})


# ---------------------------------------------------------- simulation tools --

@mcp.tool(annotations=RO)
@_audited
def run_monte_carlo(portfolio_path: str, n_paths: int = 5000, seed: int = 42,
                    correlation: float = 0.12, pd_mult: float = 1.0,
                    horizon: float = 1.0) -> str:
    """One-factor Vasicek Monte Carlo loss simulation: expected loss, VaR(99),
    ES(97.5), default rate + loss histogram. Deterministic under a seed."""
    return _json(simulators.monte_carlo_loss(portfolio_path, n_paths, seed,
                                             correlation, pd_mult, horizon))


@mcp.tool(annotations=RO)
@_audited
def run_scenario_set(portfolio_path: str, scenarios: list[str] | None = None,
                     n_paths: int = 3000, seed: int = 42) -> str:
    """Run the named macro scenario suite (baseline / mild_recession /
    severe_recession / systemic_stress / upside) over a portfolio."""
    return _json(simulators.run_scenario_set(portfolio_path, scenarios,
                                             n_paths, seed))


@mcp.tool(annotations=RO)
@_audited
def stress_test_portfolio(portfolio_path: str, severity: float = 3.2,
                          n_paths: int = 3000, seed: int = 42) -> str:
    """Stress the portfolio at a PD-multiplier severity vs baseline; reports
    VaR deltas and a board-ready read-out."""
    return _json(simulators.stress_test_portfolio(portfolio_path, severity,
                                                  n_paths, seed))


@mcp.tool(annotations=RO)
@_audited
def sensitivity_analysis(portfolio_path: str, parameter: str = "pd_mult",
                         values: list[float] | None = None,
                         n_paths: int = 2000, seed: int = 42) -> str:
    """One-at-a-time sensitivity of portfolio loss to a parameter (pd_mult)."""
    return _json(simulators.sensitivity_analysis(portfolio_path, parameter,
                                                 values, n_paths, seed))


@mcp.tool(annotations=RO)
@_audited
def compare_simulation_versions(version_a: dict, version_b: dict) -> str:
    """Delta between two simulation results (engine v1 vs v2) — flags material
    differences that require review before re-baselining."""
    return _json(simulators.compare_simulation_versions(version_a, version_b))


# ---------------------------------------------------------- validation tools --

@mcp.tool()
@_audited
def tstr_evaluate(synthetic_path: str, real_path: str, target: str,
                  seed: int = 42, test_size: float = 0.3, positive: float = 1.0,
                  model_id: str = "", actor: str = "agent",
                  role: str = "validator", purpose: str = "") -> str:
    """Mandatory Train-Synthetic-Test-Real evaluation: fit on synthetic, evaluate
    on hold-out REAL data. When model_id is given the result is persisted as a
    validation report and the model's TSTR flag is set."""
    res = validation.tstr_evaluate(synthetic_path, real_path, target, seed,
                                   test_size, positive)
    if model_id:
        core.require_model(model_id)
        core.update_model(model_id, tstr_completed=True)
        status = ("passed" if (res["metrics"].get("roc_auc") or 0.0) >= 0.6
                  else "challenged")
        core.save_validation_report(model_id, name=f"TSTR ({target})",
                                    metrics=res["metrics"],
                                    findings=[res["statement"]],
                                    status=status)
        res["model_id"] = model_id
    return _json(res)


@mcp.tool(annotations=RO)
@_audited
def compute_performance_metrics(y_true: list | None = None,
                                y_pred_proba: list | None = None,
                                y_true_csv: str = "", y_pred_csv: str = "",
                                positive: float = 1.0) -> str:
    """Binary-classification metrics (AUC, PR-AUC, KS, Brier, precision/recall)
    from ground-truth + probability scores (lists or CSV files)."""
    if y_true is None:
        if not y_true_csv:
            raise ValueError("provide y_true + y_pred_proba lists or CSV files")
        import pandas as pd
        true_ = pd.read_csv(y_true_csv).iloc[:, 0].to_numpy()
        pred_ = pd.read_csv(y_pred_csv).iloc[:, 0].to_numpy()
    else:
        true_ = y_true
        pred_ = y_pred_proba
    return _json(validation.compute_performance_metrics(true_, pred_, positive))


@mcp.tool(annotations=RO)
@_audited
def detect_drift(reference_path: str, current_path: str) -> str:
    """Population-shift detection (PSI on numeric, total variation on
    categorical) between a reference and current dataset."""
    return _json(validation.detect_drift(reference_path, current_path))


@mcp.tool(annotations=RO)
@_audited
def run_challenger(data_path: str, target: str, baseline: str = "logistic",
                   challenger: str = "gaussian_nb", seed: int = 42,
                   min_auc_gain: float = 0.005) -> str:
    """Independent challenger head-to-head: does an alternative model beat the
    baseline by a material AUC margin?"""
    return _json(validation.run_challenger(data_path, target, baseline,
                                           challenger, seed, min_auc_gain))


# ------------------------------------------------- documentation & evidence --

@mcp.tool()
@_audited
def generate_validation_report(model_id: str, profile: str = "",
                               validation_data: dict | None = None,
                               author: str = "validator",
                               actor: str = "agent", role: str = "validator",
                               purpose: str = "") -> str:
    """Compile an audit-ready Markdown validation report (Pillar 3) from the
    inventory, validation results, challenges and approvals. Writes it to the
    store and attaches it as evidence."""
    return _json(governance.generate_validation_report(model_id, profile,
                                                       validation_data, author))


@mcp.tool(annotations=RO)
@_audited
def check_cross_file_consistency(files: list[str]) -> str:
    """Cross-file consistency: schema overlap, shared-numeric KS agreement and
    row-count sanity across a family of datasets (real + synthetic)."""
    return _json(governance.check_cross_file_consistency(files))


@mcp.tool()
@_audited
def attach_evidence(model_id: str, kind: str = "report", description: str = "",
                    path: str = "", actor: str = "agent",
                    role: str = "developer", purpose: str = "") -> str:
    """Attach audit-ready evidence (report / data / notebook) to a model."""
    e = core.attach_evidence(model_id, kind, description, path, actor)
    return _json({"status": "attached", "evidence": e})


# ---------------------------------------------------------------- controls --

@mcp.tool()
@_audited
def request_approval(model_id: str, action: str, rationale: str,
                     requested_by: str = "agent",
                     requested_role: str = "developer",
                     actor: str = "agent", role: str = "developer",
                     purpose: str = "") -> str:
    """MAKER step: raise a human approval request for an approval-gated action
    (tier / status / retire / deploy / use_synthetic)."""
    a = governance.request_approval(model_id, action, rationale, requested_by,
                                    requested_role)
    return _json({"status": "requested", "approval": a,
                  "note": "A validator/admin must decide_approval before the "
                          "action can be applied."})


@mcp.tool()
@_audited
def decide_approval(approval_id: str, decision: str,
                    decided_by: str = "validator",
                    decided_role: str = "validator",
                    actor: str = "agent", role: str = "validator",
                    purpose: str = "") -> str:
    """CHECKER step: a 2nd/3rd-line role approves or rejects a pending approval
    request. Only validator/auditor/admin roles may decide."""
    a = governance.decide_approval(approval_id, decision, decided_by,
                                   decided_role)
    return _json({"status": "decided", "approval": a})


@mcp.tool(annotations=RO)
@_audited
def pending_approvals() -> str:
    """List open maker-checker approval requests."""
    return _json(governance.pending_approvals())


@mcp.tool(annotations=RO)
@_audited
def list_approvals(model_id: str = "", status: str = "") -> str:
    """List approval requests, optionally filtered by model or status."""
    return _json({"approvals": core.list_approvals(model_id, status)})


@mcp.tool()
@_audited
def tier_model(model_id: str, tier: int, rationale: str = "",
               actor: str = "agent", role: str = "developer",
               purpose: str = "") -> str:
    """Change a model's risk tier (1/2/3). Moving to a higher-risk tier is
    approval-gated unless the actor holds a validator/admin role."""
    return _json(governance.tier_model(model_id, tier, rationale, actor, role))


@mcp.tool()
@_audited
def update_model_status(model_id: str, status: str, rationale: str = "",
                        actor: str = "agent", role: str = "developer",
                        purpose: str = "") -> str:
    """Advance a model lifecycle status. approved / monitoring / retired
    transitions are approval-gated for non-validator actors."""
    return _json(governance.update_model_status(model_id, status, rationale,
                                                actor, role))


@mcp.tool()
@_audited
def retire_model(model_id: str, rationale: str = "",
                 actor: str = "agent", role: str = "developer",
                 purpose: str = "") -> str:
    """Retire a model. Approval-gated (request_approval action='retire' + a
    validator decide) unless the actor holds a validator/admin role."""
    return _json(governance.retire_model(model_id, rationale, actor, role))


@mcp.tool()
@_audited
def log_effective_challenge(model_id: str, finding: str,
                            severity: str = "medium", disposition: str = "open",
                            logged_by: str = "validator",
                            actor: str = "agent", role: str = "validator",
                            purpose: str = "") -> str:
    """Record an independent validation finding (effective challenge) against a
    model. High/critical open findings flag the model's report as challenged."""
    return _json(governance.log_effective_challenge(model_id, finding,
                                                    severity, disposition,
                                                    logged_by))


@mcp.tool(annotations=RO)
@_audited
def list_challenges(model_id: str = "") -> str:
    """List effective challenges (validation findings) for a model."""
    return _json({"challenges": core.list_challenges(model_id)})


@mcp.tool(annotations=RO)
@_audited
def list_evidence(model_id: str = "") -> str:
    """List evidence attached to a model (reports, data, notebooks)."""
    return _json({"evidence": core.list_evidence(model_id)})


@mcp.tool(annotations=RO)
@_audited
def list_validation_reports(model_id: str = "") -> str:
    """List validation reports (TSTR, monitoring) for a model."""
    return _json({"validation_reports": core.list_validation_reports(model_id)})


@mcp.tool(annotations=RO)
@_audited
def audit_log(limit: int = 100, model_id: str = "", tool: str = "",
              actor: str = "", role: str = "validator",
              purpose: str = "") -> str:
    """Query the immutable audit trail. Restricted to validator / auditor /
    admin roles (2nd & 3rd line)."""
    if role not in ("validator", "auditor", "admin"):
        raise PermissionError(
            f"audit_log requires a validator/auditor/admin role; got '{role}'")
    return _json({"events": core.audit_log(limit, model_id, tool, actor),
                  "note": "append-only; events recorded for every MCP call."})


_TOOL_COUNT = len(mcp._tool_manager.list_tools())


if __name__ == "__main__":
    mcp.run(transport="stdio")
