"""Custom skills registry for the Fox agent.

Skills are lightweight, user-defined capabilities (name, description, and an
instruction the agent can follow). They persist in <workbench>/skills.json (the
persistent volume) and their instructions are injected into the agent's context
each turn so the agent can act on them.

Bundled capabilities (example experiment scripts + notebooks) are listed
separately by the Agent dashboard.
"""

from __future__ import annotations

import datetime
import json
import uuid
from pathlib import Path

from ..paths import WORKBENCH_DIR

SKILLS_FILE = WORKBENCH_DIR / "skills.json"


def _write(skills: list[dict]) -> None:
    SKILLS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SKILLS_FILE.write_text(json.dumps(skills, indent=2))


def load_skills() -> list[dict]:
    if not SKILLS_FILE.exists():
        return []
    try:
        data = json.loads(SKILLS_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    return data if isinstance(data, list) else []


def add_skill(name: str, description: str, instruction: str = "") -> dict:
    name = (name or "").strip()
    if not name:
        raise ValueError("skill name required")
    skills = load_skills()
    if any(s.get("name", "").lower() == name.lower() for s in skills):
        raise ValueError(f"a skill named '{name}' already exists")
    skill = {
        "id": uuid.uuid4().hex[:12],
        "name": name,
        "description": (description or "").strip(),
        "instruction": (instruction or "").strip(),
        "created_at": datetime.datetime.now().astimezone().isoformat(),
    }
    skills.append(skill)
    _write(skills)
    return skill


def delete_skill(skill_id: str) -> bool:
    skills = load_skills()
    out = [s for s in skills if s.get("id") != skill_id]
    if len(out) == len(skills):
        return False
    _write(out)
    return True


def skills_context() -> str:
    """Markdown snippet describing available custom skills (for agent context)."""
    skills = load_skills()
    if not skills:
        return ""
    lines = ["Custom skills registered by the researcher:", ""]
    for s in skills:
        lines.append(f"- **{s.get('name')}** — {s.get('description', '')}")
        if s.get("instruction"):
            lines.append(f"  Instruction: {s['instruction']}")
    lines.append("")
    lines.append("Follow these instructions when the user's request matches a skill.")
    return "\n".join(lines)
