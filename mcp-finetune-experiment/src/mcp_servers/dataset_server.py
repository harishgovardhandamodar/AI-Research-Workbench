"""``mcp.dataset.*`` tools: data & dataset management."""

from __future__ import annotations

from data.pipeline import DataPipeline
from .base import ToolServer
from .project import project_dir


def register(srv: ToolServer) -> None:
    """Register this category's tools onto a ToolServer (own or shared)."""
    def _dp():
        return DataPipeline(project_dir() / "data")

    @srv.tool("mcp.dataset.list", "List all datasets with record counts and content hashes.",
              {"type": "object", "properties": {}})
    async def list_datasets():
        return {"datasets": _dp().list()}

    @srv.tool("mcp.dataset.inspect", "Inspect a dataset (schema, kinds, sample records).",
              {"type": "object",
               "properties": {"name": {"type": "string"}, "limit": {"type": "integer", "default": 10}},
               "required": ["name"]})
    async def inspect(name: str, limit: int = 10):
        return _dp().inspect(name, limit)

    @srv.tool("mcp.dataset.generate",
              "Deterministically generate synthetic trajectories / teacher tool-calls / rubric labels.",
              {"type": "object",
               "properties": {
                   "name": {"type": "string"},
                   "n_trajectories": {"type": "integer", "default": 200},
                   "n_teacher": {"type": "integer", "default": 200},
                   "n_rubric": {"type": "integer", "default": 100},
                   "seed": {"type": "integer", "default": 0},
                   "template": {"type": "object"},
               },
               "required": ["name"]})
    async def generate(name: str, n_trajectories: int = 200, n_teacher: int = 200,
                       n_rubric: int = 100, seed: int = 0, template: dict | None = None):
        return _dp().generate(name, n_trajectories, n_teacher, n_rubric, seed, template)

    @srv.tool("mcp.dataset.add_incremental",
              "Append new examples to a dataset without rebuilding everything.",
              {"type": "object",
               "properties": {
                   "name": {"type": "string"},
                   "records": {"type": "array", "items": {"type": "object"}},
                   "append": {"type": "boolean", "default": True},
               },
               "required": ["name", "records"]})
    async def add_incremental(name: str, records: list[dict], append: bool = True):
        return _dp().add_incremental(name, records, append)

    @srv.tool("mcp.dataset.split", "Split a dataset into train/val/test (seeded, reproducible).",
              {"type": "object",
               "properties": {
                   "name": {"type": "string"},
                   "train_frac": {"type": "number", "default": 0.8},
                   "val_frac": {"type": "number", "default": 0.1},
                   "seed": {"type": "integer", "default": 0},
                   "overwrite": {"type": "boolean", "default": False},
               },
               "required": ["name"]})
    async def split(name: str, train_frac: float = 0.8, val_frac: float = 0.1,
                    seed: int = 0, overwrite: bool = False):
        return _dp().split(name, train_frac, val_frac, seed, overwrite)

    @srv.tool("mcp.dataset.validate", "Validate a dataset's schema and content hash.",
              {"type": "object",
               "properties": {"name": {"type": "string"}},
               "required": ["name"]})
    async def validate(name: str):
        return _dp().validate(name)


def build() -> ToolServer:
    srv = ToolServer(
        "mcp-finetune-dataset",
        instructions="Data & dataset tools for the incremental fine-tuning experiment.",
    )
    register(srv)
    return srv
