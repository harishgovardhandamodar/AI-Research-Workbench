"""Web backend — serves TSX dashboard + audited API.

Local-first: no egress, all data from ~/.hive/* DBs and local LLM health.
"""

from __future__ import annotations

import json
import urllib.parse
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from typing import Optional

from hive.config import load_config
from hive.machine import MACHINE_DIR, WORKSPACE
from hive.machine.audit import query_audit, stats, verify_chain
from hive.research.session import list_sessions
from hive.machine.workflows import list_workflows

WEB_DIST = Path(__file__).parent.parent.parent / "web" / "dist"
WEB_SRC = Path(__file__).parent.parent.parent / "web" / "src"

class WebHandler(SimpleHTTPRequestHandler):
    def _set_headers(self, ctype="application/json", status=200):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_OPTIONS(self):
        self._set_headers("text/plain", 200)
        self.wfile.write(b"")

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length) if length else b""
        try:
            data = json.loads(body) if body else {}
        except Exception:
            data = {}
        # feedback
        if path == "/api/feedback":
            eid = data.get("execution_id") or self.headers.get("X-Execution-Id")
            reward = int(data.get("reward", 0))
            note = data.get("note", "")
            try:
                from hive.ledger import add_feedback
                fid = add_feedback(eid, reward, note)
                self._set_headers()
                self.wfile.write(json.dumps({"ok": True, "feedback_id": fid}).encode())
            except Exception as e:
                self._set_headers("application/json", 400)
                self.wfile.write(json.dumps({"ok": False, "error": str(e)}).encode())
            return
        if path == "/api/learn/run":
            wb = data.get("workbench")
            it = int(data.get("iterations", 10))
            rt = float(data.get("reward_threshold", 4.0))
            lr = float(data.get("learning_rate", 0.3))
            dry = bool(data.get("dry", False))
            try:
                from hive.learn import run_loop
                res = run_loop(workbench=wb or "default", iterations=it, reward_threshold=rt, learning_rate=lr, dry=dry)
                self._set_headers()
                self.wfile.write(json.dumps(res).encode())
            except Exception as e:
                self._set_headers("application/json", 500)
                self.wfile.write(json.dumps({"error": str(e)}).encode())
            return
        self._set_headers("application/json", 404)
        self.wfile.write(json.dumps({"error": f"unknown POST {path}"}).encode())

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        # ── API ───────────────────────────────────────────────────
        if path == "/api/health":
            self._api_health()
            return
        if path == "/api/models":
            self._api_models()
            return
        if path == "/api/audit":
            limit = int(qs.get("limit", ["50"])[0])
            min_sev = int(qs.get("min_severity", ["0"])[0])
            self._api_audit(limit, min_sev)
            return
        if path == "/api/stats":
            self._api_stats()
            return
        if path == "/api/verify":
            self._api_verify()
            return
        if path == "/api/sessions":
            self._api_sessions()
            return
        if path == "/api/workflows":
            self._api_workflows()
            return
        if path == "/api/files":
            self._api_files()
            return
        if path == "/api/nvidia":
            self._api_nvidia()
            return
        if path == "/api/export":
            self._api_export()
            return
        if path == "/api/workflow_detail":
            name = qs.get("name", [""])[0]
            wtype = qs.get("type", ["machine"])[0]
            self._api_workflow_detail(name, wtype)
            return
        if path == "/api/reports":
            self._api_reports()
            return
        if path == "/api/report":
            rpath = qs.get("path", [""])[0]
            self._api_report(rpath)
            return
        if path == "/api/ledger":
            limit = int(qs.get("limit", ["50"])[0])
            wb = qs.get("workbench", [None])[0]
            self._api_ledger(limit, wb)
            return
        if path == "/api/workbenches":
            self._api_workbenches()
            return
        if path == "/api/learn_status":
            self._api_learn_status()
            return
        if path == "/api/memory":
            wb = qs.get("workbench", [None])[0]
            self._api_memory(wb)
            return
        if path == "/api/derived":
            wb = qs.get("workbench", [None])[0]
            self._api_derived(wb)
            return

        # ── static — serve web/dist if built, else fallback ─────
        if WEB_DIST.exists() and (WEB_DIST / "index.html").exists():
            # serve dist
            rel = path.lstrip("/") or "index.html"
            # sanitize
            target = (WEB_DIST / rel).resolve()
            if not str(target).startswith(str(WEB_DIST.resolve())):
                self._set_headers("text/plain", 403)
                self.wfile.write(b"forbidden")
                return
            if target.is_dir():
                target = target / "index.html"
            if target.exists():
                # guess ctype
                ctype = "text/html"
                if target.suffix == ".js":
                    ctype = "application/javascript"
                elif target.suffix == ".css":
                    ctype = "text/css"
                elif target.suffix == ".json":
                    ctype = "application/json"
                self._set_headers(ctype, 200)
                self.wfile.write(target.read_bytes())
                return
            # fallback to index.html for SPA
            self._set_headers("text/html", 200)
            self.wfile.write((WEB_DIST / "index.html").read_bytes())
            return

        # dev fallback: serve simple placeholder with instructions
        if path == "/" or path == "/index.html":
            self._set_headers("text/html", 200)
            html = """<!doctype html><html><head><meta charset="utf-8"><title>Hive Web</title>
            <style>body{font-family:system-ui;background:#0b0e14;color:#e6e8ec;padding:40px} a{color:#4f8cff} code{background:#11151d;border:1px solid #1f2533;padding:2px 6px;border-radius:4px}</style></head><body>
            <h1>Hive Research - A Local research companion — Web Dashboard</h1>
            <p>TSX build not found. Build it:</p>
            <pre><code>cd web && npm install && npm run build</code></pre>
            <p>Then <code>hive serve --web</code> or <code>hive web</code> will serve <code>web/dist</code> on <code>http://localhost:8000</code>.</p>
            <p>API is live:</p>
            <ul><li><a href="/api/health">/api/health</a></li><li><a href="/api/audit?limit=5">/api/audit</a></li><li><a href="/api/stats">/api/stats</a></li><li><a href="/api/verify">/api/verify</a></li></ul>
            <p>For dev (hot reload): <code>cd web && npm run dev</code> (proxies /api to :8000)</p>
            </body></html>"""
            self.wfile.write(html.encode())
            return

        # fallback 404
        self._set_headers("text/plain", 404)
        self.wfile.write(b"not found")

    # ── API implementations ──────────────────────────────────────

    def _api_health(self):
        from hive.llm.ollama import OllamaProvider
        from hive.llm.lmstudio import LMStudioProvider
        from hive.llm.nvidia import NvidiaProvider
        import httpx
        cfg = load_config()
        res = {}
        for label, prov in [
            ("ollama", OllamaProvider(cfg.llm.ollama_url, cfg.llm.ollama_model)),
            ("lmstudio", LMStudioProvider(cfg.llm.lmstudio_url, cfg.llm.lmstudio_model)),
            ("nvidia", NvidiaProvider(cfg.llm.nvidia_url, cfg.llm.nvidia_model)),
        ]:
            ok, msg = prov.health()
            res[label] = f"ok ({msg})" if ok else msg
        # paper APIs
        for name, url in [("openalex", "https://api.openalex.org/works?search=test&per-page=1"), ("arxiv", "https://export.arxiv.org/api/query?search_query=all:test&max_results=1")]:
            try:
                with httpx.Client(timeout=5) as c:
                    r = c.get(url)
                    res[name] = str(r.status_code)
            except Exception as e:
                res[name] = str(e)
        self._set_headers()
        self.wfile.write(json.dumps(res).encode())

    def _api_models(self):
        from hive.machine.nvidia_pair import discover_all
        cfg = load_config()
        models = discover_all(cfg)
        self._set_headers()
        self.wfile.write(json.dumps([m.__dict__ for m in models]).encode())

    def _api_audit(self, limit: int, min_sev: int):
        rows = query_audit(limit=limit, min_severity=min_sev)
        self._set_headers()
        self.wfile.write(json.dumps(rows).encode())

    def _api_stats(self):
        s = stats()
        self._set_headers()
        self.wfile.write(json.dumps({"total": s["total"], "by_tool": s["by_tool"]}).encode())

    def _api_verify(self):
        ok, msg = verify_chain()
        self._set_headers()
        self.wfile.write(json.dumps({"ok": ok, "msg": msg}).encode())

    def _api_sessions(self):
        # merge DB + legacy personal-experiments so dashboard never empty
        try:
            from hive.research.session import list_all_sessions
            rows = list_all_sessions(limit=30)
        except Exception:
            from hive.research.session import list_sessions
            rows = list_sessions(limit=30)
        self._set_headers()
        self.wfile.write(json.dumps(rows).encode())

    def _api_workflows(self):
        wfs = list_workflows()
        self._set_headers()
        self.wfile.write(json.dumps([{"name": p.stem, "path": str(p)} for p in wfs]).encode())

    def _api_files(self):
        WORKSPACE.mkdir(parents=True, exist_ok=True)
        items = []
        for p in sorted(WORKSPACE.iterdir()):
            try:
                items.append({"path": p.name, "type": "dir" if p.is_dir() else "file", "size": p.stat().st_size})
            except Exception:
                continue
        self._set_headers()
        self.wfile.write(json.dumps(items).encode())

    def _api_nvidia(self):
        from hive.machine.nvidia_pair import discover_all, nvidia_smi_info
        cfg = load_config()
        gpu = nvidia_smi_info()
        models = discover_all(cfg)
        self._set_headers()
        self.wfile.write(json.dumps({"gpu": gpu, "models": [m.__dict__ for m in models]}).encode())

    def _api_export(self):
        from hive.machine.audit import export_proofs
        out = export_proofs()
        self._set_headers()
        self.wfile.write(json.dumps(out).encode())

    def _api_workflow_detail(self, name: str, wtype: str = "machine"):
        """Return workflow steps/detail for research-companion or Hive-Machine."""
        if wtype == "research":
            # research-companion workflows are code-defined (hive/research/workflows.py)
            try:
                from hive.research import prompts as _prompts
                mapping = {
                    "deepresearch": getattr(_prompts, "DEEP_RESEARCH", ""),
                    "lit": getattr(_prompts, "LIT_REVIEW", ""),
                    "review": getattr(_prompts, "REVIEW", ""),
                    "audit": getattr(_prompts, "AUDIT", ""),
                    "replicate": getattr(_prompts, "REPLICATE", ""),
                    "recipe": getattr(_prompts, "RECIPE", ""),
                    "compare": getattr(_prompts, "COMPARE", ""),
                    "draft": getattr(_prompts, "DRAFT", ""),
                    "autoresearch": getattr(_prompts, "AUTORESEARCH", ""),
                    "watch": getattr(_prompts, "WATCH", ""),
                }
                key = name.lower()
                if key not in mapping:
                    self._set_headers("application/json", 404)
                    self.wfile.write(json.dumps({"error": f"research workflow not found: {name}", "available": list(mapping.keys())}).encode())
                    return
                # synthesize steps as used in hive/research/workflows.py run_workflow
                steps = [
                    {"id": "search_and_rank", "tool": "openalex+arxiv search", "desc": f"Search and rank papers for topic (top_k, PaperRank)", "prompt": ""},
                    {"id": "enrich_full_text", "tool": "europe_pmc fetch", "desc": "Enrich top papers with full text (Europe PMC / arXiv)", "prompt": ""},
                    {"id": "llm_synthesis", "tool": "llm_chat (local Ollama/LMStudio)", "desc": f"Kind: {key} — LLM synthesis via prompts.{key.upper()}", "prompt": mapping[key][:2000]},
                ]
                # include raw prompt preview trimmed
                self._set_headers()
                self.wfile.write(json.dumps({"name": key, "type": "research", "description": f"Research-companion /{key}", "access": "core-workflow", "steps": steps, "prompt_preview": mapping[key][:4000]}).encode())
                return
            except Exception as e:
                self._set_headers("application/json", 500)
                self.wfile.write(json.dumps({"error": str(e)}).encode())
                return
        # machine workflows: load from WORKFLOWS_DIR
        from hive.machine import WORKFLOWS_DIR
        try:
            import yaml
        except ImportError:
            yaml = None
        cand = None
        for ext in (".yaml", ".yml", ".json"):
            pth = WORKFLOWS_DIR / f"{name}{ext}"
            if pth.exists():
                cand = pth
                break
        if cand is None:
            # try exact path
            p2 = WORKFLOWS_DIR / name
            if p2.exists() and p2.is_file():
                cand = p2
        if cand is None or not cand.exists():
            self._set_headers("application/json", 404)
            self.wfile.write(json.dumps({"error": f"workflow not found: {name}", "looked_in": str(WORKFLOWS_DIR)}).encode())
            return
        try:
            text = cand.read_text(encoding="utf-8")
            if cand.suffix in (".yaml", ".yml"):
                data = yaml.safe_load(text) if yaml else {"raw": text}
            else:
                data = json.loads(text)
            # normalize steps for frontend
            steps = data.get("steps", [])
            self._set_headers()
            self.wfile.write(json.dumps({"name": data.get("name", cand.stem), "type": "machine", "description": data.get("description", ""), "access": data.get("access", "core-workflow"), "path": str(cand), "steps": steps}).encode())
        except Exception as e:
            self._set_headers("application/json", 500)
            self.wfile.write(json.dumps({"error": str(e)}).encode())

    def _api_reports(self):
        """List all generated reports across research + Hive-Machine (local-only safe)."""
        items = []
        # Machine workspace: WORKSPACE/**/reports/*.md etc
        try:
            if WORKSPACE.exists():
                for pat in ["**/reports/*.md", "**/reports/*.json", "**/reports/*.csv", "**/reports/*.png", "**/reports/*.html"]:
                    for f in WORKSPACE.glob(pat):
                        try:
                            rel = f.relative_to(WORKSPACE)
                            # only include files under reports (already filtered)
                            items.append({"path": str(rel), "full_path": str(f), "type": "machine", "tier": rel.parts[1] if len(rel.parts) > 1 else "", "workflow": rel.parts[0] if len(rel.parts) > 0 else "", "name": f.name, "size": f.stat().st_size, "mtime": f.stat().st_mtime})
                        except Exception:
                            continue
        except Exception:
            pass
        # Research outputs: ./output/*.md and ~/.hive/sessions
        try:
            out_dir = Path.cwd() / "output"
            if out_dir.exists():
                for f in out_dir.glob("*.md"):
                    try:
                        items.append({"path": f"output/{f.name}", "full_path": str(f), "type": "research", "workflow": "research", "name": f.name, "size": f.stat().st_size, "mtime": f.stat().st_mtime})
                    except Exception:
                        continue
        except Exception:
            pass
        try:
            from hive.config import CONFIG_DIR
            sess_dir = CONFIG_DIR / "sessions"
            if sess_dir.exists():
                for f in sess_dir.rglob("*.md"):
                    try:
                        rel = f.relative_to(CONFIG_DIR)
                        items.append({"path": str(rel), "full_path": str(f), "type": "research", "workflow": f.parent.name, "name": f.name, "size": f.stat().st_size, "mtime": f.stat().st_mtime})
                    except Exception:
                        continue
        except Exception:
            pass
        # personal-experiments (legacy) — so reports dashboard never empty
        try:
            from hive.config import EXPERIMENTS_DIR
            if EXPERIMENTS_DIR and EXPERIMENTS_DIR.exists():
                for pat in ["**/*.json", "**/*.md", "**/*.png"]:
                    for f in EXPERIMENTS_DIR.glob(pat):
                        if f.is_file() and f.stat().st_size < 5_000_000:
                            try:
                                rel = f.relative_to(EXPERIMENTS_DIR)
                                items.append({"path": f"personal-experiments/{rel}", "full_path": str(f), "type": "legacy", "workflow": str(rel.parent), "name": f.name, "size": f.stat().st_size, "mtime": f.stat().st_mtime})
                                if len(items) > 400:
                                    break
                            except Exception:
                                continue
                        if len(items) > 400:
                            break
                # throttle legacy items after merge sorting will top 200
        except Exception:
            pass
        # sort by mtime desc
        items.sort(key=lambda x: x.get("mtime", 0), reverse=True)
        self._set_headers()
        self.wfile.write(json.dumps(items[:200]).encode())

    def _api_report(self, rpath: str):
        """Serve single report file (jailed). Allows WORKSPACE reports and output/research reports."""
        if not rpath:
            self._set_headers("application/json", 400)
            self.wfile.write(json.dumps({"error": "missing ?path="}).encode())
            return
        # guard traversal
        if ".." in rpath or rpath.startswith("/"):
            # allow absolute only if inside allowed roots
            pass
        allowed_roots = []
        try:
            allowed_roots.append(WORKSPACE.resolve())
        except Exception:
            pass
        try:
            allowed_roots.append((Path.cwd() / "output").resolve())
        except Exception:
            pass
        try:
            from hive.config import CONFIG_DIR
            allowed_roots.append((CONFIG_DIR / "sessions").resolve())
            allowed_roots.append(CONFIG_DIR.resolve())
        except Exception:
            pass
        # resolve candidate
        # try WORKSPACE relative first
        candidates = []
        if not Path(rpath).is_absolute():
            candidates.append((WORKSPACE / rpath).resolve())
            candidates.append((Path.cwd() / rpath).resolve())
            try:
                from hive.config import CONFIG_DIR
                candidates.append((CONFIG_DIR / rpath).resolve())
            except Exception:
                pass
        else:
            candidates.append(Path(rpath).resolve())
        target = None
        for c in candidates:
            if c.exists() and c.is_file():
                # check jailing: must be inside one of allowed roots
                for root in allowed_roots:
                    try:
                        if str(c).startswith(str(root)):
                            target = c
                            break
                    except Exception:
                        continue
            if target:
                break
        if target is None:
            # fallback: try direct WORKSPACE jailed resolution for any relative
            try:
                t2 = (WORKSPACE / Path(rpath)).resolve()
                if t2.exists() and str(t2).startswith(str(WORKSPACE.resolve())):
                    target = t2
            except Exception:
                pass
        if target is None or not target.exists():
            self._set_headers("application/json", 404)
            self.wfile.write(json.dumps({"error": f"report not found: {rpath}", "hint": "use /api/reports to list"}).encode())
            return
        # serve with ctype guess
        ctype = "text/plain"
        if target.suffix == ".md":
            ctype = "text/markdown; charset=utf-8"
        elif target.suffix == ".json":
            ctype = "application/json; charset=utf-8"
        elif target.suffix == ".csv":
            ctype = "text/csv; charset=utf-8"
        elif target.suffix == ".html":
            ctype = "text/html; charset=utf-8"
        elif target.suffix == ".png":
            ctype = "image/png"
        try:
            data = target.read_bytes()
            # cap 2MB
            if len(data) > 2_000_000:
                data = data[:2_000_000] + b"\n... truncated 2MB"
            self._set_headers(ctype, 200)
            self.wfile.write(data)
        except Exception as e:
            self._set_headers("application/json", 500)
            self.wfile.write(json.dumps({"error": str(e)}).encode())

    def _api_ledger(self, limit: int = 20, workbench: str | None = None):
        from hive.ledger import query_ledger
        rows = query_ledger(limit=limit, workbench=workbench)
        self._set_headers()
        self.wfile.write(json.dumps(rows).encode())

    def _api_workbenches(self):
        from hive.workbench import list_workbenches
        wbs = list_workbenches()
        self._set_headers()
        self.wfile.write(json.dumps(wbs).encode())

    def _api_learn_status(self):
        from hive.learn import learn_status
        from hive.ledger import ledger_stats
        st = learn_status()
        st["ledger"] = ledger_stats()
        self._set_headers()
        self.wfile.write(json.dumps(st).encode())

    def _api_memory(self, workbench: str | None = None):
        from hive.learn import query_memory
        rows = query_memory(workbench=workbench, limit=20)
        self._set_headers()
        self.wfile.write(json.dumps(rows).encode())

    def _api_derived(self, workbench: str | None = None):
        from hive.derived import list_derived
        rows = list_derived(workbench=workbench)
        self._set_headers()
        self.wfile.write(json.dumps(rows).encode())


def run_web(host: str = "127.0.0.1", port: int = 8000, open_browser: bool = False):
    # ensure machine dirs
    MACHINE_DIR.mkdir(parents=True, exist_ok=True)
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    addr = (host, port)
    # if web/dist exists, serve from there; else use handler fallback
    # we set directory to WEB_DIST if exists, else MACHINE_DIR
    handler = WebHandler
    # override directory for SimpleHTTPRequestHandler
    import os
    os.chdir(WEB_DIST if WEB_DIST.exists() else MACHINE_DIR)
    httpd = HTTPServer(addr, handler)
    print(f"Hive Web — http://{host}:{port}  (api: /api/health, /api/audit, /api/stats, etc.)")
    if WEB_DIST.exists():
        print(f"Serving TSX build: {WEB_DIST}")
    else:
        print(f"TSX not built — run: cd web && npm install && npm run build  — serving fallback + API")
    if open_browser:
        import webbrowser
        webbrowser.open(f"http://{host}:{port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped")
