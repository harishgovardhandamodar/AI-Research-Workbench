"""Domain Knowledge LoRA Fine-Tuning MCP server (dk-lora).

Turns a local directory of heterogeneous artifacts (PDFs, diarized interview
transcripts, news, policies, blog extracts, small datasets) into a high-quality
LoRA/QLoRA adapter for a local LLM, so a local chatbot can answer deeply about
*your* content without constant RAG context stuffing.

Local-first and privacy-preserving: nothing leaves the machine. Unsloth is the
preferred training backend (fastest single-GPU + lowest VRAM) with TRL + PEFT as
a fallback. All paths are validated against traversal; every training example
retains provenance (source file, chunk id, page/speaker).

Run standalone (stdio):

    python -m mcp_servers.dk_lora.server

The MCP server is registered in the workbench's ``DEFAULT_SERVERS`` so it can be
launched automatically by ``MCPRegistry``.
"""

from __future__ import annotations

__version__ = "0.1.0"

# Env var that overrides the default workspace directory.
DK_LORA_WORKSPACE_ENV = "FOX_DK_LORA_WORKSPACE"
DEFAULT_WORKSPACE = "~/.fox/dk-lora"
