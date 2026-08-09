"""dk-lora MCP server entrypoint.

Exposes the outcome-oriented tool surface (≈13 tools) that takes a local folder
of artifacts and produces a usable LoRA adapter for a local chatbot:

    ingest_artifacts → chunk_artifacts → generate_dataset →
    configure_training → start_training (poll) → export_adapter →
    register_for_chatbot / chat_with_adapter

Resources (read-only context): ``artifacts://list``, ``artifacts://{id}/content``,
``artifacts://{id}/chunks``, ``datasets://{id}/preview``, ``jobs://{id}/status``,
``adapters://{id}/info``, ``configs://{id}``.

Prompts: ``prepare_domain_qa``, ``critique_training_example``,
``suggest_training_hyperparams``.

Run standalone: ``python -m mcp_servers.dk_lora.server``
"""

from __future__ import annotations

import json
from typing import Any

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from . import __version__
from . import chunking, dataset, export as export_mod, ingest, training
from .models import TrainingConfig
from .store import Workspace

mcp = MCPServer("dk-lora", version=__version__,
                instructions=(
                    "Turns local artifacts (PDFs, transcripts, policies, blog "
                    "extracts, small datasets) into a LoRA/QLoRA adapter for a "
                    "local chatbot. Workflow: ingest_artifacts → "
                    "chunk_artifacts → generate_dataset → configure_training → "
                    "start_training → get_training_status → export_adapter → "
                    "register_for_chatbot."))
RO = ToolAnnotations(read_only_hint=True)


def _ws() -> Workspace:
    return Workspace()


def _out(**data: Any) -> str:
    """Serialize a tool result to a JSON string (MCP tools return text)."""
    return json.dumps({"ok": True, **data}, default=str)


def _err(exc: Exception, recovery: str = "") -> str:
    return json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}",
                       "recovery": recovery}, default=str)


# ------------------------------------------------------------------- tools ----

@mcp.tool(annotations=RO)
def health() -> str:
    """Check the workspace, available training backends and optional heavy
    dependencies (unsloth, torch, sentence-transformers, pymupdf, ollama)."""
    import importlib.util

    def has(mod: str) -> bool:
        return importlib.util.find_spec(mod) is not None

    ws = _ws()
    return _out(
        workspace=str(ws.root),
        unsloth=has("unsloth"),
        torch=has("torch"),
        sentence_transformers=has("sentence_transformers"),
        pymupdf=has("fitz"),
        counts={"artifacts": len(ws.list_entries("artifacts")),
                "chunks": len(ws.list_entries("chunks")),
                "datasets": len(ws.list_entries("datasets")),
                "jobs": len(ws.list_entries("jobs"))},
    )


@mcp.tool()
def ingest_artifacts(path: str, recursive: bool = True) -> str:
    """Scan a local directory (or single file) and normalize supported artifacts
    (PDF, Markdown, text, JSON/JSONL, CSV, diarized transcripts). Stores them in
    the workspace catalog with provenance. Returns a summary of ingested and
    skipped files."""
    try:
        return _out(**ingest.ingest_artifacts(_ws(), path, recursive))
    except Exception as e:  # noqa: BLE001
        return _err(e, "Check the path exists and is readable; only local paths "
                       "are allowed.")


@mcp.tool(annotations=RO)
def list_artifacts(filter: str = "") -> str:
    """List every artifact in the workspace catalog (title/path/type, size,
    date). Pass an optional substring to filter."""
    try:
        return _out(**ingest.list_artifacts(_ws(), filter))
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool(annotations=RO)
def get_artifact_metadata(artifact_id: str) -> str:
    """Return full metadata + a text preview for one artifact, including
    diarization info (speakers, turn count) for transcripts."""
    try:
        return _out(**ingest.get_artifact_metadata(_ws(), artifact_id))
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool()
def chunk_artifacts(artifact_ids: list[str] | None = None,
                    strategy: str = "recursive",
                    chunk_size: int = 512, overlap: int = 64) -> str:
    """Chunk artifacts (default: all) with semantic/recursive/fixed strategy.
    Produces overlapping chunks that keep provenance (page, speaker, source).
    Call before generate_dataset."""
    try:
        return _out(**chunking.chunk_artifacts(
            _ws(), artifact_ids, strategy, chunk_size, overlap))
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool()
def generate_dataset(mode: str = "mixed", num_pairs_per_chunk: int = 3,
                     use_local_llm: bool = True,
                     quality_threshold: float = 0.7,
                     dataset_id: str | None = None,
                     local_llm_model: str = "auto",
                     artifact_ids: list[str] | None = None) -> str:
    """Synthesize a training dataset from chunks. Modes: qa, instruction,
    continued_pretrain, mixed (default). Uses a local LLM (Ollama) when
    available and falls back to offline templates. Applies a quality gate
    (length, diversity, provenance) and returns a dataset_id."""
    try:
        return _out(**dataset.generate_dataset(
            _ws(), mode=mode, num_pairs_per_chunk=num_pairs_per_chunk,
            use_local_llm=use_local_llm, quality_threshold=quality_threshold,
            dataset_id=dataset_id, local_llm_model=local_llm_model,
            artifact_ids=artifact_ids))
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool(annotations=RO)
def preview_dataset(dataset_id: str, n: int = 10) -> str:
    """Preview n examples from a generated dataset (mode, quality, provenance)."""
    try:
        return _out(**dataset.preview_dataset(_ws(), dataset_id, n))
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool(annotations=RO)
def evaluate_dataset_quality(dataset_id: str) -> str:
    """Score a dataset: count, avg quality, output lengths, uniqueness,
    provenance coverage and mode mix. Use before training to sanity-check."""
    try:
        return _out(**dataset.evaluate_dataset_quality(_ws(), dataset_id))
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool()
def configure_training(base_model: str = "unsloth/Meta-Llama-3.1-8B-Instruct-bnb-4bit",
                       lora_r: int = 16, lora_alpha: int = 32,
                       lora_dropout: float = 0.05,
                       quant: str = "4bit", epochs: int = 2,
                       learning_rate: float = 2e-4,
                       max_seq_length: int = 2048,
                       backend: str = "unsloth",
                       per_device_batch_size: int = 4,
                       output_dir: str = "",
                       config_id: str | None = None) -> str:
    """Build + validate a training config for the given GPU/hardware. Returns a
    config_id (pass to start_training). Defaults target a single consumer GPU
    (RTX 3090/4090 class, 7B-8B QLoRA)."""
    try:
        from .models import Quant
        if quant not in Quant.__args__:
            raise ValueError(f"quant must be one of {Quant.__args__}")
        if backend not in ("unsloth", "trl"):
            raise ValueError("backend must be 'unsloth' or 'trl'")
        cfg = TrainingConfig(
            id=config_id or "default",
            base_model=base_model, lora_r=lora_r, lora_alpha=lora_alpha,
            lora_dropout=lora_dropout, quant=quant, epochs=epochs,
            learning_rate=learning_rate, max_seq_length=max_seq_length,
            backend=backend, per_device_batch_size=per_device_batch_size,
            output_dir=output_dir)
        ws = _ws()
        summary = training.configure_training(ws, cfg)
        return _out(**summary)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool()
def start_training(config_id: str = "", dataset_id: str = "",
                   output_dir: str = "", toy: bool = False,
                   config: dict | None = None) -> str:
    """Start LoRA/QLoRA training in the background (Unsloth preferred, TRL
    fallback). Pass a config_id from configure_training (or an inline config
    dict). Returns a job_id to poll with get_training_status. Set toy=True for a
    CPU-only smoke run."""
    try:
        return _out(**training.start_training(
            _ws(), config_id=config_id, dataset_id=dataset_id,
            output_dir=output_dir, toy=toy, config=config))
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool(annotations=RO)
def get_training_status(job_id: str) -> str:
    """Poll a training job: status (queued/running/done/failed/cancelled), the
    tail of its log, output dir and any error."""
    try:
        return _out(**training.get_training_status(_ws(), job_id))
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool()
def cancel_training(job_id: str) -> str:
    """Cancel a queued/running training job (best-effort process kill)."""
    try:
        return _out(**training.cancel_training(_ws(), job_id))
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool(annotations=RO)
def list_jobs() -> str:
    """List all training jobs (id, kind, status, output dir, dates)."""
    try:
        return _out(**training.list_jobs(_ws()))
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool()
def export_adapter(job_id: str, format: str = "peft", merge: bool = False) -> str:
    """Export a finished training job: peft (default, adapter dir + README),
    merged (base+adapter via Unsloth), or gguf (Modelfile + convert script)."""
    try:
        return _out(**export_mod.export_adapter(_ws(), job_id, format, merge))
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool()
def register_for_chatbot(job_id: str, name: str = "dk-lora",
                         modelfile_path: str = "", ollama: bool = True) -> str:
    """Write an Ollama Modelfile for a finished job and optionally register it
    with the local Ollama daemon so the chatbot can use it."""
    try:
        return _out(**export_mod.register_for_chatbot(
            _ws(), job_id, name=name, modelfile_path=modelfile_path,
            ollama=ollama))
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool()
def chat_with_adapter(message: str, adapter_path: str,
                      system_prompt: str | None = None,
                      endpoint: str = "", model_name: str = "dk-lora") -> str:
    """Ask the local chatbot endpoint (Ollama default) to answer with the
    adapter. The adapter must be registered first (register_for_chatbot)."""
    try:
        return _out(**export_mod.chat_with_adapter(
            message, adapter_path, system_prompt, endpoint, model_name))
    except Exception as e:  # noqa: BLE001
        return _err(e)


# ------------------------------------------------------------- resources ----

@mcp.resource("artifacts://list")
def artifacts_list() -> str:
    """Catalog of ingested artifacts."""
    ws = _ws()
    return json.dumps(ws.list_artifacts(), indent=2, default=str)


@mcp.resource("artifacts://{id}/content")
def artifact_content(id: str) -> str:
    """Full normalized text of one artifact."""
    art = _ws().get_artifact(id)
    return art.text if art else json.dumps({"error": f"artifact not found: {id}"})


@mcp.resource("artifacts://{id}/chunks")
def artifact_chunks(id: str) -> str:
    """Chunks for one artifact."""
    ws = _ws()
    chunks = ws.list_chunks([id])
    return json.dumps([c.model_dump() for c in chunks], indent=2, default=str)


@mcp.resource("datasets://{id}/preview")
def dataset_preview(id: str) -> str:
    """Preview of a generated dataset."""
    return json.dumps(dataset.preview_dataset(_ws(), id, 20), indent=2, default=str)


@mcp.resource("jobs://{id}/status")
def job_status(id: str) -> str:
    """Status of a training job."""
    return json.dumps(training.get_training_status(_ws(), id), indent=2, default=str)


@mcp.resource("adapters://{id}/info")
def adapter_info(id: str) -> str:
    """Info about a finished job's adapter (dir, files)."""
    job = _ws().get_job(id)
    if not job:
        return json.dumps({"error": f"job not found: {id}"})
    return json.dumps({"job_id": id, "status": job.status,
                       "output_dir": job.output_dir,
                       "config": job.config, "result": job.result},
                      indent=2, default=str)


@mcp.resource("configs://{id}")
def config_info(id: str) -> str:
    """Validated training config."""
    cfg = _ws().get_config(id)
    return json.dumps(cfg.model_dump() if cfg else {"error": f"config not found: {id}"},
                      indent=2, default=str)


# ---------------------------------------------------------------- prompts ----

@mcp.prompt(name="prepare_domain_qa",
            description="Guide high-quality Q&A generation from domain material")
def prepare_domain_qa(artifact_summary: str) -> str:
    return (dataset.PROMPTS["prepare_domain_qa"] +
            f"\n\nARTIFACT SUMMARY:\n{artifact_summary}")


@mcp.prompt(name="critique_training_example",
            description="Critique a training example for quality/faithfulness")
def critique_training_example(example: str) -> str:
    return (dataset.PROMPTS["critique_training_example"] +
            f"\n\nEXAMPLE:\n{example}")


@mcp.prompt(name="suggest_training_hyperparams",
            description="Suggest LoRA/QLoRA hyperparameters for hardware + data size")
def suggest_training_hyperparams(hardware: str, dataset_size: str) -> str:
    return (
        f"Suggest LoRA/QLoRA hyperparameters (r, alpha, epochs, lr, batch, "
        f"max_seq_length) for:\n- Hardware: {hardware}\n- Dataset size: "
        f"{dataset_size}\n\nReturn concrete numbers with one-line "
        f"justifications. Prefer r=16/alpha=32, epochs=2, lr=2e-4 for a "
        f"7B-8B model on an RTX 3090/4090.")


if __name__ == "__main__":
    mcp.run(transport="stdio")
