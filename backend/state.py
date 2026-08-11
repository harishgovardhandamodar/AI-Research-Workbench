"""Shared application state for the Fox FastAPI server.

Holds the mutable singletons (config, project runtimes, MCP registry, LLM cache)
so the API router modules and the WebSocket chat handler in main.py share one
source of truth without circular imports.
"""

from __future__ import annotations

import json
import time
import os

from . import editor as editor_cfg
from .llm import DEFAULT_BASE_URL, DEFAULT_MODEL, DEFAULT_TOOL_BASE_URL, LLMClient
from .mcp import DEFAULT_SERVERS, MCPRegistry
from .paths import CONFIG_PATH

DEFAULT_CONFIG = {
    "llm": {
        "base_url": DEFAULT_BASE_URL,
        "tool_base_url": DEFAULT_TOOL_BASE_URL,
        "model": DEFAULT_MODEL,
        "temperature": 0.2,
        "max_tokens": 4096,
    },
    "agent": {"max_iters": 20, "reviewer_enabled": True,
              "runtime_idle_timeout": 0},
    "mcp": {"servers": DEFAULT_SERVERS},
    "finetune": {"workspace": ""},
    "editor": editor_cfg.editor_config(),
    "kaggle": {"username": "", "key": ""},
    "management": {"repo_dir": "", "github_repo": "", "auto_commit": True, "auto_push": False},
}


def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            saved = json.loads(CONFIG_PATH.read_text())
            cfg = json.loads(json.dumps(DEFAULT_CONFIG))
            cfg["llm"].update(saved.get("llm", {}))
            cfg["agent"].update(saved.get("agent", {}))
            cfg["editor"].update(saved.get("editor", {}))
            cfg["kaggle"].update(saved.get("kaggle", {}))
            cfg["management"].update(saved.get("management", {}))
            cfg["finetune"].update(saved.get("finetune", {}))
            if "servers" in saved.get("mcp", {}):
                # Keep user's servers but always surface the bundled default
                # servers (e.g. newly-added "privacy") unless overridden by name.
                by_name = {s.get("name"): s for s in saved["mcp"]["servers"]}
                merged = list(saved["mcp"]["servers"])
                for s in DEFAULT_SERVERS:
                    if s["name"] not in by_name:
                        merged.append(s)
                cfg["mcp"]["servers"] = merged
            return cfg
        except json.JSONDecodeError:
            pass
    return json.loads(json.dumps(DEFAULT_CONFIG))


def save_config(cfg: dict):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))


CONFIG = load_config()


def make_llm() -> LLMClient:
    llm_cfg = CONFIG["llm"]
    return LLMClient(
        base_url=llm_cfg.get("base_url", DEFAULT_BASE_URL),
        tool_base_url=llm_cfg.get("tool_base_url", DEFAULT_TOOL_BASE_URL),
        model=llm_cfg.get("model", DEFAULT_MODEL),
        temperature=llm_cfg.get("temperature", 0.2),
        max_tokens=llm_cfg.get("max_tokens", 4096),
    )


def allowed_origins() -> set[str]:
    """Cross-origin allowlist from FOX_ALLOWED_ORIGINS (comma-separated list).

    Defaults to same-origin only. Set this when the UI is served from a
    different origin than the API (e.g. behind a reverse proxy or a separate
    frontend dev server).
    """
    return {o.strip() for o in os.environ.get("FOX_ALLOWED_ORIGINS", "").split(",") if o.strip()}


def origin_allowed(origin: str, host: str) -> bool:
    """Same-origin check for WebSocket upgrades (blocks CSWSH).

    Browsers always send an Origin header on WebSocket handshakes; require it to
    match the Host header (or be explicitly allowlisted). Origin-less
    (non-browser) clients are accepted.
    """
    if not origin or origin == "null":
        return True
    try:
        origin_host = origin.split("://", 1)[1].rstrip("/")
    except IndexError:
        return False
    return origin_host == host or origin in allowed_origins()


runtimes: dict = {}
_llm_cache: LLMClient | None = None
mcp_registry: MCPRegistry = MCPRegistry(CONFIG.get("mcp", {}).get("servers", []))


def get_runtime(name: str):
    from .project_runtime import ProjectRuntime

    if name not in runtimes:
        runtimes[name] = ProjectRuntime(name)
    rt = runtimes[name]
    # Every access (ws connect, REST request) marks the project active so the
    # idle-eviction loop knows it's in use.
    try:
        rt.last_active = time.time()
    except Exception:  # noqa: BLE001
        pass
    return rt


def get_llm() -> LLMClient:
    global _llm_cache
    if _llm_cache is None:
        _llm_cache = make_llm()
    return _llm_cache


def reset_llm_cache():
    """Drop the cached LLM client so the next call rebuilds from CONFIG."""
    global _llm_cache
    _llm_cache = None


async def rebuild_mcp():
    global mcp_registry
    if mcp_registry is not None:
        await mcp_registry.close()
    mcp_registry = MCPRegistry(CONFIG.get("mcp", {}).get("servers", []))
