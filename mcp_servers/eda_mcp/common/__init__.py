"""Shared subpackage: dataset store, helpers and optional local-LLM narrative."""

from .store import DatasetStore, workspace_dir
from . import utils

__all__ = ["DatasetStore", "workspace_dir", "utils"]
