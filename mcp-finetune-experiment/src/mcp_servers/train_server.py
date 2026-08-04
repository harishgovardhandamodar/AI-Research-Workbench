"""``mcp.train.*`` tools: incremental training (stage-based, LoRA-first)."""

from __future__ import annotations

from experiment.store import ExperimentStore
from training.trainer import REAL_AVAILABLE, Trainer
from .base import ToolServer
from .project import project_dir


def register(srv: ToolServer) -> None:
    """Register this category's tools onto a ToolServer (own or shared)."""
    def _trainer():
        store = ExperimentStore(project_dir())
        return Trainer(store, project_dir(), store.get_config())

    @srv.tool("mcp.train.start_stage",
              "Start a new fine-tuning stage from a previous adapter/checkpoint.",
              {"type": "object",
               "properties": {
                   "stage_id": {"type": "string"},
                   "from_adapter": {"type": "string"},
                   "new_data": {"type": "string"},
                   "epochs": {"type": "number"},
                   "lr": {"type": "number"},
                   "lora_rank": {"type": "integer"},
                   "method": {"type": "string"},
               },
               "required": ["stage_id"]})
    async def start_stage(stage_id: str, from_adapter: str | None = None,
                          new_data: str | None = None, epochs: float | None = None,
                          lr: float | None = None, lora_rank: int | None = None,
                          method: str | None = None):
        return await _trainer().start_stage(stage_id, from_adapter, new_data,
                                            epochs, lr, lora_rank, method)

    @srv.tool("mcp.train.resume", "Continue an interrupted training run.",
              {"type": "object",
               "properties": {"stage_id": {"type": "string"},
                              "steps": {"type": "integer", "default": 20}},
               "required": ["stage_id"]})
    async def resume(stage_id: str, steps: int = 20):
        return await _trainer().resume(stage_id, steps)

    @srv.tool("mcp.train.list_checkpoints", "List all saved checkpoints.",
              {"type": "object", "properties": {}})
    async def list_checkpoints():
        return {"checkpoints": _trainer().list_checkpoints()}

    @srv.tool("mcp.train.load_adapter", "Load an adapter and report where it is on disk.",
              {"type": "object",
               "properties": {"adapter_id": {"type": "string"}},
               "required": ["adapter_id"]})
    async def load_adapter(adapter_id: str):
        return _trainer().load_adapter(adapter_id)

    @srv.tool("mcp.train.get_status", "Get a stage's training status and progress.",
              {"type": "object",
               "properties": {"stage_id": {"type": "string"}},
               "required": ["stage_id"]})
    async def get_status(stage_id: str):
        return _trainer().get_status(stage_id)

    @srv.tool("mcp.train.get_metrics", "Get a stage's final training metrics + loss curve.",
              {"type": "object",
               "properties": {"stage_id": {"type": "string"}},
               "required": ["stage_id"]})
    async def get_metrics(stage_id: str):
        return _trainer().get_metrics(stage_id)

    @srv.tool("mcp.train.set_hyperparams",
              "Change LR, rank, epochs, method etc. between stages.",
              {"type": "object",
               "properties": {"updates": {"type": "object"}},
               "required": ["updates"]})
    async def set_hyperparams(updates: dict):
        return {"training": _trainer().set_hyperparams(updates),
                "real_mode_available": REAL_AVAILABLE}

    srv.resource("mcpft://training/mode", "Training mode", "text/plain",
                 lambda: "real" if REAL_AVAILABLE else "simulate")


def build() -> ToolServer:
    srv = ToolServer(
        "mcp-finetune-train",
        instructions="Incremental fine-tuning tools (LoRA/QLoRA by default).",
    )
    register(srv)
    return srv