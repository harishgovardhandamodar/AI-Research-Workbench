"""In-browser VS Code (code-server) integration for the workbench.

The bundled ``code-server`` Docker service shares the workbench data volume so
the generated content (artifacts, notebooks, knowledge graphs, project files)
can be opened and edited in a full VS Code editor that runs in a browser tab.
This module holds the URL/folder configuration (env-driven) shared by the REST
API (``/api/editor``) and the agent's ``editor__*`` tools.
"""

from __future__ import annotations

import os

EDITOR_ENABLED = os.environ.get("FOX_EDITOR_ENABLED", "1").lower() not in (
    "0", "false", "no", "off",
)
# Where the shared workbench volume is mounted inside the code-server container.
EDITOR_FOLDER = os.environ.get("FOX_EDITOR_FOLDER", "/home/coder/workbench")
# Browser-visible URL of the code-server UI (same host as the workbench UI).
EDITOR_URL = os.environ.get("FOX_EDITOR_URL", "http://127.0.0.1:8787")
# URL the *server container* uses to probe code-server (compose service name).
EDITOR_PROBE_URL = os.environ.get(
    "FOX_EDITOR_PROBE_URL", EDITOR_URL)


def editor_enabled() -> bool:
    return EDITOR_ENABLED


def editor_url() -> str:
    return EDITOR_URL


def editor_folder() -> str:
    return EDITOR_FOLDER


def editor_probe_url() -> str:
    return EDITOR_PROBE_URL


def editor_enabled() -> bool:
    return EDITOR_ENABLED


def editor_url() -> str:
    return EDITOR_URL


def editor_folder() -> str:
    return EDITOR_FOLDER


def editor_config() -> dict:
    return {
        "enabled": editor_enabled(),
        "url": editor_url(),
        "folder": editor_folder(),
    }
