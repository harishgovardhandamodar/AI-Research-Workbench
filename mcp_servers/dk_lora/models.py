"""Pydantic schemas for dk-lora: artifacts, chunks, datasets, configs, jobs."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

ChunkStrategy = Literal["semantic", "recursive", "fixed"]
DatasetMode = Literal["qa", "instruction", "continued_pretrain", "mixed"]
Quant = Literal["4bit", "8bit", "none"]


class Artifact(BaseModel):
    """A single ingested artifact (a normalized document)."""

    id: str
    path: str  # absolute source path
    file_type: str  # pdf / markdown / text / json / jsonl / csv
    title: str = ""
    size_bytes: int = 0
    created_at: float = 0.0
    metadata: dict[str, Any] = Field(default_factory=dict)
    text: str = ""


class Chunk(BaseModel):
    """A chunk of an artifact, with full provenance."""

    id: str
    artifact_id: str
    index: int
    text: str
    source_path: str = ""
    page: int | None = None
    speaker: str | None = None
    strategy: str = "recursive"


class DatasetExample(BaseModel):
    """One training example in Alpaca (or continued-pretrain text) form."""

    id: str
    mode: DatasetMode
    instruction: str = ""
    input: str = ""
    output: str = ""
    text: str = ""  # for continued_pretrain mode
    quality: float = 1.0
    provenance: dict[str, Any] = Field(default_factory=dict)


class Dataset(BaseModel):
    """A generated training dataset."""

    id: str
    mode: DatasetMode
    created_at: float = 0.0
    examples: list[DatasetExample] = Field(default_factory=list)
    quality: float = 0.0


class TrainingConfig(BaseModel):
    """Validated LoRA/QLoRA training configuration (Unsloth-first)."""

    id: str = "default"
    base_model: str = "unsloth/Meta-Llama-3.1-8B-Instruct-bnb-4bit"
    lora_r: int = 16
    lora_alpha: int = 32
    lora_dropout: float = 0.05
    bias: Literal["none", "all", "lora_only"] = "none"
    quant: Quant = "4bit"
    max_seq_length: int = 2048
    target_modules: list[str] = Field(default_factory=lambda: [
        "q_proj", "k_proj", "v_proj", "o_proj",
        "gate_proj", "up_proj", "down_proj",
    ])
    use_gradient_checkpointing: bool = True
    backend: Literal["unsloth", "trl"] = "unsloth"
    epochs: int = 2
    learning_rate: float = 2e-4
    per_device_batch_size: int = 4
    gradient_accumulation_steps: int = 4
    warmup_ratio: float = 0.1
    weight_decay: float = 0.01
    lr_scheduler_type: str = "linear"
    random_state: int = 3407
    output_dir: str = ""

    def validate_config(self) -> tuple[bool, str]:
        """Return (ok, error). Actionable message when invalid."""
        if not self.base_model.strip():
            return False, "base_model is required (e.g. unsloth/Meta-Llama-3.1-8B-Instruct-bnb-4bit)"
        if self.lora_r < 1 or self.lora_r > 256:
            return False, f"lora_r must be in 1..256 (got {self.lora_r})"
        if self.lora_alpha < 1:
            return False, f"lora_alpha must be >= 1 (got {self.lora_alpha})"
        if not (0 < self.lora_dropout < 1):
            return False, f"lora_dropout must be in (0, 1) (got {self.lora_dropout})"
        if self.epochs < 1:
            return False, f"epochs must be >= 1 (got {self.epochs})"
        if not (0 < self.learning_rate <= 1):
            return False, f"learning_rate must be in (0, 1] (got {self.learning_rate})"
        if self.max_seq_length < 256:
            return False, f"max_seq_length must be >= 256 (got {self.max_seq_length})"
        if self.backend not in ("unsloth", "trl"):
            return False, f"backend must be 'unsloth' or 'trl' (got {self.backend})"
        if not self.target_modules:
            return False, "target_modules cannot be empty"
        return True, ""


class JobRecord(BaseModel):
    """A training (or other long-running) job."""

    id: str
    kind: str = "training"
    status: Literal["queued", "running", "done", "failed", "cancelled"] = "queued"
    created_at: float = 0.0
    updated_at: float = 0.0
    config: dict[str, Any] = Field(default_factory=dict)
    log_path: str = ""
    output_dir: str = ""
    error: str = ""
    result: dict[str, Any] = Field(default_factory=dict)
