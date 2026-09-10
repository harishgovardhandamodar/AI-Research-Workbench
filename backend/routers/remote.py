"""Remote workbench proxy — LAN/Tailscale-deployable hive-machine agents.

A remote workbench is a `fox-kernel` server running on another machine
(e.g. axiom with 2x RTX5080) plus this proxy. The workbench:

- stores remote hosts in CONFIG["remote"] (persisted to config.json via
  save_config; tokens are redacted on read like kaggle keys),
- discovers them on demand: GET {base}/health then {base}/api/kernel/gpu
  with short timeouts (no SSH, no subprocess, no background pollers —
  discovery only runs when the user clicks Discover),
- offloads code: POST {base}/api/kernel/execute with a Bearer token and
  records the result as a kind="remote" run in the project store.

Deploy on the remote machine (axiom):
    REMOTE_TOKEN=<token> python -m backend.kernels.server --host 0.0.0.0 --port 8891
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from urllib.parse import urlparse

import requests
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from ..state import CONFIG, get_runtime, save_config
from .system import _MCP_MASK

router = APIRouter(prefix="/api/remote", tags=["remote"])

HEALTH_TIMEOUT = 5.0
GPU_TIMEOUT = 8.0
EXECUTE_TIMEOUT = 120.0
EXECUTE_TIMEOUT_MAX = 600.0
REPLY_TRUNCATE = 8000


# -- pure helpers (unit-tested) -------------------------------------------


def normalize_base_url(raw: str) -> str:
    """Normalize a host base URL: strip, default to http, drop trailing slash."""
    raw = (raw or "").strip()
    if not raw:
        raise ValueError("base_url required (e.g. http://axiom:8891)")
    if "://" not in raw:
        raw = "http://" + raw
    parts = urlparse(raw)
    if parts.scheme not in ("http", "https"):
        raise ValueError("base_url must be http(s)")
    if not parts.hostname:
        raise ValueError("base_url must include a host")
    return f"{parts.scheme}://{parts.netloc}"


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def validate_host_entry(entry: dict) -> dict:
    """Validate + normalize one host entry (raises ValueError)."""
    if not isinstance(entry, dict):
        raise ValueError("host must be an object")
    base_url = normalize_base_url(entry.get("base_url", ""))
    name = (entry.get("name") or "").strip() or urlparse(base_url).hostname or base_url
    host_id = (entry.get("id") or "").strip() or _new_id()
    return {
        "id": host_id,
        "name": name,
        "base_url": base_url,
        "username": (entry.get("username") or "").strip(),
        "token": entry.get("token") or "",
        "verify_tls": bool(entry.get("verify_tls", True)),
        "use_gpu": bool(entry.get("use_gpu", True)),
    }


def redact_hosts(hosts: list) -> list:
    """Copy hosts with tokens masked (mirrors kaggle key redaction)."""
    out = []
    for h in hosts or []:
        h = dict(h)
        if h.get("token"):
            h["token"] = _MCP_MASK
        out.append(h)
    return out


def merge_hosts(orig: list, new: list) -> list:
    """Merge host list preserving live tokens behind the mask (like _merge_mcp_server)."""
    old_by_id = {h.get("id"): h for h in (orig or []) if isinstance(h, dict)}
    merged = []
    for entry in new or []:
        entry = validate_host_entry(entry)
        old = old_by_id.get(entry["id"], {})
        if entry.get("token") == _MCP_MASK and old.get("token"):
            entry["token"] = old["token"]
        merged.append(entry)
    return merged


def parse_gpu_payload(data: object) -> dict:
    """Normalize a remote /api/kernel/gpu payload (tolerant to shapes)."""
    if not isinstance(data, dict):
        return {"available": False, "devices": [], "error": "unexpected gpu payload"}
    devices = data.get("devices")
    if not isinstance(devices, list):
        devices = []
    available = bool(data.get("available")) and bool(devices)
    return {
        "available": available,
        "devices": devices,
        "error": "" if available else str(data.get("error") or "no GPUs reported"),
    }


def _http_json(method: str, url: str, headers: dict | None = None,
               body: dict | None = None, timeout: float = 5.0,
               verify: bool | str = True) -> tuple[int | None, dict | list | None, str]:
    """One-shot HTTP JSON call; always closes the response (no fd leaks)."""
    try:
        with requests.Session() as session:
            resp = session.request(method, url, headers=headers or {},
                                   json=body, timeout=timeout, verify=verify)
            status = resp.status_code
            try:
                return status, resp.json(), ""
            except ValueError:
                text = (resp.text or "")[:200]
                return status, None, f"non-JSON response: {text}"
    except requests.Timeout:
        return None, None, "timed out"
    except requests.ConnectionError:
        return None, None, "connection refused/unreachable"
    except Exception as e:  # noqa: BLE001
        return None, None, f"{type(e).__name__}: {e}"[:200]


def _auth_headers(host: dict) -> dict:
    token = (host.get("token") or "").strip()
    return {"Authorization": f"Bearer {token}"} if token else {}


def _remote_config() -> dict:
    cfg = CONFIG.get("remote") or {}
    if not isinstance(cfg, dict):
        cfg = {}
    cfg.setdefault("hosts", [])
    cfg.setdefault("active_host", "")
    return cfg


def _seed_from_env() -> tuple[list, bool]:
    """Seed hosts from REMOTE_HOSTS env (comma-separated base_urls) when config is empty.

    Read-only: never saves, so the GUI stays the source of truth after first save.
    """
    cfg = _remote_config()
    if cfg.get("hosts"):
        return cfg["hosts"], False
    raw = os.environ.get("REMOTE_HOSTS", "").strip()
    if not raw:
        return [], False
    token = os.environ.get("REMOTE_TOKEN", "")
    seeded = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            base_url = normalize_base_url(part)
        except ValueError:
            continue
        seeded.append({
            "id": _new_id(),
            "name": urlparse(base_url).hostname or base_url,
            "base_url": base_url,
            "username": os.environ.get("REMOTE_USER", ""),
            "token": token,
            "verify_tls": True,
            "use_gpu": True,
        })
    return seeded, bool(seeded)


async def _probe_host(host: dict) -> dict:
    """Probe one host: /health then /api/kernel/gpu (sequential, bounded)."""
    base = host["base_url"]
    verify = host.get("verify_tls", True)
    started = time.time()
    result: dict = {
        "id": host["id"], "name": host.get("name", base), "base_url": base,
        "online": False, "compatible": False, "server": "",
        "latency_ms": 0, "gpu": {"available": False, "devices": [], "error": ""},
        "error": "",
    }
    status, data, err = await asyncio.to_thread(
        _http_json, "GET", base + "/health", None, None, HEALTH_TIMEOUT, verify)
    result["latency_ms"] = int((time.time() - started) * 1000)
    if err or status != 200 or not isinstance(data, dict):
        result["error"] = err or f"health check HTTP {status}"
        return result
    result["online"] = True
    # Compatibility: our fox-kernel answers {"ok": true, "kernel": ...}.
    # Anything else (e.g. "Hive Trade Util") is online but not a remote workbench.
    if data.get("ok") is True and "kernel" in data:
        result["compatible"] = True
        result["server"] = "fox-kernel"
    else:
        result["server"] = str(data.get("name") or data.get("title") or "unknown service")
        result["error"] = f"not a fox-kernel remote workbench (got {result['server']})"
        return result
    _, gdata, gerr = await asyncio.to_thread(
        _http_json, "GET", base + "/api/kernel/gpu", _auth_headers(host),
        None, GPU_TIMEOUT, verify)
    if gerr:
        result["gpu"] = {"available": False, "devices": [], "error": gerr}
    else:
        result["gpu"] = parse_gpu_payload(gdata)
    return result


def _resolve_host(host_id: str | None) -> tuple[dict | None, str]:
    cfg = _remote_config()
    hosts, _ = _seed_from_env()
    if not hosts:
        hosts = cfg.get("hosts", [])
    if not hosts:
        return None, "no remote hosts configured (add one in the Remote tab or set REMOTE_HOSTS)"
    if host_id:
        for h in hosts:
            if h.get("id") == host_id:
                return h, ""
        return None, f"unknown host id {host_id}"
    active = (cfg.get("active_host") or "").strip()
    if active:
        for h in hosts:
            if h.get("id") == active:
                return h, ""
    if len(hosts) == 1:
        return hosts[0], ""
    return None, "multiple hosts configured — pick one (or set active)"


# -- routes -----------------------------------------------------------------


@router.get("/hosts")
async def remote_hosts():
    hosts, seeded = _seed_from_env()
    return {"hosts": redact_hosts(hosts), "active_host": _remote_config().get("active_host", ""),
            "seeded_from_env": seeded}


@router.post("/hosts")
async def remote_upsert_host(body: dict):
    try:
        entry = validate_host_entry(body or {})
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    cfg = _remote_config()
    hosts = [h for h in cfg.get("hosts", []) if h.get("id") != entry["id"]]
    old = next((h for h in cfg.get("hosts", []) if h.get("id") == entry["id"]), {})
    if entry.get("token") == _MCP_MASK and old.get("token"):
        entry["token"] = old["token"]
    hosts.append(entry)
    cfg["hosts"] = hosts
    CONFIG["remote"] = cfg
    save_config(CONFIG)
    return {"host": redact_hosts([entry])[0]}


@router.delete("/hosts/{host_id}")
async def remote_delete_host(host_id: str):
    cfg = _remote_config()
    hosts = [h for h in cfg.get("hosts", []) if h.get("id") != host_id]
    if len(hosts) == len(cfg.get("hosts", [])):
        return JSONResponse({"error": "unknown host id"}, status_code=404)
    cfg["hosts"] = hosts
    if cfg.get("active_host") == host_id:
        cfg["active_host"] = ""
    CONFIG["remote"] = cfg
    save_config(CONFIG)
    return {"deleted": host_id}


@router.post("/active")
async def remote_set_active(body: dict):
    host_id = ((body or {}).get("host_id") or "").strip()
    cfg = _remote_config()
    ids = {h.get("id") for h in cfg.get("hosts", [])}
    if host_id and host_id not in ids:
        return JSONResponse({"error": "unknown host id"}, status_code=404)
    cfg["active_host"] = host_id
    CONFIG["remote"] = cfg
    save_config(CONFIG)
    return {"active_host": host_id}


@router.post("/discover")
async def remote_discover(body: dict | None = None):
    """Probe hosts on demand: /health then /api/kernel/gpu (bounded timeouts).

    No SSH, no subprocess, no background pollers — runs only when called so a
    dead host can never wedge the server with leaked file descriptors.
    """
    body = body or {}
    host_id = (body.get("host_id") or "").strip() or None
    cfg = _remote_config()
    hosts, _ = _seed_from_env()
    if not hosts:
        hosts = cfg.get("hosts", [])
    if host_id:
        hosts = [h for h in hosts if h.get("id") == host_id]
        if not hosts:
            return JSONResponse({"error": "unknown host id"}, status_code=404)
    results = []
    for host in hosts:
        try:
            results.append(await _probe_host(host))
        except Exception as e:  # noqa: BLE001
            results.append({"id": host.get("id"), "name": host.get("name"),
                            "base_url": host.get("base_url"), "online": False,
                            "error": f"{type(e).__name__}: {e}"[:200]})
    return {"hosts": results, "count": len(results)}


@router.post("/run")
async def remote_run(body: dict):
    """Offload code to a remote workbench and report back as a kind=remote run.

    Body: {host_id?, project, code, timeout?, label?, experiment_id?, use_gpu?}.
    use_gpu=true fails fast when the host reports no GPU.
    """
    body = body or {}
    project = ((body.get("project") or "").strip() or "default").replace("/", "_")
    code = body.get("code") or ""
    if not (code or "").strip():
        return JSONResponse({"error": "code required"}, status_code=400)
    try:
        timeout = float(body.get("timeout") or EXECUTE_TIMEOUT)
    except (TypeError, ValueError):
        return JSONResponse({"error": "timeout must be a number"}, status_code=400)
    timeout = min(max(timeout, 1.0), EXECUTE_TIMEOUT_MAX)
    use_gpu = bool(body.get("use_gpu", False))

    host, err = _resolve_host((body.get("host_id") or "").strip() or None)
    if host is None:
        return JSONResponse({"error": err}, status_code=400)

    gpu_devices: list = []
    if use_gpu:
        gpu = await _probe_host_gpu_only(host)
        if not gpu.get("available"):
            return JSONResponse(
                {"error": f"host {host.get('name')} reports no GPU: {gpu.get('error')}"},
                status_code=400)
        gpu_devices = [d.get("name", "?") for d in gpu.get("devices", [])]

    started = time.time()
    status, data, herr = await asyncio.to_thread(
        _http_json, "POST", host["base_url"] + "/api/kernel/execute",
        _auth_headers(host), {"code": code, "timeout": timeout},
        timeout + 15.0, host.get("verify_tls", True))
    duration = time.time() - started

    ok = herr == "" and status == 200 and isinstance(data, dict) and data.get("ok", True)
    output = ""
    error = herr or ("" if status == 200 else f"remote HTTP {status}")
    if isinstance(data, dict):
        output = str(data.get("output") or "")
        if not ok and not error:
            error = str(data.get("error") or "remote execution failed")[:2000]
    truncated = False
    if len(output) > REPLY_TRUNCATE:
        output = output[:REPLY_TRUNCATE]
        truncated = True
    metrics = {
        "host": host.get("name", host["base_url"]),
        "base_url": host["base_url"],
        "duration_s": round(duration, 2),
        "remote_ok": bool(ok),
        "truncated": truncated,
        "gpu_used": gpu_devices,
    }
    label = (body.get("label") or "").strip() or f"remote:{host.get('name')}"
    try:
        rt = get_runtime(project)
        rid = rt.store.add_run(
            prompt=label, reply=output, status="done" if ok else "error",
            started_at=started, finished_at=time.time(),
            tool_sequence=[{"name": "remote_execute", "ok": bool(ok),
                            "args": {"host": host.get("name"), "timeout": timeout}}],
            artifact_ids=[], metrics=metrics,
            experiment_id=body.get("experiment_id"), config={"host_id": host["id"]},
            label=label, kind="remote",
            code=[{"name": "remote_execute", "code": code}],
            error=None if ok else error)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": f"remote ok but local record failed: {e}"},
                            status_code=500)
    out: dict = {"run_id": rid, "project": project, "host": host.get("name"),
                 "ok": bool(ok), "output": output, "error": error, "metrics": metrics}
    return JSONResponse(out, status_code=200 if ok else 502)


async def _probe_host_gpu_only(host: dict) -> dict:
    _, gdata, gerr = await asyncio.to_thread(
        _http_json, "GET", host["base_url"] + "/api/kernel/gpu",
        _auth_headers(host), None, GPU_TIMEOUT, host.get("verify_tls", True))
    if gerr:
        return {"available": False, "devices": [], "error": gerr}
    return parse_gpu_payload(gdata)
