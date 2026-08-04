"""Combined server: exposes every ``mcp.*`` tool through one MCP connection.

Connecting to a single server (rather than four stdio subprocesses at once) is
more robust for notebook clients and most MCP hosts; the individual category
servers remain available for granular setups.
"""

from __future__ import annotations

from .base import ToolServer
from . import dataset_server, eval_server, experiment_server, train_server

_INSTRUCTIONS = (
    "MCP-native incremental fine-tuning experiment for tool-using agents. "
    "Tool categories: mcp.dataset.* (data), mcp.train.* (incremental LoRA), "
    "mcp.eval.* (evaluation + paper comparison), mcp.experiment.* (stages/rollback/reports)."
)


def build() -> ToolServer:
    srv = ToolServer("mcp-finetune", instructions=_INSTRUCTIONS)
    for module in (dataset_server, train_server, eval_server, experiment_server):
        module.register(srv)
    return srv
