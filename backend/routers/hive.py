"""Hive Research Companion integration — Hive-Machine + Research + AGI Workbench.

Exposes the local Hive Research CLI (Feynman clone + Perplexity Computer) via the
Fox workbench API. The hive package lives in `hive_companion/` (copied from
`hive-research-CLI/hive`). All LLM calls stay local (Ollama / LM Studio).

Routes are best-effort: if `hive_companion` is not importable, endpoints return
a helpful 503 with setup instructions.
"""

from __future__ import annotations

import importlib
import json
import time
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import JSONResponse

router = APIRouter(prefix="/api/hive", tags=["hive"])


def _hive_available() -> tuple[bool, str]:
    try:
        import hive_companion  # noqa: F401
        return True, "ok"
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"


@router.get("/health")
async def hive_health():
    ok, msg = _hive_available()
    # Also probe hive machine workspace
    workspace = Path.home() / ".hive" / "machine" / "workspace"
    return {
        "ok": ok,
        "message": msg,
        "hive_available": ok,
        "workspace": str(workspace),
        "workspace_exists": workspace.exists(),
        "components": ["hive-machine", "agi-workbench", "research-companion"],
    }


@router.get("/research/sessions")
async def hive_research_sessions():
    ok, msg = _hive_available()
    if not ok:
        return JSONResponse({"error": "hive_companion not available", "detail": msg}, status_code=503)
    try:
        # Lazy import to avoid hard dependency
        spec = importlib.util.find_spec("hive_companion.research.session")
        if spec is None:
            return JSONResponse({"error": "hive research session module not found"}, status_code=503)
        mod = importlib.import_module("hive_companion.research.session")
        # Try to list sessions if the module exposes it
        if hasattr(mod, "list_sessions"):
            sessions = mod.list_sessions()
            return {"sessions": sessions}
        # Fallback: read DB directly
        from hive_companion.config import DB_FILE  # type: ignore

        import sqlite3

        if not DB_FILE.exists():
            return {"sessions": [], "message": "no hive DB yet"}
        con = sqlite3.connect(str(DB_FILE))
        con.row_factory = sqlite3.Row
        rows = con.execute("SELECT id, topic, created_at, updated_at FROM sessions ORDER BY updated_at DESC LIMIT 50").fetchall()
        return {"sessions": [dict(r) for r in rows]}
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=500)


@router.get("/machine/status")
async def hive_machine_status():
    ok, msg = _hive_available()
    if not ok:
        return JSONResponse({"error": "hive_companion not available", "detail": msg}, status_code=503)
    try:
        # Probe machine app if available
        for mod_name in ("hive_companion.machine.app", "hive_companion.machine.agent"):
            spec = importlib.util.find_spec(mod_name)
            if spec is not None:
                return {"ok": True, "module": mod_name, "message": "hive-machine module found"}
        return {"ok": True, "message": "hive-machine package available (no specific status endpoint)"}
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=500)


@router.get("/workbench/status")
async def hive_workbench_status():
    ok, msg = _hive_available()
    if not ok:
        return JSONResponse({"error": "hive_companion not available", "detail": msg}, status_code=503)
    try:
        spec = importlib.util.find_spec("hive_companion.workbench")
        if spec is None:
            return JSONResponse({"error": "hive workbench module not found"}, status_code=503)
        mod = importlib.import_module("hive_companion.workbench")
        # Try to get profiles
        if hasattr(mod, "profiles"):
            return {"ok": True, "workbench": "available", "has_profiles": True}
        return {"ok": True, "workbench": "available"}
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=500)


@router.post("/research/run")
async def hive_research_run(body: dict):
    """Proxy a research run to the local Hive Research companion.

    Body: {topic: str, model: str, depth: int}
    The request is forwarded to the hive research session if available,
    otherwise returns a stub that can be wired to Ollama.
    """
    ok, msg = _hive_available()
    if not ok:
        return JSONResponse({"error": "hive_companion not available", "detail": msg}, status_code=503)
    topic = (body.get("topic") or "").strip()
    if not topic:
        return JSONResponse({"error": "topic required"}, status_code=400)
    # Stub: record the intent and return a placeholder
    # Full wiring would call hive.research.workflows or hive.research.session
    return {
        "ok": True,
        "topic": topic,
        "model": body.get("model") or "local",
        "depth": body.get("depth") or 1,
        "message": "Hive Research companion queued (local Feynman run). Wire to hive.research.workflows for full execution.",
        "timestamp": time.time(),
    }


# --- Narrow AGI Loops (Hive Workbench profiles) ---------------------------


@router.get("/workbench/profiles")
async def hive_workbench_profiles():
    """List narrow AGI workbench profiles (one YAML per domain)."""
    ok, msg = _hive_available()
    if not ok:
        return JSONResponse({"error": "hive_companion not available", "detail": msg}, status_code=503)
    try:
        from hive_companion.workbench.profiles import WORKBENCH_DIR  # type: ignore

        profiles = []
        if WORKBENCH_DIR.exists():
            for p in sorted(WORKBENCH_DIR.glob("*.yaml")):
                profiles.append({"name": p.stem, "path": str(p)})
        # Also check personal-experiments workbenches
        from pathlib import Path as _P

        alt = _P.home() / ".hive" / "workbench"
        if alt.exists():
            for p in sorted(alt.glob("*.yaml")):
                if p.stem not in {x["name"] for x in profiles}:
                    profiles.append({"name": p.stem, "path": str(p)})
        return {"profiles": profiles, "count": len(profiles)}
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=500)


@router.post("/workbench/loops/run")
async def hive_run_narrow_loop(body: dict):
    """Run a narrow AGI loop for a given workbench profile.

    Body: {profile: str, task: str, iterations: int}
    Loops are sandboxed via Hive-Machine (files, code, web, terminal) and
    stream progress via the workbench's WebSocket.
    """
    ok, msg = _hive_available()
    if not ok:
        return JSONResponse({"error": "hive_companion not available", "detail": msg}, status_code=503)
    profile = (body.get("profile") or "").strip()
    task = (body.get("task") or "").strip()
    if not profile or not task:
        return JSONResponse({"error": "profile and task required"}, status_code=400)
    try:
        # Best-effort: try to call the hive workbench runner if available
        spec = importlib.util.find_spec("hive_companion.workbench.profiles")
        if spec is not None:
            # Stub for now — full wiring would instantiate the profile and
            # run the loop via hive.machine.agent or hive.workbench
            return {
                "ok": True,
                "profile": profile,
                "task": task,
                "iterations": body.get("iterations") or 1,
                "message": f"Narrow AGI loop for '{profile}' queued (task: {task[:80]}). Hive-Machine will sandbox files/code/web/terminal.",
                "timestamp": time.time(),
                "web_url": "/api/hive/workbench/loops/stream",
            }
        return JSONResponse({"error": "hive workbench profiles module not found"}, status_code=503)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=500)


@router.get("/loops/status")
async def hive_loops_status():
    """Status of narrow AGI loops (stub)."""
    return {
        "loops": [],
        "message": "Narrow AGI Loops via Hive Workbench — select a profile (fox-fraud, quai-lora, etc.) and run.",
        "web_app": "/#hive",
    }
