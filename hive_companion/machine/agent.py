from __future__ import annotations

import json
import re
import time
from pathlib import Path

from hive.config import AppConfig, load_config
from hive.llm import ChatMessage, chat, get_provider
from hive.machine.tools import DISPATCH, dispatch_tool, TOOL_SCHEMAS, WORKSPACE
from hive.machine import MACHINE_DIR, HISTORY_DB
import sqlite3

SYSTEM = """You are Hive-Machine — a local Perplexity Computer. You run on Ollama / LM Studio only.
You have a sandboxed computer at ~/.hive/machine/workspace (cwd). Tools available:

- list_files(path=".", recursive=false)
- read_file(path, max_bytes=50000)
- write_file(path, content)
- delete_path(path)
- run_bash(cmd, timeout=30)      # bash in workspace
- run_python(code, timeout=30)   # python3 in workspace
- web_fetch(url)
- web_search(query, top_k=5)

Respond with EITHER a tool call OR a final answer. Tool call format:
```json
{"tool": "run_bash", "args": {"cmd": "ls -la"}}
```
Only one tool per turn. After tool result, you will be called again. When done, answer in markdown with citations/paths.
Be concise, local-only, no cloud. Prefer file+code evidence over hallucination.
"""

HISTORY_SYSTEM = "You are Hive-Machine history summarizer."

def _init_db():
    MACHINE_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(HISTORY_DB)
    con.execute("CREATE TABLE IF NOT EXISTS runs (id INTEGER PRIMARY KEY, task TEXT, created REAL, steps INTEGER, final TEXT)")
    con.commit()
    con.close()

def save_run(task: str, steps: int, final: str):
    _init_db()
    con = sqlite3.connect(HISTORY_DB)
    con.execute("INSERT INTO runs(task, created, steps, final) VALUES (?,?,?,?)", (task, time.time(), steps, final[:20000]))
    con.commit()
    con.close()

_tool_re = re.compile(r"```json\s*(\{.*?\"tool\".*?\})\s*```", re.S)

def _extract_tool(text: str):
    m = _tool_re.search(text)
    if not m:
        # try raw json line
        m2 = re.search(r"\{\s*\"tool\"\s*:\s*\"(\w+)\"", text)
        if m2:
            # try parse whole text as json
            try:
                obj = json.loads(text.strip().split("```")[-1].strip() if "```" in text else text.strip())
                if "tool" in obj:
                    return obj
            except Exception:
                pass
        return None
    try:
        return json.loads(m.group(1))
    except Exception:
        return None

def run_task(task: str, cfg: AppConfig | None = None, max_steps: int = 12, verbose: bool = False) -> str:
    cfg = cfg or load_config()
    prov = get_provider(cfg)
    ok, msg = prov.health()
    if not ok:
        return f"[error] No local LLM reachable: {msg} (ollama {cfg.llm.ollama_url}, lmstudio {cfg.llm.lmstudio_url})"

    messages = [ChatMessage(role="system", content=SYSTEM), ChatMessage(role="user", content=task)]
    transcript = []
    for step in range(max_steps):
        resp = chat(cfg, messages)
        text = resp.content.strip()
        transcript.append(f"Assistant step {step+1}:\n{text}")
        if verbose:
            print(text)
        tool_call = _extract_tool(text)
        if not tool_call:
            # no tool → final answer
            save_run(task, step+1, text)
            return text
        tool = tool_call.get("tool")
        args = tool_call.get("args", {})
        if verbose:
            print(f"[tool {tool} {args}]")
        result = dispatch_tool(tool, **args)
        # truncate result for LLM
        result_clipped = result[:8000]
        messages.append(ChatMessage(role="assistant", content=text))
        messages.append(ChatMessage(role="user", content=f"[tool {tool} result]\n{result_clipped}\n\nContinue or answer."))
        transcript.append(f"Tool {tool} result:\n{result_clipped}")
        # if LLM seems stuck looping same tool, break after
        if step == max_steps - 1:
            # force final
            final_prompt = "You have reached max steps. Summarize what you did, what files you created/modified, and answer the task in markdown."
            messages.append(ChatMessage(role="user", content=final_prompt))
            final = chat(cfg, messages).content
            save_run(task, max_steps, final)
            return final
    return "\n".join(transcript)

def list_history(limit: int = 20):
    _init_db()
    con = sqlite3.connect(HISTORY_DB)
    cur = con.execute("SELECT id, task, created, steps FROM runs ORDER BY id DESC LIMIT ?", (limit,))
    rows = cur.fetchall()
    con.close()
    return rows
