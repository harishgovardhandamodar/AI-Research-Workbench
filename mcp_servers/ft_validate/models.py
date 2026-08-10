"""Pydantic schemas for ft-validate: eval sets, runs, scores, configs."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

EvalMode = Literal["heldout", "synthetic", "hard", "custom"]
ReportFormat = Literal["json", "markdown", "both"]
Metrics = Literal["faithfulness", "accuracy", "hallucination", "retention"]


class EvalQuestion(BaseModel):
    """A single held-out verification question with evidence pointers."""

    id: str
    question: str
    gold_answer: str = ""
    evidence_chunk_ids: list[str] = Field(default_factory=list)
    evidence_snippets: list[str] = Field(default_factory=list)
    difficulty: str = "medium"  # easy / medium / hard
    tags: list[str] = Field(default_factory=list)


class EvalSet(BaseModel):
    """A collection of verification questions over the source knowledge."""

    id: str
    mode: EvalMode
    created_at: float = 0.0
    questions: list[EvalQuestion] = Field(default_factory=list)


class AnswerRecord(BaseModel):
    """One model's answer to one question + evidence used."""

    model_id: str
    answer: str
    evidence_chunk_ids: list[str] = Field(default_factory=list)
    evidence_snippets: list[str] = Field(default_factory=list)
    latency_s: float = 0.0


class QuestionResult(BaseModel):
    """Per-question comparison for base vs adapter."""

    question_id: str
    question: str
    gold_answer: str = ""
    evidence_chunk_ids: list[str] = Field(default_factory=list)
    base: AnswerRecord | None = None
    adapter: AnswerRecord | None = None
    base_scores: dict[str, float] = Field(default_factory=dict)
    adapter_scores: dict[str, float] = Field(default_factory=dict)
    delta: dict[str, float] = Field(default_factory=dict)


class ValidationRun(BaseModel):
    """A full verification run (async, pollable)."""

    id: str
    eval_set_id: str
    base_model: str = ""
    adapter_path: str = ""
    model_ids: list[str] = Field(default_factory=list)
    status: Literal["queued", "running", "done", "failed", "cancelled"] = "queued"
    created_at: float = 0.0
    updated_at: float = 0.0
    error: str = ""
    aggregate: dict[str, Any] = Field(default_factory=dict)
    per_question: list[dict] = Field(default_factory=list)
    failures: list[dict] = Field(default_factory=list)
    report_md: str = ""
    report_path: str = ""
    log_path: str = ""


class UnslothConfig(BaseModel):
    """A validated Unsloth training config (template or custom)."""

    name: str = "qlora_7b_default"
    base_model: str = "unsloth/Meta-Llama-3.1-8B-Instruct-bnb-4bit"
    r: int = 16
    lora_alpha: int = 32
    lora_dropout: float = 0.05
    bias: str = "none"
    quant: str = "4bit"
    max_seq_length: int = 2048
    target_modules: list[str] = Field(default_factory=lambda: [
        "q_proj", "k_proj", "v_proj", "o_proj",
        "gate_proj", "up_proj", "down_proj",
    ])
    use_gradient_checkpointing: bool = True
    epochs: int = 2
    learning_rate: float = 2e-4
    random_state: int = 3407
