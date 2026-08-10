"""Deterministic EDA runner: profile + visualize + report a project data file.

The agent's free-form EDA can get stuck re-running the same heavy analysis
(e.g. re-reading a 76 MB CSV every tool call) without ever producing charts.
This endpoint runs the EDA MCP suite once, registers the generated plots and
report as workbench artifacts, and returns the artifact ids so the UI can show
them in chat — reliably, no LLM loop.
"""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException

from ..artifacts.store import Artifact
from ..state import get_runtime

router = APIRouter()


def _resolve_project_file(rt, filename: str) -> Path:
    rel = Path(filename)
    if rel.is_absolute() or ".." in rel.parts:
        raise HTTPException(status_code=400, detail="invalid filename")
    cand = rt.dir / rel
    if not cand.exists() or not cand.is_file():
        raise HTTPException(status_code=404, detail=f"file not found: {filename}")
    return cand


@router.post("/api/projects/{name}/eda/run")
async def eda_run(name: str, body: dict):
    """Run EDA on a project data file: load → profile → auto-visualize →
    report, registering plots + report as artifacts.

    body: {filename, max_plots?} — returns {artifact_ids, figures, report_id,
    summary, dataset_id, message}.
    """
    filename = (body.get("filename") or "").strip()
    if not filename:
        raise HTTPException(status_code=400, detail="filename required")
    rt = get_runtime(name)
    path = _resolve_project_file(rt, filename)
    max_plots = max(1, min(int(body.get("max_plots", 12)), 24))

    try:
        from mcp_servers.eda_mcp.common.store import DatasetStore
        from mcp_servers.eda_mcp import profiler, visualizer, report as eda_report
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=501,
                            detail=f"EDA suite unavailable: {type(e).__name__}: {e}")

    try:
        store = DatasetStore()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=501,
                            detail=f"EDA store unavailable: {type(e).__name__}: {e}")

    # 1. Load the file into the EDA workspace (once).
    try:
        loaded = store.load(str(path), "auto")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=422,
                            detail=f"could not load {filename}: {type(e).__name__}: {e}")
    dataset_id = loaded.get("dataset_id") or loaded.get("id") or ""
    if not dataset_id:
        raise HTTPException(status_code=500, detail="EDA load returned no dataset_id")

    # 2. Profile.
    profile = {}
    try:
        profile = profiler.profile_data(store, dataset_id)
    except Exception:  # noqa: BLE001
        pass

    # 3. Auto-visualize -> PNG plot paths.
    try:
        plots = visualizer.auto_visualize_plots(store, dataset_id, max_plots)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500,
                            detail=f"EDA visualize failed: {type(e).__name__}: {e}")

    # 4. Compile the report (markdown) if available.
    report_md = ""
    try:
        rep = eda_report.compile_report_impl(
            store, dataset_id,
            sections=["profile", "missingness", "univariate", "bivariate", "summary"],
            fmt="markdown")
        rp = Path(rep.get("report_path") or "")
        if rp.exists():
            report_md = rp.read_text(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

    # 5. Register plots + report as workbench artifacts.
    artifact_ids: list[str] = []
    figures: list[dict] = []
    for i, p in enumerate(plots, 1):
        pp = Path(p.get("plot_path") or "")
        if not pp.exists():
            continue
        try:
            data = pp.read_bytes()
        except OSError:
            continue
        art = Artifact(kind="figure", name=pp.stem,
                       description=p.get("caption") or f"EDA plot {i}",
                       code=f"eda({filename})", env={},
                       message_id="", run_id="", data_type="png")
        rt.artifacts.add_artifact(art, data=data, data_type="png")
        artifact_ids.append(art.id)
        figures.append({"id": art.id, "name": art.name,
                        "caption": p.get("caption") or ""})
    report_id = ""
    if report_md:
        art = Artifact(kind="report", name=f"eda-report-{Path(filename).stem}",
                       description=f"EDA report for {filename}",
                       code=f"eda({filename})", env={},
                       message_id="", run_id="", data_type="text")
        rt.artifacts.add_artifact(art, data=report_md.encode(), data_type="text")
        report_id = art.id
        artifact_ids.append(art.id)

    n_rows = (loaded.get("shape") or [0, 0])[0]
    return {
        "ok": True, "filename": filename, "dataset_id": dataset_id,
        "artifact_ids": artifact_ids, "figures": figures,
        "report_id": report_id,
        "summary": {
            "shape": loaded.get("shape"),
            "columns": loaded.get("columns") or [],
            "n_rows": n_rows,
            "n_figures": len(figures),
            "missing": profile.get("missing_columns") if profile else None,
            "high_cardinality": profile.get("high_cardinality") if profile else None,
        },
        "message": (f"EDA done on `{filename}` — {len(figures)} chart(s) "
                    f"and a report generated as artifacts."),
    }


@router.get("/api/projects/{name}/eda/datasets")
async def eda_datasets(name: str):
    """Datasets already loaded into the EDA workspace for this session."""
    try:
        from mcp_servers.eda_mcp.common.store import DatasetStore
        return {"datasets": DatasetStore().list_datasets()}
    except Exception as e:  # noqa: BLE001
        return {"datasets": [], "error": str(e)}
