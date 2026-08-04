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

Then give up to 3 concrete, actionable next-step suggestions to improve the experiment:
better hyperparameters, more data, a different method, a follow-up comparison, etc.
Suggestions must be specific to what was actually run.

Reply with JSON only, an object:
{"findings": [{"severity": "critical"|"warning"|"info", "message": "short description"}],
 "suggestions": [{"title": "short action title, e.g. 'Try eps=1.0'",
                  "action": "one-sentence description of the change to make",
                  "prompt": "A complete user prompt that, sent to the assistant, would
                             perform this suggested change and rerun. Start with a verb,
                             reference the experiment, and state the exact new
                             config/metrics, e.g. 'Start variant run 'eps=1.0' with config
                             {...}, rerun the experiment, and report the new metrics.'"}]}
If everything checks out, findings is an empty array. If you cannot suggest anything
useful, suggestions is an empty array.
"""

FINDINGS_RE = re.compile(r"\{.*\}", re.DOTALL)


class Reviewer:
    def __init__(self, llm: LLMClient, store: ProjectStore, window: int = 8):
        self.llm = llm
        self.store = store
        self.window = window

    async def review(self) -> dict:
        """Return {"findings": [...], "suggestions": [...]} for the last turn."""
        msgs = self.store.list_messages(limit=self.window)
        if not msgs:
            return {"findings": [], "suggestions": []}
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
            return {"findings": [], "suggestions": []}
        text = resp.get("content", "")
        return _parse_review(text)


def _parse_review(text: str) -> dict:
    # Try a JSON object first, then fall back to a bare array of findings.
    for pattern in (r"\{.*\}", r"\[.*\]"):
        m = re.search(pattern, text, re.DOTALL)
        if not m:
            continue
        try:
            parsed = json.loads(m.group(0))
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, list):
            parsed = {"findings": parsed, "suggestions": []}
        if not isinstance(parsed, dict):
            continue
        findings = []
        for f in (parsed.get("findings") or [])[:12]:
            if isinstance(f, dict):
                findings.append({
                    "severity": f.get("severity", "info"),
                    "message": str(f.get("message", "")),
                })
        suggestions = []
        for s in (parsed.get("suggestions") or [])[:3]:
            sug = _normalize_suggestion(s)
            if sug is not None:
                suggestions.append(sug)
        return {"findings": findings, "suggestions": suggestions}
    return {"findings": [], "suggestions": []}


def _normalize_suggestion(raw) -> dict | None:
    """Coerce a reviewer suggestion into a structured {title, action, prompt} form.

    Accepts dict suggestions (title/action/prompt) as well as legacy plain strings.
    """
    if isinstance(raw, dict):
        title = str(raw.get("title") or "").strip()
        action = str(raw.get("action") or "").strip()
        prompt = str(raw.get("prompt") or "").strip()
        if not title and action:
            title = action[:80]
        if not action and prompt:
            action = prompt[:200]
        if not prompt and action:
            prompt = action
        if title or prompt:
            return {"title": title or prompt[:80], "action": action,
                    "prompt": prompt or action}
        return None
    if isinstance(raw, str) and raw.strip():
        text = raw.strip()
        return {"title": text[:80], "action": text, "prompt": text}
    return None
