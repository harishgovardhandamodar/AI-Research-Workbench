"""Fine-Tune Validation MCP server (ft-validate).

Post-training verification of a LoRA/QLoRA adapter against the original
artifacts. Uses local RAG over the source documents as ground truth, compares
base model vs adapter on the same held-out questions, and scores faithfulness,
factual accuracy, hallucination and retention. Produces actionable JSON +
Markdown reports.

Local-first and privacy-preserving. Unsloth is the preferred inference backend;
sentence-transformers + a disk-backed vector index power the RAG layer. Long
verification runs execute in background subprocesses with pollable job status.

Run standalone (stdio):

    python -m mcp_servers.ft_validate.server

Registered in the workbench's ``DEFAULT_SERVERS`` so ``MCPRegistry`` can launch
it automatically.
"""

from __future__ import annotations

__version__ = "0.1.0"

# Env var that overrides the default workspace directory.
FT_VALIDATE_WORKSPACE_ENV = "FOX_FT_VALIDATE_WORKSPACE"
DEFAULT_WORKSPACE = "~/.fox/ft-validate"
