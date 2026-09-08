from __future__ import annotations

import subprocess
import shlex
import time
from pathlib import Path
from typing import List

import httpx
from bs4 import BeautifulSoup

from . import WORKSPACE

# ── sandbox helpers ────────────────────────────────────────────────

def _ensure_workspace() -> Path:
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    return WORKSPACE

def _jail(path: str | Path) -> Path:
    """Resolve path inside WORKSPACE — prevents escape via .. or absolute."""
    _ensure_workspace()
    p = (WORKSPACE / Path(path)).resolve()
    ws = WORKSPACE.resolve()
    if not str(p).startswith(str(ws)):
        raise ValueError(f"path escapes workspace: {path}")
    return p

# ── file ops ───────────────────────────────────────────────────────

def list_files(path: str = ".", recursive: bool = False) -> str:
    base = _jail(path)
    if not base.exists():
        return f"[error] not found: {path}"
    if base.is_file():
        return f"{path} (file, {base.stat().st_size} bytes)"
    pattern = "**/*" if recursive else "*"
    items: List[str] = []
    for child in sorted(base.glob(pattern)):
        rel = child.relative_to(WORKSPACE)
        typ = "dir" if child.is_dir() else f"file {child.stat().st_size}B"
        items.append(f"{rel}  [{typ}]")
        if len(items) > 200:
            items.append("... truncated 200")
            break
    return "\n".join(items) or "(empty)"

def read_file(path: str, max_bytes: int = 50000) -> str:
    p = _jail(path)
    if not p.exists():
        return f"[error] not found: {path}"
    if p.is_dir():
        return f"[error] is directory: {path}, use list_files"
    data = p.read_bytes()[:max_bytes]
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return f"[binary {len(data)} bytes, not utf-8] hex preview: {data[:200].hex()}"
    if len(p.read_bytes()) > max_bytes:
        text += f"\n... truncated {len(p.read_bytes())-max_bytes} bytes"
    return text

def write_file(path: str, content: str) -> str:
    p = _jail(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return f"wrote {len(content)} chars → {path} ({p.stat().st_size} bytes)"

def delete_path(path: str) -> str:
    p = _jail(path)
    if not p.exists():
        return f"[error] not found: {path}"
    if p.is_dir():
        import shutil
        shutil.rmtree(p)
        return f"deleted dir {path}"
    p.unlink()
    return f"deleted {path}"

# ── code exec ──────────────────────────────────────────────────────

def run_bash(cmd: str, timeout: int = 30) -> str:
    """Run bash in WORKSPACE, timeout 30s, capture stdout+stderr."""
    _ensure_workspace()
    try:
        proc = subprocess.run(
            cmd, shell=True, cwd=WORKSPACE, capture_output=True, text=True, timeout=timeout
        )
        out = proc.stdout or ""
        err = proc.stderr or ""
        code = proc.returncode
        body = out + ("\n[stderr]\n"+err if err else "")
        return f"exit {code}\n{body[:20000]}" if body else f"exit {code} (no output)"
    except subprocess.TimeoutExpired:
        return f"[timeout {timeout}s] {cmd}"
    except Exception as e:
        return f"[error] {e}"

def run_python(code: str, timeout: int = 30) -> str:
    """Run python snippet in workspace (writes to temp file)."""
    _ensure_workspace()
    tmp = WORKSPACE / "_machine_exec.py"
    tmp.write_text(code, encoding="utf-8")
    return run_bash(f"python3 {shlex.quote(str(tmp.name))}", timeout=timeout)

# ── web ────────────────────────────────────────────────────────────

def web_fetch(url: str, max_chars: int = 15000) -> str:
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    try:
        with httpx.Client(timeout=15, follow_redirects=True, headers={"User-Agent": "Hive-Machine/0.1"}) as c:
            r = c.get(url)
            r.raise_for_status()
            ctype = r.headers.get("content-type", "")
            if "html" in ctype:
                soup = BeautifulSoup(r.text, "html.parser")
                # strip scripts
                for tag in soup(["script", "style", "nav", "footer"]):
                    tag.decompose()
                text = soup.get_text(separator="\n", strip=True)
                return text[:max_chars] + (f"\n... truncated" if len(text) > max_chars else "")
            return r.text[:max_chars]
    except Exception as e:
        return f"[fetch error {url}: {e}]"

def web_search(query: str, top_k: int = 5) -> str:
    """Lightweight search via arXiv + OpenAlex + web_fetch fallback."""
    # Use OpenAlex as search backend (no key, local LLM will synthesize)
    from hive.papers.openalex import search as oa_search
    try:
        papers = oa_search(query, top_k=top_k)
        lines = []
        for p in papers:
            lines.append(f"- {p.title} ({p.year or 'n.d.'}) {p.doi or p.id} [{p.cited_by_count} cites] — {(p.abstract or '')[:200]}")
        return "\n".join(lines) or "(no results)"
    except Exception as e:
        return f"[search error: {e}]"

# ── tool registry for agent / docs ─────────────────────────────────

TOOL_SCHEMAS = [
    {"name": "list_files", "args": {"path": "str, relative to workspace (default '.')", "recursive": "bool"}, "desc": "List files in workspace"},
    {"name": "read_file", "args": {"path": "str", "max_bytes": "int 50k"}, "desc": "Read file text"},
    {"name": "write_file", "args": {"path": "str", "content": "str"}, "desc": "Write/create file"},
    {"name": "delete_path", "args": {"path": "str"}, "desc": "Delete file/dir"},
    {"name": "run_bash", "args": {"cmd": "str", "timeout": "int 30"}, "desc": "Run bash in workspace"},
    {"name": "run_python", "args": {"code": "str", "timeout": "int 30"}, "desc": "Run python snippet"},
    {"name": "web_fetch", "args": {"url": "str"}, "desc": "Fetch URL text"},
    {"name": "web_search", "args": {"query": "str", "top_k": "int 5"}, "desc": "Search papers/web (OpenAlex)"},
]

DISPATCH = {
    "list_files": lambda **kw: list_files(kw.get("path", "."), kw.get("recursive", False)),
    "read_file": lambda **kw: read_file(kw["path"], kw.get("max_bytes", 50000)),
    "write_file": lambda **kw: write_file(kw["path"], kw["content"]),
    "delete_path": lambda **kw: delete_path(kw["path"]),
    "run_bash": lambda **kw: run_bash(kw["cmd"], kw.get("timeout", 30)),
    "run_python": lambda **kw: run_python(kw["code"], kw.get("timeout", 30)),
    "web_fetch": lambda **kw: web_fetch(kw["url"]),
    "web_search": lambda **kw: web_search(kw["query"], kw.get("top_k", 5)),
}

def dispatch_tool(name: str, **kwargs) -> str:
    # audited dispatch — logs every tool with severity/impact/proofs
    from hive.machine.audit import log_event
    fn = DISPATCH.get(name)
    if not fn:
        res = f"[error] unknown tool {name}"
        try:
            log_event(tool=name, args=kwargs, result=res, bytes_out=len(str(kwargs)), bytes_in=len(res))
        except Exception:
            pass
        return res
    try:
        res = fn(**kwargs)
        # network / file / command bytes
        bout = len(str(kwargs).encode())
        binb = len(res.encode()) if isinstance(res, str) else 0
        try:
            log_event(tool=name, args=kwargs, result=res, bytes_out=bout, bytes_in=binb)
        except Exception:
            pass
        return res
    except Exception as e:
        res = f"[tool error {name}: {e}]"
        try:
            log_event(tool=name, args=kwargs, result=res, bytes_out=len(str(kwargs)), bytes_in=len(res))
        except Exception:
            pass
        return res
