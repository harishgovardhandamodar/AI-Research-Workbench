"""Background reviewer agent.

Re-reads the last N conversation messages, code outputs and artifacts to flag
problems: untraceable numbers, mismatched figures, unsupported claims. Findings
are returned as a JSON list and surfaced in the UI.
"""

from __future__ import annotations

import json
import re

from ..llm import LLMClient
from ..store import ProjectStore

REVIEWER_PROMPT = """\
You are a rigorous scientific reviewer embedded in a research workbench. You review
the recent assistant/user conversation and the code execution transcript for:

1. Untraceable numbers or claims: statements not backed by any code output or artifact.
2. Mismatched figures: figure descriptions that don't match what the code produced.
3. Unsupported/overconfident conclusions relative to the evidence.
4. Missing provenance: important results that were never saved as artifacts.
5. Code issues: bugs, silent failures, or non-reproducible snippets.

Reply with JSON only, an array of findings:
[{"severity": "critical"|"warning"|"info", "message": "short description"}]
If everything checks out, reply with an empty array.
"""

FINDINGS_RE = re.compile(r"\[.*\]", re.DOTALL)


class Reviewer:
    def __init__(self, llm: LLMClient, store: ProjectStore, window: int = 8):
        self.llm = llm
        self.store = store
        self.window = window

    async def review(self) -> list[dict]:
        msgs = self.store.list_messages(limit=self.window)
        if not msgs:
            return []
        transcript = []
        for m in msgs:
            role = m["role"]
            content = m["content"] or ""
            if role == "user":
                transcript.append(f"USER: {content}")
            elif role == "assistant":
                transcript.append(f"ASSISTANT: {content}")
            elif role == "tool":
                meta = m.get("meta", {})
                name = meta.get("name", "tool")
                transcript.append(f"TOOL({name}) OUTPUT:\n{content[:4000]}")
        prompt = REVIEWER_PROMPT + "\n\nTranscript:\n" + "\n".join(transcript)
        try:
            resp = await self.llm.complete(
                [{"role": "system", "content": prompt}],
                temperature=0.1, tools=None)
        except Exception:  # noqa: BLE001
            return []
        text = resp.get("content", "")
        m = FINDINGS_RE.search(text)
        if not m:
            return []
        try:
            findings = json.loads(m.group(0))
        except json.JSONDecodeError:
            return []
        if not isinstance(findings, list):
            return []
        clean = []
        for f in findings[:12]:
            if isinstance(f, dict):
                clean.append({
                    "severity": f.get("severity", "info"),
                    "message": str(f.get("message", "")),
                })
        return clean
