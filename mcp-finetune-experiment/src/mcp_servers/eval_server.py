"""``mcp.eval.*`` tools: evaluation harness + paper comparison."""

from __future__ import annotations

from eval.harness import EvalHarness
from experiment.store import ExperimentStore
from .base import ToolServer
from .project import project_dir


def register(srv: ToolServer) -> None:
    """Register this category's tools onto a ToolServer (own or shared)."""
    def _harness():
        store = ExperimentStore(project_dir())
        return EvalHarness(store, project_dir(), store.get_config())

    @srv.tool("mcp.eval.run", "Run an evaluation (full or a subset) for a stage.",
              {"type": "object",
               "properties": {
                   "stage_id": {"type": "string"},
                   "split": {"type": "string"},
                   "subset": {"type": "integer"},
                   "seed": {"type": "integer", "default": 0},
               },
               "required": ["stage_id"]})
    async def run(stage_id: str, split: str | None = None, subset: int | None = None,
                  seed: int = 0):
        return _harness().run(stage_id, split, subset, seed)

    @srv.tool("mcp.eval.compare_to_paper",
              "Side-by-side comparison of a stage's metrics against the authors' numbers.",
              {"type": "object",
               "properties": {"stage_id": {"type": "string"}},
               "required": ["stage_id"]})
    async def compare_to_paper(stage_id: str):
        return _harness().compare_to_paper(stage_id)

    @srv.tool("mcp.eval.failure_cases", "List a stage's failure cases.",
              {"type": "object",
               "properties": {"stage_id": {"type": "string"},
                              "top_k": {"type": "integer", "default": 10}},
               "required": ["stage_id"]})
    async def failure_cases(stage_id: str, top_k: int = 10):
        return _harness().failure_cases(stage_id, top_k)

    @srv.tool("mcp.eval.llm_judge", "Judge model outputs with a configurable LLM judge.",
              {"type": "object",
               "properties": {
                   "stage_id": {"type": "string"},
                   "judge_model": {"type": "string"},
                   "samples": {"type": "integer", "default": 20},
               },
               "required": ["stage_id"]})
    async def llm_judge(stage_id: str, judge_model: str | None = None, samples: int = 20):
        return await _harness().llm_judge(stage_id, judge_model, samples)


def build() -> ToolServer:
    srv = ToolServer(
        "mcp-finetune-eval",
        instructions="Evaluation tools: run evals, compare to the paper, failure cases, LLM judge.",
    )
    register(srv)
    return srv
