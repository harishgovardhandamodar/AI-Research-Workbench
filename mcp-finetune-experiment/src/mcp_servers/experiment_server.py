"""``mcp.experiment.*`` tools: create project, manage stages, export reports."""

from __future__ import annotations

from experiment.controller import ExperimentController
from .base import ToolServer
from .project import project_dir


def register(srv: ToolServer) -> None:
    """Register this category's tools onto a ToolServer (own or shared)."""
    def _ctl():
        return ExperimentController(project_dir())

    @srv.tool("mcp.experiment.create",
              "Create the experiment project (name, base model, paper.json).",
              {"type": "object",
               "properties": {
                   "name": {"type": "string"},
                   "base_model": {"type": "string"},
                   "paper": {"type": "object"},
                   "seed": {"type": "integer", "default": 0},
                   "config": {"type": "object"},
               },
               "required": ["name", "base_model"]})
    async def create(name: str, base_model: str, paper: dict | None = None,
                     seed: int = 0, config: dict | None = None):
        return _ctl().create(name, base_model, paper, seed, config)

    @srv.tool("mcp.experiment.list_stages", "List all stages and the current stage.",
              {"type": "object", "properties": {}})
    async def list_stages():
        return _ctl().list_stages()

    @srv.tool("mcp.experiment.create_stage",
              "Create a new stage (optionally branching from a parent stage).",
              {"type": "object",
               "properties": {
                   "stage_id": {"type": "string"},
                   "name": {"type": "string"},
                   "parent": {"type": "string"},
                   "config": {"type": "object"},
                   "data_hashes": {"type": "array", "items": {"type": "string"}},
               },
               "required": ["stage_id"]})
    async def create_stage(stage_id: str, name: str = "", parent: str | None = None,
                           config: dict | None = None,
                           data_hashes: list[str] | None = None):
        return _ctl().create_stage(stage_id, name, parent, config, data_hashes)

    @srv.tool("mcp.experiment.rollback_to_stage",
              "Roll the experiment back to a stage (removes later stages/adapters).",
              {"type": "object",
               "properties": {"stage_id": {"type": "string"}},
               "required": ["stage_id"]})
    async def rollback_to_stage(stage_id: str):
        return _ctl().rollback_to_stage(stage_id)

    @srv.tool("mcp.experiment.export_report", "Export a markdown report for a stage (or current).",
              {"type": "object",
               "properties": {"stage_id": {"type": "string"}}})
    async def export_report(stage_id: str | None = None):
        return _ctl().export_report(stage_id)

    @srv.tool("mcp.experiment.get_config", "Get the experiment config.",
              {"type": "object", "properties": {}})
    async def get_config():
        return {"config": _ctl().get_config()}

    @srv.tool("mcp.experiment.update_config", "Update the experiment config.",
              {"type": "object",
               "properties": {"updates": {"type": "object"}},
               "required": ["updates"]})
    async def update_config(updates: dict):
        return {"config": _ctl().update_config(updates)}

    srv.resource("mcpft://experiment/state", "Experiment state JSON", "application/json",
                 lambda: _ctl().store._load().__repr__())


def build() -> ToolServer:
    srv = ToolServer(
        "mcp-finetune-experiment",
        instructions="Experiment control: create the project, manage stages, rollback, export reports.",
    )
    register(srv)
    return srv
