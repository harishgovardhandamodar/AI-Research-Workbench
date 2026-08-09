"""Unsloth configuration templates + export for ft-validate.

These are first-class citizens: battle-tested starting points exposed as
resources (``configs://unsloth/{template}``) and via the
``export_unsloth_config`` tool, which renders a ready-to-paste Python snippet.
"""

from __future__ import annotations

import json

from .models import UnslothConfig

# name -> UnslothConfig (battle-tested starting points)
TEMPLATES: dict[str, UnslothConfig] = {
    "qlora_7b_default": UnslothConfig(
        name="qlora_7b_default",
        base_model="unsloth/Meta-Llama-3.1-8B-Instruct-bnb-4bit",
        r=16, lora_alpha=32, lora_dropout=0.05, quant="4bit",
        max_seq_length=2048, use_gradient_checkpointing=True,
        epochs=2, learning_rate=2e-4,
    ),
    "qlora_high_capacity": UnslothConfig(
        name="qlora_high_capacity",
        base_model="unsloth/Meta-Llama-3.1-8B-Instruct-bnb-4bit",
        r=64, lora_alpha=128, lora_dropout=0.05, quant="4bit",
        max_seq_length=2048, use_gradient_checkpointing=True,
        epochs=3, learning_rate=1.5e-4,
    ),
    "continued_pretrain": UnslothConfig(
        name="continued_pretrain",
        base_model="unsloth/Meta-Llama-3.1-8B-Instruct-bnb-4bit",
        r=16, lora_alpha=32, lora_dropout=0.05, quant="4bit",
        max_seq_length=4096, use_gradient_checkpointing=True,
        epochs=2, learning_rate=1e-5,
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj",
                        "gate_proj", "up_proj", "down_proj", "embed_tokens",
                        "lm_head"],
    ),
}


def list_templates() -> dict:
    return {"templates": [t.model_dump() for t in TEMPLATES.values()]}


def _apply_overrides(template: UnslothConfig,
                     overrides: dict | None) -> UnslothConfig:
    cfg = template.model_copy(deep=True)
    for k, v in (overrides or {}).items():
        if not hasattr(cfg, k):
            raise ValueError(f"unknown override key '{k}' (valid: "
                             f"{sorted(cfg.model_fields)})")
        setattr(cfg, k, v)
    if not cfg.base_model.strip():
        raise ValueError("base_model cannot be empty")
    if cfg.r < 1 or cfg.r > 256:
        raise ValueError(f"r must be in 1..256 (got {cfg.r})")
    if cfg.lora_alpha < 1:
        raise ValueError(f"lora_alpha must be >= 1 (got {cfg.lora_alpha})")
    return cfg


def export_unsloth_config(template: str = "qlora_7b_default",
                          overrides: dict | None = None,
                          as_json: bool = False) -> dict:
    """Return a ready-to-use Unsloth training snippet (or JSON config) for a
    template with optional overrides. Validates adapter-compatible settings."""
    tpl = TEMPLATES.get(template)
    if tpl is None:
        raise ValueError(f"unknown template '{template}' (available: "
                         f"{', '.join(TEMPLATES)})")
    cfg = _apply_overrides(tpl, overrides)
    if as_json:
        return {"template": cfg.name, "config": cfg.model_dump()}

    q4 = "load_in_4bit=True" if cfg.quant == "4bit" else (
        "load_in_8bit=True" if cfg.quant == "8bit" else "load_in_4bit=False")
    targets = json.dumps(cfg.target_modules)
    snippet = f'''\
# Template: {cfg.name} (ft-validate export)
from unsloth import FastLanguageModel

model, tokenizer = FastLanguageModel.from_pretrained(
    model_name = "{cfg.base_model}",
    max_seq_length = {cfg.max_seq_length},
    dtype = None,
    {q4},
)
model = FastLanguageModel.get_peft_model(
    model,
    r = {cfg.r},
    target_modules = {targets},
    lora_alpha = {cfg.lora_alpha},
    lora_dropout = {cfg.lora_dropout},
    bias = "{cfg.bias}",
    use_gradient_checkpointing = "unsloth" if {str(cfg.use_gradient_checkpointing)} else False,
    random_state = {cfg.random_state},
)
FastLanguageModel.for_training(model)

# Training args
#   num_train_epochs = {cfg.epochs}
#   learning_rate = {cfg.learning_rate:g}
# (see dk-lora start_training for a full runnable pipeline)
'''
    return {"template": cfg.name, "snippet": snippet, "config": cfg.model_dump(),
            "hint": "Validate the adapter against this config with "
                    "run_rag_verification after training."}
