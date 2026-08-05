"""HTTP client for the Fox workbench REST API.

Zero-dependency (``urllib``). Connects to a running workbench server
(``FOX_URL`` env, default ``http://127.0.0.1:8765``) and speaks to the same
endpoints the web UI uses, so the CLI is a thin second frontend to the
same backend — including the Research Knowledge Graphs / scenario API.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any


class FoxClientError(Exception):
    """A structured error raised when the server refuses a request."""

    def __init__(self, status: int, detail: Any, path: str) -> None:
        super().__init__(f"HTTP {status} from {path}: {detail}")
        self.status = status
        self.detail = detail
        self.path = path


def base_url() -> str:
    return os.environ.get("FOX_URL", "http://127.0.0.1:8765").rstrip("/")


class FoxClient:
    def __init__(self, url: str | None = None, timeout: float = 30.0) -> None:
        self.url = (url or base_url()).rstrip("/")
        self.timeout = timeout

    # ------------------------------------------------------------ transport --
    def request(self, method: str, path: str, body: dict | None = None,
                timeout: float | None = None) -> Any:
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(
            self.url + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                raw = resp.read().decode()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            detail = None
            try:
                detail = json.loads(e.read().decode())
            except Exception:  # noqa: BLE001
                pass
            raise FoxClientError(e.code, detail, path) from e
        except urllib.error.URLError as e:
            raise FoxClientError(0, f"cannot reach {self.url} — is the server "
                                    f"running? ({e.reason})", path) from e

    def get(self, path: str, **kw: Any) -> Any:
        return self.request("GET", path, **kw)

    def post(self, path: str, body: dict | None = None, **kw: Any) -> Any:
        return self.request("POST", path, body, **kw)

    def delete(self, path: str, **kw: Any) -> Any:
        return self.request("DELETE", path, **kw)

    def health(self) -> dict:
        return self.get("/api/health")

    # ------------------------------------------------------------------ misc --
    def config(self) -> dict:
        return self.get("/api/config").get("config", {})

    def models(self) -> list:
        return self.get("/api/models").get("models", [])

    # -------------------------------------------------------------- projects --
    def projects(self) -> list:
        return self.get("/api/projects").get("projects", [])

    def project(self, name: str) -> dict:
        return self.get(f"/api/projects/{name}")

    def create_project(self, name: str, description: str = "") -> dict:
        return self.post("/api/projects", {"name": name, "description": description})

    def delete_project(self, name: str) -> dict:
        return self.delete(f"/api/projects/{name}")

    def fork_project(self, name: str, target: str) -> dict:
        return self.post(f"/api/projects/{name}/fork", {"name": target})

    def runs(self, name: str) -> list:
        return self.get(f"/api/projects/{name}/runs").get("runs", [])

    def experiments(self, name: str) -> list:
        return self.get(f"/api/projects/{name}/experiments").get("experiments", [])

    def start_experiment(self, name: str) -> dict:
        return self.post(f"/api/projects/{name}/experiments", {})

    def run_obfuscation(self, name: str, n_rows: int = 2000, seed: int = 42,
                        timeout: float | None = 120.0) -> dict:
        """Run the bank-transaction obfuscation scenario suite on a project.

        Records each scenario as a run (metrics + figure + masked-vs-raw
        transactions table) under an "obfuscation (bank)" experiment so the
        app's Experiments panel can display results and transactions.
        """
        return self.post(f"/api/projects/{name}/experiments/run-obfuscation",
                         {"dataset": "bank", "n_rows": n_rows, "seed": seed},
                         timeout=timeout)

    def experiment(self, name: str, eid: str) -> dict:
        return self.get(f"/api/projects/{name}/experiments/{eid}")

    # ------------------------------------------------- research (RKG) --------
    def scenarios(self) -> list:
        return self.get("/api/rkg/scenarios").get("scenarios", [])

    def scenario(self, sid: str) -> dict:
        return self.get(f"/api/rkg/scenarios/{sid}")

    def scenario_status(self, sid: str) -> dict:
        return self.get(f"/api/rkg/scenarios/{sid}/status")

    def scenario_report(self, sid: str) -> str:
        return self.get(f"/api/rkg/scenarios/{sid}/report").get("report", "")

    def scenario_action(self, sid: str, action: str) -> dict:
        return self.post(f"/api/rkg/scenarios/{sid}/{action}", {})

    def rkg_graph(self) -> dict:
        return self.get("/api/rkg/graph")

    def rkg_stats(self) -> dict:
        return self.get("/api/rkg/stats")

    def rkg_papers(self) -> list:
        papers = self.get("/api/rkg/papers")
        return papers if isinstance(papers, list) else papers.get("papers", [])

    # ---------------------------------------------------------------- jobs ----
    def job(self, job_id: str) -> dict:
        return self.get(f"/api/rkg/jobs/{job_id}")

    def wait_job(self, job_id: str, poll: float = 4.0, max_wait: float = 1800.0,
                 progress_cb=None) -> dict:
        """Poll a background job until it finishes; returns final job view."""
        import time

        waited = 0.0
        last = None
        while waited < max_wait:
            job = self.job(job_id)
            last = job
            if progress_cb:
                progress_cb(job)
            if job.get("status") in ("done", "error"):
                return job
            time.sleep(poll)
            waited += poll
        return last or {}
