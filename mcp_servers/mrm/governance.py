"""Governance (Pillar 3): tiering, maker-checker approvals, effective challenge,
retirement, cross-file consistency and audit-ready report generation.

Model tier changes and retirement are approval-gated: a 1st-line actor must
raise a ``request_approval`` that a 2nd/3rd-line role decides before the change
applies (or the actor must already hold a validator/admin role). Every action
is written to the append-only audit log by the MCP layer.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from . import core

TIER_DESCRIPTIONS = {
    1: "Tier 1 — high materiality: large exposures, systemic relevance, "
       "synthetic-data reliance; strongest controls (approval-gated).",
    2: "Tier 2 — moderate materiality: notable economic impact, routine "
       "monitoring with periodic validation.",
    3: "Tier 3 — low materiality: supplementary/supporting models; light "
       "oversight and annual review.",
}


# -------------------------------------------------------------- tiering + state --

def tier_model(model_id: str, tier: int, rationale: str = "",
               actor: str = "agent", role: str = "validator") -> dict:
    """Change a model's risk tier. Approval-gated for non-validator actors."""
    m = core.require_model(model_id)
    if tier not in (1, 2, 3):
        raise ValueError("tier must be 1, 2 or 3")
    if int(tier) == int(m["tier"]):
        return {"status": "unchanged", "model_id": model_id, "tier": int(tier)}
    if int(tier) < int(m["tier"]):
        # moving to a HIGHER-risk tier needs a checker
        core._ensure_approved(model_id, "tier", role)
    m = core.update_model(model_id, tier=int(tier))
    return {"status": "tiered", "model_id": model_id, "tier": int(tier),
            "rationale": rationale,
            "tier_description": TIER_DESCRIPTIONS[int(tier)]}


def update_model_status(model_id: str, status: str, rationale: str = "",
                        actor: str = "agent", role: str = "validator") -> dict:
    """Advance a model through its lifecycle. Approval-gated transitions:
    development -> validation (deploy) and any -> retired."""
    m = core.require_model(model_id)
    if status not in core.MODEL_STATUSES:
        raise ValueError(f"invalid status '{status}' — must be one of "
                         f"{list(core.MODEL_STATUSES)}")
    if status == m["status"]:
        return {"status": "unchanged", "model_id": model_id,
                "model_status": status}
    if status == "retired":
        core._ensure_approved(model_id, "retire", role)
    elif status in ("approved", "monitoring", "retirement_pending"):
        core._ensure_approved(model_id, "status", role)
    m = core.update_model(model_id, status=status)
    return {"status": "updated", "model_id": model_id, "model_status": status,
            "rationale": rationale}


def retire_model(model_id: str, rationale: str = "",
                 actor: str = "agent", role: str = "validator") -> dict:
    """Retire a model (approval-gated: raises an approval request if none is on
    record, and requires a checker role to apply immediately)."""
    m = core.require_model(model_id)
    core._ensure_approved(model_id, "retire", role)
    m = core.update_model(model_id, status="retired")
    return {"status": "retired", "model_id": model_id, "rationale": rationale}


# ------------------------------------------------------- maker-checker gateway --

def request_approval(model_id: str, action: str, rationale: str,
                     requested_by: str = "agent",
                     requested_role: str = "developer") -> dict:
    """Maker step: raise a human approval request for an approval-gated action
    (tier / status / retire / deploy / use_synthetic)."""
    a = core.request_approval(model_id, action, rationale, requested_by,
                              requested_role)
    return a


def decide_approval(approval_id: str, decision: str,
                    decided_by: str = "validator",
                    decided_role: str = "validator") -> dict:
    """Checker step: a 2nd/3rd-line role approves or rejects a pending request."""
    a = core.decide_approval(approval_id, decision, decided_by, decided_role)
    return a


def pending_approvals(actor_role: str = "developer") -> dict:
    """List open maker-checker requests (visible to checker roles)."""
    approvals = core.list_approvals(status="pending")
    return {"count": len(approvals), "pending": approvals}


# ----------------------------------------------------- effective challenge --

def log_effective_challenge(model_id: str, finding: str, severity: str = "medium",
                            disposition: str = "open",
                            logged_by: str = "validator") -> dict:
    """Record a validation finding (effective challenge) against a model.

    High/critical open findings automatically flag the model for review and
    downgrade its latest validation report to 'findings'."""
    core.require_model(model_id)
    c = core.log_challenge(model_id, finding, severity, disposition, logged_by)
    if severity in ("high", "critical") and disposition in ("open", "accepted"):
        reports = core.list_validation_reports(model_id)
        if reports:
            core.save_validation_report(
                model_id, reports[0]["name"], reports[0]["metrics"],
                reports[0]["findings"] + [finding],
                status="challenged", evidence=reports[0]["evidence"])
    return c


# ------------------------------------------------------------- consistency --

def check_cross_file_consistency(files: list[str]) -> dict:
    """Cross-file consistency: schema overlap, shared-numeric KS agreement and
    row-count sanity across a family of datasets (real + synthetic + generated).
    """
    if len(files) < 2:
        raise ValueError("provide at least two files to compare")
    frames, issues = [], []
    for f in files:
        p = Path(f)
        if not p.exists():
            raise ValueError(f"file not found: {f}")
        try:
            frames.append((str(p), pd.read_csv(p)))
        except Exception as e:  # noqa: BLE001
            issues.append({"file": f, "error": f"{type(e).__name__}: {e}"})
    if issues:
        return {"verdict": "FAIL", "issues": issues, "checked": []}

    name_a, a = frames[0]
    checks = []
    for name_b, b in frames[1:]:
        shared = sorted(set(a.columns) & set(b.columns))
        schema_ok = len(shared) == max(len(a.columns), len(b.columns))
        numeric = [c for c in shared if a[c].dtype.kind in "fiu"
                   and b[c].dtype.kind in "fiu"]
        weak = []
        for c in numeric[:6]:
            try:
                from scipy.stats import ks_2samp
                _, p = ks_2samp(a[c].dropna(), b[c].dropna(), method="asymp")
            except ImportError:  # noqa: BLE001
                p = 1.0
            if p is not None and p < 0.01:
                weak.append(c)
        checks.append({
            "file_b": str(name_b),
            "columns_shared": shared,
            "schema_consistent": bool(schema_ok),
            "row_counts": [len(a), len(b)],
            "numeric_ks_fail": weak,
            "consistent": bool(schema_ok and not weak),
        })
    verdict = "PASS" if all(c["consistent"] for c in checks) else "REVIEW"
    return {"verdict": verdict, "checks": checks, "files": [str(f) for f in files],
            "assessed_at": core._now()}


# -------------------------------------------------------------- report builder --

def generate_validation_report(model_id: str, profile: str = "",
                               validation_data: dict | None = None,
                               author: str = "validator") -> dict:
    """Compile an audit-ready Markdown validation report from the inventory +
    the latest validation results + evidence + challenges. Writes it to
    ``<store>/reports/<model_id>_validation.md`` and attaches it as evidence."""
    m = core.require_model(model_id)
    if profile and profile not in _profile_categories():
        raise ValueError(f"unknown profile '{profile}' — available: "
                         f"{_profile_categories()}")

    reports = core.list_validation_reports(model_id)
    latest = reports[0] if reports else {"metrics": {}, "findings": [],
                                         "status": "pending", "name": ""}
    challenges = core.list_challenges(model_id)
    evidence = core.list_evidence(model_id)
    approvals = core.list_approvals(model_id)
    sims = core.list_simulations(model_id)

    lines = [
        "# Model Risk Management — Validation Report",
        "",
        f"**Model:** {m['name']} (`{m['id']}`)",
        f"**Category:** {m['category']} | **Tier:** {m['tier']} | "
        f"**Status:** {m['status']}",
        f"**Owner:** {m['owner']} | **Validator:** {m['validator'] or 'TBD'}",
        f"**Synthetic data used:** {'yes' if m['synthetic_used'] else 'no'} | "
        f"**TSTR completed:** {'yes' if m['tstr_completed'] else 'no'}",
        f"**Profile:** {profile or 'custom'}",
        f"**Generated:** {core._now()} by {author}",
        "",
        "## 1. Intended use & limitations",
        m["use_limitations"] or "_None documented._",
        "",
        "## 2. Assumptions (conceptual soundness — Pillar 1)",
    ]
    for a in json.loads(m.get("assumptions") or "[]"):
        lines.append(f"- **{a.get('aspect', '')}:** {a.get('assumption', '')}")
    if not json.loads(m.get("assumptions") or "[]"):
        lines.append("_No assumptions recorded — extract_generator_assumptions "
                     "or register with assumptions._")
    lines += ["", "## 3. Validation results (Pillar 2)", ""]
    lines.append(f"Latest report: **{latest['status']}** — {latest['name'] or 'n/a'}")
    if latest["metrics"]:
        lines.append("```json")
        lines.append(json.dumps(latest["metrics"], indent=2, default=str))
        lines.append("```")
    if validation_data:
        lines += ["", "Validation data (this run):", ""]
        lines.append("```json")
        lines.append(json.dumps(validation_data, indent=2, default=str)[:6000])
        lines.append("```")
    lines += ["", "## 4. Effective challenge log", ""]
    if challenges:
        for c in challenges:
            lines.append(f"- `[{c['severity']}/{c['disposition']}]` {c['finding']} "
                         f"(_logged by {c['logged_by']}_)")
    else:
        lines.append("_No open challenges._")
    lines += ["", "## 5. Maker-checker approvals", ""]
    if approvals:
        for a in approvals:
            lines.append(f"- `{a['action']}` -> `{a['status']}` "
                         f"(requested by {a['requested_by']}, "
                         f"decided by {a['decided_by'] or '—'})")
    else:
        lines.append("_None._")
    lines += ["", "## 6. Evidence", ""]
    for e in evidence:
        lines.append(f"- [{e['kind']}] {e['description']} — `{e['path'] or 'inline'}`")
    if sims:
        lines += ["", "## 7. Linked simulations", ""]
        for s in sims:
            lines.append(f"- `{s['id']}` {s['name']} ({s['generator'] or '—'}, "
                         f"seed={s['seed']}, status={s['status']})")

    body = "\n".join(lines) + "\n"
    out = core.store_root() / "reports" / f"{m['id']}_validation.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(body, encoding="utf-8")
    ev = core.attach_evidence(model_id, kind="report",
                              description=f"Validation report ({profile or 'custom'})",
                              path=str(out), created_by=author)
    return {
        "status": "success",
        "model_id": model_id,
        "report_path": str(out),
        "report_markdown": body,
        "evidence_id": ev["id"],
        "character_count": len(body),
    }


def _profile_categories() -> list[str]:
    from . import profiles as _p
    return [p["category"] for p in _p.list_profiles()]
