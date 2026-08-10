"""ft-validate MCP server entrypoint.

Exposes the outcome-oriented verification surface (10 tools):

    build_rag_index → generate_eval_set → load_models →
    run_rag_verification (async) → get_validation_report →
    compare_base_vs_adapter → suggest_improvements

Plus standalone scoring (``score_answers``), listing, and Unsloth config export.

Resources: ``rag://index/{id}/stats``, ``evalsets://{id}/preview``,
``runs://{id}/report``, ``configs://unsloth/{template}``.

Prompts: ``rag_faithfulness_judge``, ``generate_hard_questions``,
``diagnose_forgetting``.

Run standalone: ``python -m mcp_servers.ft_validate.server``
"""

from __future__ import annotations

import json
from typing import Any

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from . import __version__
from . import configs, evalset, inference, rag, reports, verify
from .store import ValidateStore

mcp = MCPServer("ft-validate", version=__version__,
                instructions=(
                    "Verifies a LoRA/QLoRA adapter against the original "
                    "artifacts using local RAG. Workflow: build_rag_index → "
                    "generate_eval_set → load_models → run_rag_verification → "
                    "get_validation_report → suggest_improvements."))
RO = ToolAnnotations(read_only_hint=True)


def _store() -> ValidateStore:
    return ValidateStore()


def _out(**data: Any) -> str:
    return json.dumps({"ok": True, **data}, default=str)


def _err(exc: Exception, recovery: str = "") -> str:
    return json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}",
                       "recovery": recovery}, default=str)


# ------------------------------------------------------------------- tools ----

@mcp.tool(annotations=RO)
def health() -> str:
    """Check the workspace and available backends (unsloth, torch, transformers,
    sentence-transformers, ollama)."""
    return _out(workspace=str(_store().root), backend=inference.backend_status())


@mcp.tool()
def build_rag_index(chunks: list[dict], index_id: str = "",
                    embedding_model: str = "auto", root: str = "") -> str:
    """Build (or update) a persistent local RAG index from source chunks.
    Each chunk needs 'id' + 'text' (+ optional 'source_path'/'metadata'). Build
    the index from the ORIGINAL artifacts, not the training JSONL, to avoid
    leakage. Pass an optional root to enforce path containment."""
    try:
        return _out(**rag.build_rag_index(_store(), chunks, index_id,
                                          embedding_model, root or None))
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool(annotations=RO)
def list_rag_indexes() -> str:
    """List RAG indexes in the workspace (id, embedding model, chunk count)."""
    try:
        return _out(**rag.list_rag_indexes(_store()))
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool(annotations=RO)
def retrieve(index_id: str, question: str, top_k: int = 5) -> str:
    """Retrieve the top-k most relevant chunks for a question from a RAG index
    (returns chunk ids, scores, snippets, sources)."""
    try:
        hits = rag.retrieve(_store(), index_id, question, top_k)
        return _out(query=question, hits=hits, count=len(hits))
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool()
def generate_eval_set(index_id: str, mode: str = "heldout", n: int = 20,
                      difficulty: str = "medium",
                      local_llm_model: str = "auto",
                      eval_set_id: str | None = None) -> str:
    """Generate a held-out evaluation set over the source index: heldout
    (deterministic, offline), synthetic (local LLM), or hard (multi-hop
    cross-chunk). Returns an eval_set_id to pass to run_rag_verification."""
    try:
        return _out(**evalset.generate_eval_set(
            _store(), index_id, mode=mode, n=n, difficulty=difficulty,
            local_llm_model=local_llm_model, eval_set_id=eval_set_id))
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool()
def generate_custom_eval_set(index_id: str, questions: list[str] | None = None,
                             mine_transcripts: bool = True, n: int = 12,
                             eval_set_id: str | None = None,
                             topics: list[str] | None = None) -> str:
    """Build a small eval set to TEST the finetuned LLM with custom questions.
    Pass your own questions (e.g. about QUAI/QI, crypto assets) and/or let it
    mine sample queries from the corpus — including diarized interview
    transcripts. Returns an eval_set_id for run_rag_verification."""
    try:
        return _out(**evalset.generate_custom_eval_set(
            _store(), index_id, questions=questions,
            mine_transcripts=mine_transcripts, n=n,
            eval_set_id=eval_set_id, topics=topics))
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool(annotations=RO)
def preview_eval_set(eval_set_id: str, n: int = 10) -> str:
    """Preview questions from an eval set (with gold answers + evidence)."""
    try:
        return _out(**evalset.preview_eval_set(_store(), eval_set_id, n))
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool(annotations=RO)
def list_eval_sets() -> str:
    """List eval sets (id, mode, question count)."""
    try:
        return _out(**evalset.list_eval_sets(_store()))
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool()
def load_models(base_model: str, adapter_path: str | None = None,
                unsloth_config: dict | None = None,
                model_id: str = "") -> str:
    """Validate a base model + optional PEFT adapter and register model handles.
    Returns model_ids to pass to run_rag_verification plus backend availability.
    Falls back to evidence-based generation when no local model backend exists."""
    try:
        return _out(**inference.load_models(_store(), base_model, adapter_path,
                                            unsloth_config, model_id))
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool()
def run_rag_verification(eval_set_id: str, index_id: str,
                         model_ids: list[str] | None = None,
                         base_model: str = "", adapter_path: str = "",
                         top_k: int = 5, judge_model: str = "",
                         generation: str = "", max_new_tokens: int = 256) -> str:
    """Run RAG-grounded verification (base vs adapter) as an async job. Returns
    a run_id to poll with get_validation_report. Each question is retrieved
    against the index, answered by each model, and scored for faithfulness,
    accuracy, hallucination and retention."""
    try:
        return _out(**verify.run_rag_verification(
            _store(), eval_set_id, index_id, model_ids, base_model,
            adapter_path, top_k, judge_model, generation, max_new_tokens))
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool(annotations=RO)
def get_validation_report(run_id: str) -> str:
    """Poll a verification run: status, aggregate metrics, failure cases, log."""
    try:
        return _out(**verify.get_validation_report(_store(), run_id))
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool(annotations=RO)
def list_validation_runs() -> str:
    """List verification runs (id, status, eval set, models, dates)."""
    try:
        return _out(**verify.list_validation_runs(_store()))
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool()
def compare_base_vs_adapter(run_id: str, report_format: str = "both") -> str:
    """Summarize base vs adapter deltas for a finished run and materialize a
    JSON/Markdown report in the workspace. Returns the report paths + content."""
    try:
        return _out(**reports.compare_base_vs_adapter(
            _store(), run_id, report_format))
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool()
def score_answers(predictions: list[dict],
                  metrics: list[str] | None = None) -> str:
    """Score a batch of predictions against evidence/gold. Each prediction:
    {"answer", "evidence": [..], "gold": ""}."""
    try:
        from . import scoring
        out = []
        for p in predictions:
            out.append({"scores": scoring.score_answer(
                p.get("answer", ""), p.get("evidence") or [], p.get("gold", ""),
                metrics)})
        return _out(scored=out, count=len(out))
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool(annotations=RO)
def suggest_improvements(run_id: str) -> str:
    """Analyze a finished run's failures and recommend data/hyperparameter
    changes (e.g. 'add more examples about policy section X')."""
    try:
        return _out(**reports.suggest_improvements(_store(), run_id))
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool(annotations=RO)
def export_unsloth_config(template: str = "qlora_7b_default",
                          overrides: dict | None = None,
                          as_json: bool = False) -> str:
    """Export a ready-to-use Unsloth training snippet or JSON config. Templates:
    qlora_7b_default, qlora_high_capacity, continued_pretrain. Optional
    overrides are validated."""
    try:
        return _out(**configs.export_unsloth_config(template, overrides, as_json))
    except Exception as e:  # noqa: BLE001
        return _err(e)


# ------------------------------------------------------------- resources ----

@mcp.resource("rag://index/{id}/stats")
def rag_stats(id: str) -> str:
    """RAG index stats (chunk count, embedding model, sources)."""
    try:
        return json.dumps(rag.rag_stats(_store(), id), indent=2, default=str)
    except Exception as e:  # noqa: BLE001
        return json.dumps({"error": str(e)})


@mcp.resource("evalsets://{id}/preview")
def evalset_preview(id: str) -> str:
    """Preview of an eval set's questions."""
    try:
        return json.dumps(evalset.preview_eval_set(_store(), id, 10), indent=2,
                          default=str)
    except Exception as e:  # noqa: BLE001
        return json.dumps({"error": str(e)})


@mcp.resource("runs://{id}/report")
def run_report(id: str) -> str:
    """Validation report for a run (aggregate + failures)."""
    try:
        return json.dumps(verify.get_validation_report(_store(), id), indent=2,
                          default=str)
    except Exception as e:  # noqa: BLE001
        return json.dumps({"error": str(e)})


@mcp.resource("configs://unsloth/{template}")
def unsloth_config_resource(template: str) -> str:
    """Unsloth training config template (JSON)."""
    try:
        cfg = configs.TEMPLATES.get(template)
        return json.dumps(cfg.model_dump() if cfg else
                          {"error": f"unknown template '{template}' (available: "
                           f"{', '.join(configs.TEMPLATES)})"}, indent=2)
    except Exception as e:  # noqa: BLE001
        return json.dumps({"error": str(e)})


# ---------------------------------------------------------------- prompts ----

@mcp.prompt(name="rag_faithfulness_judge",
            description="Judge whether an answer is supported by retrieved evidence")
def rag_faithfulness_judge(answer: str, evidence: str) -> str:
    return (
        "You are a faithfulness judge. The answer must only use facts from the "
        "provided evidence. Respond with a JSON object: {'faithful': bool, "
        "'score': 0.0..1.0, 'unsupported_claims': [..], 'note': '..'}\n\n"
        f"EVIDENCE:\n{evidence}\n\nANSWER:\n{answer}")


@mcp.prompt(name="generate_hard_questions",
            description="Write hard multi-hop evaluation questions about domain material")
def generate_hard_questions(domain_notes: str) -> str:
    return (
        "Write hard, multi-hop evaluation questions about this domain that "
        "require combining facts across passages. For each, give the answer and "
        "which passages support it. Return JSON:\n"
        "[{'question': '..', 'answer': '..', 'requires': ['..']}]\n\n"
        f"DOMAIN NOTES:\n{domain_notes}")


@mcp.prompt(name="diagnose_forgetting",
            description="Analyze whether an adapter lost general knowledge")
def diagnose_forgetting(base_answer: str, adapter_answer: str) -> str:
    return (
        "Compare the base model and the fine-tuned adapter answers to the same "
        "general-knowledge question. Determine whether the adapter shows "
        "catastrophic forgetting. Respond with JSON: {'forgetting': bool, "
        "'severity': 'none'|'mild'|'severe', 'evidence': '..', 'fix': '..'}\n\n"
        f"BASE:\n{base_answer}\n\nADAPTER:\n{adapter_answer}")


if __name__ == "__main__":
    mcp.run(transport="stdio")
