"""Shared low-level helpers: JSON I/O, content hashing, seeded randomness.

Every dataset file and stage records a SHA-256 hash so the experiment is fully
reproducible (data hash + config + adapter metadata).
"""

from __future__ import annotations

import hashlib
import json
import random
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def epoch() -> float:
    return time.time()


def json_load(path: Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def json_dump(path: Path, data: Any) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str),
                    encoding="utf-8")
    return path


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def hash_object(obj: Any) -> str:
    return sha256_text(json.dumps(obj, sort_keys=True, default=str))


def rng(seed: str | int) -> random.Random:
    """A reproducible RNG derived from a string/int seed."""
    if isinstance(seed, str):
        seed = int(sha256_text(seed)[:16], 16)
    return random.Random(seed)
