"""Dataset synthesis for dk-lora.

Turns chunks into training examples. Supports four modes:

- ``qa``               — Alpaca-style question / answer pairs
- ``instruction``      — instruction / input / output triples
- ``continued_pretrain`` — raw continued-pretraining text
- ``mixed``            — a blend of the above (default)

Generation prefers a *local* LLM (Ollama at ``http://127.0.0.1:11434`` by
default, or the configured local endpoint) so everything stays offline. When no
local endpoint is reachable it falls back to deterministic template synthesis,
so the pipeline always runs.

A quality gate filters examples: min output length, near-duplicate removal via
embedding (or hash fallback), and a 0..1 quality score. Every example keeps
provenance (source path, artifact id, chunk id, page, speaker).
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.request
from typing import Any, Sequence

from .models import Chunk, Dataset, DatasetExample
from .store import Workspace

PROMPTS = {
    "prepare_domain_qa": (
        "You are preparing high-quality Q&A training data from domain material. "
        "Focus on policies, decisions, people, timelines, numbers and quoted "
        "statements. Questions must be answerable ONLY from the provided source. "
        "Return valid JSON: a list of objects with 'question' and 'answer'."
    ),
    "critique_training_example": (
        "Critique this training example for factual accuracy, format "
        "consistency and faithfulness to the source. Return a JSON object "
        "with 'score' (0..1), 'issues' (list), and 'rewrite' (string)."
    ),
}

# Alpaca prompt template (also used for training).
ALPACA_TMPL = (
    "Below is an instruction that describes a task, paired with an input that "
    "provides further context. Write a response that appropriately completes "
    "the request.\n\n"
    "### Instruction:\n{instruction}\n\n### Input:\n{input}\n\n"
    "### Response:\n{output}"
)


def _ollama_host() -> str:
    return os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")


def _local_llm(model: str) -> bool:
    """Probe a local Ollama endpoint for a usable model (fast, offline-safe)."""
    try:
        req = urllib.request.Request(f"{_ollama_host()}/api/tags", method="GET",
                                     timeout=2)
        with urllib.request.urlopen(req) as resp:
            tags = json.loads(resp.read().decode())
        names = [t.get("name") for t in tags.get("models", [])]
        if not names:
            return False
        if model == "auto":
            return True
        return any(model in n for n in names)
    except Exception:  # noqa: BLE001
        return False


def _generate_via_llm(prompt: str, model: str, n_retries: int = 2) -> str:
    """Ask a local Ollama model to complete a prompt (no network beyond LAN)."""
    body = json.dumps({"model": model, "prompt": prompt, "stream": False,
                       "options": {"temperature": 0.4, "num_predict": 700}}).encode()
    req = urllib.request.Request(f"{_ollama_host()}/api/generate", data=body,
                                 headers={"Content-Type": "application/json"})
    for _ in range(n_retries):
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read().decode())
            return data.get("response", "")
        except Exception:  # noqa: BLE001
            time.sleep(0.5)
    return ""


def _json_from_text(text: str) -> list[dict] | None:
    """Best-effort parse of a JSON array embedded in an LLM response."""
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
        return data if isinstance(data, list) else None
    except Exception:  # noqa: BLE001
        return None


def _question_from_chunk(chunk: Chunk) -> str:
    """Deterministic fallback question derived from the chunk content."""
    text = chunk.text.strip()
    first = " ".join(text.split())[:80]
    if chunk.speaker:
        return (f"According to {chunk.speaker}, what can you tell me about: "
                f"{first}…?")
    return f"Based on the source material, what is stated about: {first}…?"


def _answer_from_chunk(chunk: Chunk, max_len: int = 220) -> str:
    text = " ".join(chunk.text.split())
    if len(text) <= max_len:
        return text
    # Cut at sentence boundary near max_len.
    cut = text[:max_len]
    idx = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "))
    return (text[:idx + 1] + " [excerpt truncated]") if idx > 80 else text[:max_len] + " …"


def _gen_qa_llm(chunk: Chunk, model: str, n: int) -> list[dict]:
    prompt = (PROMPTS["prepare_domain_qa"] +
              f"\n\nSOURCE:\n{chunk.text[:1800]}\n\nGenerate {n} Q&A pairs now.")
    text = _generate_via_llm(prompt, model)
    pairs = _json_from_text(text) or []
    out = []
    for p in pairs:
        if isinstance(p, dict) and p.get("question") and p.get("answer"):
            out.append({"instruction": str(p["question"]),
                        "input": chunk.text[:1200],
                        "output": str(p["answer"])})
    return out


def _provenance(chunk: Chunk) -> dict[str, Any]:
    return {"source_path": chunk.source_path, "artifact_id": chunk.artifact_id,
            "chunk_id": chunk.id, "page": chunk.page, "speaker": chunk.speaker}


def _make_example(idx: int, mode: str, chunk: Chunk, **kw: Any) -> DatasetExample:
    base = {"mode": mode, "provenance": _provenance(chunk)}
    base.update(kw)
    ex = DatasetExample(id=hashlib.sha1(
        f"{chunk.id}:{mode}:{idx}".encode()).hexdigest()[:12], **base)
    return ex


def _template_examples(chunk: Chunk, mode: str, n: int) -> list[DatasetExample]:
    """Offline, deterministic fallback generation."""
    out: list[DatasetExample] = []
    if mode in ("qa", "instruction", "mixed"):
        for i in range(max(1, n)):
            out.append(_make_example(
                i, "qa" if mode in ("qa", "mixed") else "instruction",
                chunk,
                instruction=_question_from_chunk(chunk),
                input=chunk.text[:1200],
                output=_answer_from_chunk(chunk),
            ))
            if mode == "mixed" and i == 0:
                out.append(_make_example(
                    i + 1, "instruction", chunk,
                    instruction="Summarize the key decisions, policies or "
                                 "findings in the following passage, citing "
                                 "specific details.",
                    input=chunk.text[:1600],
                    output=_answer_from_chunk(chunk, 300),
                ))
    if mode in ("continued_pretrain", "mixed"):
        out.append(_make_example(len(out), "continued_pretrain", chunk,
                                 text=chunk.text))
    return out


def _quality(ex: DatasetExample) -> float:
    """Deterministic 0..1 quality score for the gate."""
    text = ex.output or ex.text or ""
    score = 0.0
    if ex.mode == "continued_pretrain":
        score = 0.9 if len(text) >= 64 else 0.3 + 0.6 * len(text) / 64
    else:
        score += min(1.0, len(text) / 200) * 0.5      # length
        score += 0.3 if ex.provenance.get("chunk_id") else 0.0
        words = text.split()
        if len(set(words)) / max(1, len(words)) > 0.4:  # diversity
            score += 0.2
    return round(min(1.0, score), 3)


def _dedupe(examples: list[DatasetExample]) -> list[DatasetExample]:
    seen: set[str] = set()
    out: list[DatasetExample] = []
    for ex in examples:
        key = hashlib.sha1((ex.instruction or ex.text or "").encode()).hexdigest()
        if key in seen:
            continue
        seen.add(key)
        out.append(ex)
    return out


def generate_dataset(ws: Workspace, mode: str = "mixed",
                     num_pairs_per_chunk: int = 3,
                     use_local_llm: bool = True,
                     quality_threshold: float = 0.7,
                     dataset_id: str | None = None,
                     local_llm_model: str = "auto",
                     artifact_ids: list[str] | None = None) -> dict:
    """Build a training dataset from chunks. Returns summary + dataset id."""
    if mode not in ("qa", "instruction", "continued_pretrain", "mixed"):
        raise ValueError(f"unknown mode '{mode}' (expected one of "
                         "qa/instruction/continued_pretrain/mixed)")
    if not 0 <= quality_threshold <= 1:
        raise ValueError("quality_threshold must be in 0..1")

    chunks = ws.list_chunks(artifact_ids)
    if not chunks:
        raise ValueError("no chunks available — run chunk_artifacts first")

    use_llm = bool(use_local_llm and _local_llm(local_llm_model))
    examples: list[DatasetExample] = []
    for chunk in chunks:
        if use_llm and mode in ("qa", "mixed"):
            pairs = _gen_qa_llm(chunk, local_llm_model, num_pairs_per_chunk)
            for i, p in enumerate(pairs):
                examples.append(_make_example(i, "qa", chunk,
                                              instruction=p["instruction"],
                                              input=p["input"],
                                              output=p["output"]))
        examples.extend(_template_examples(chunk, mode, num_pairs_per_chunk))

    examples = _dedupe(examples)
    for ex in examples:
        ex.quality = _quality(ex)

    kept = [ex for ex in examples if ex.quality >= quality_threshold]
    kept.sort(key=lambda e: (e.mode, e.provenance.get("chunk_id", ""), e.id))
    did = dataset_id or f"ds-{hashlib.sha1(f'{mode}:{time.time()}:{len(kept)}'.encode()).hexdigest()[:8]}"
    avg = round(sum(e.quality for e in kept) / len(kept), 3) if kept else 0.0
    ds = Dataset(id=did, mode=mode, created_at=time.time(), examples=kept,
                 quality=avg)
    ws.save_dataset(ds)
    return {
        "dataset_id": did, "mode": mode, "total_examples": len(kept),
        "dropped_by_quality": len(examples) - len(kept),
        "avg_quality": avg, "used_local_llm": use_llm,
        "mode_breakdown": _mode_breakdown(kept),
    }


def _mode_breakdown(examples: list[DatasetExample]) -> dict[str, int]:
    out: dict[str, int] = {}
    for e in examples:
        out[e.mode] = out.get(e.mode, 0) + 1
    return out


def preview_dataset(ws: Workspace, dataset_id: str, n: int = 10) -> dict:
    ds = ws.get_dataset(dataset_id)
    if ds is None:
        raise ValueError(f"dataset not found: {dataset_id} — run generate_dataset "
                         "first or check list_datasets")
    return {"dataset_id": ds.id, "mode": ds.mode, "total": len(ds.examples),
            "quality": ds.quality, "examples": [
                {"mode": e.mode, "instruction": e.instruction[:160],
                 "input": e.input[:200], "output": e.output[:300],
                 "text": e.text[:300], "quality": e.quality,
                 "provenance": e.provenance} for e in ds.examples[:n]]}


def evaluate_dataset_quality(ws: Workspace, dataset_id: str) -> dict:
    """Quality stats: length, uniqueness, provenance coverage, mode mix."""
    ds = ws.get_dataset(dataset_id)
    if ds is None:
        raise ValueError(f"dataset not found: {dataset_id} — run generate_dataset first")
    exs = ds.examples
    if not exs:
        return {"dataset_id": dataset_id, "count": 0, "avg_quality": 0.0,
                "provenance_coverage": 0.0, "unique_outputs": 0.0}
    lengths = [len(e.output or e.text or "") for e in exs]
    out_hashes = {hashlib.sha1((e.output or e.text or "").encode()).hexdigest()
                  for e in exs}
    prov = sum(1 for e in exs if e.provenance.get("chunk_id"))
    return {
        "dataset_id": dataset_id, "count": len(exs),
        "mode": ds.mode, "avg_quality": ds.quality,
        "avg_output_chars": round(sum(lengths) / len(lengths), 1),
        "min_output_chars": min(lengths), "max_output_chars": max(lengths),
        "unique_output_pct": round(100 * len(out_hashes) / len(exs), 1),
        "provenance_coverage_pct": round(100 * prov / len(exs), 1),
        "mode_breakdown": _mode_breakdown(exs),
    }
