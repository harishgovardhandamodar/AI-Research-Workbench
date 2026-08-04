"""Hyperparameter helpers: validate and merge training settings."""

from __future__ import annotations

VALID_METHODS = {"lora", "qlora", "full", "rft", "grpo", "decoupled"}

_KEYS = {
    "method": (str, VALID_METHODS),
    "lora_rank": (int, None),
    "lora_alpha": (int, None),
    "learning_rate": (float, None),
    "epochs": (float, None),
    "batch_size": (int, None),
    "max_seq_len": (int, None),
    "quantization": (type(None), None),
    "warmup_steps": (int, None),
    "weight_decay": (float, None),
    "real": (bool, None),
}


def apply_hyperparams(training_cfg: dict, updates: dict) -> dict:
    """Merge + validate a hyperparameter update; raise on unknown/invalid keys."""
    out = dict(training_cfg)
    for key, value in (updates or {}).items():
        if key not in _KEYS:
            raise ValueError(f"unknown hyperparameter: {key}")
        expected, choices = _KEYS[key]
        if value is not None and not isinstance(value, expected):
            raise ValueError(f"hyperparameter {key} must be {expected.__name__}")
        if choices and value not in choices:
            raise ValueError(f"hyperparameter {key} must be one of {sorted(choices)}")
        out[key] = value
    return out
