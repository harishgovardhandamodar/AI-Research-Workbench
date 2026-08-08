"""Round-10: portable project export (zip bundle).

Packages the project's recorded research — report, experiments, runs, learnings,
campaigns, evals, suggestions, audit summary and artifacts — into a zip for
sharing / backup. Uses only stdlib.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import zipfile
from pathlib import Path


def export_project(rt, include_code: bool = False) -> Path:
    """Build a zip bundle of the project's research record. Returns the zip path
    (in a temp dir); the caller streams it as a download."""
    store = rt.store
    from .report import build_report_body

    report = build_report_body(rt)
    fd, tmp = tempfile.mkstemp(suffix=".zip")
    os.close(fd)
    Path(tmp).unlink(missing_ok=True)

    def jdump(obj) -> bytes:
        return json.dumps(obj, indent=1, default=str).encode("utf-8")

    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("report.md", report)
        z.writestr("provenance.json",
                   jdump({"project": rt.name, "exported_at": time.time(),
                          "exported_at_iso": time.strftime(
                              "%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                          "format": "fox-project-export-v1"}))
        z.writestr("experiments.json", jdump([
            {**e, "runs": store.experiment_runs(e["id"])}
            for e in store.list_experiments()]))
        z.writestr("learnings.json", jdump(store.list_learnings(limit=2000)))
        z.writestr("campaigns.json", jdump([
            {**c, "steps": store.list_campaign_steps(c["id"])}
            for c in store.list_campaigns()]))
        z.writestr("evals.json", jdump(store.list_evals()))
        z.writestr("suggestions.json", jdump(store.list_suggestions()))
        try:
            z.writestr("audit-summary.json", jdump({
                "summary": rt.audit_store.summary() or {},
                "open_deviations": rt.audit_store.count_open_deviations() or 0,
                "chain_verified": bool((rt.audit_store.verify_chain() or {}).get("verified")),
            }))
        except Exception:  # noqa: BLE001
            pass
        runs_dir = "runs/"
        for r in store.list_runs(limit=2000):
            z.writestr(f"{runs_dir}{r['id']}.json",
                       jdump(store.get_run(r["id"], include_code=include_code)))
        # Artifact files (byte copies).
        try:
            for a in rt.artifacts.list():
                aid = a.get("id")
                if not aid:
                    continue
                data = rt.artifacts.data(aid)
                if data is not None:
                    z.writestr(f"artifacts/{aid}", data)
        except Exception:  # noqa: BLE001
            pass
    return Path(tmp)


def _cleanup(path: Path):
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
