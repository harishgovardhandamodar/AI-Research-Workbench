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
import urllib.parse
import urllib.request
from typing import Any

from . import log
from .log import log as _log


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
        _log.debug("{} {}", method, path)
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                raw = resp.read().decode()
                _log.debug("{} {} -> {} ({} bytes)", method, path,
                           resp.status, len(raw))
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            detail = None
            try:
                detail = json.loads(e.read().decode())
            except Exception:  # noqa: BLE001
                pass
            _log.debug("{} {} -> HTTP {} {}", method, path, e.code, detail)
            raise FoxClientError(e.code, detail, path) from e
        except urllib.error.URLError as e:
            _log.debug("{} {} -> unreachable ({})", method, path, e.reason)
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

    def start_experiment(self, name: str, exp_name: str | None = None,
                         hypothesis: str = "", goal_metric: str = "",
                         goal_target: float | None = None,
                         plan: str = "") -> dict:
        body = {"name": exp_name or f"{name} experiment",
                "hypothesis": hypothesis,
                "goal_metric": goal_metric,
                "plan": plan}
        if goal_target is not None:
            body["goal_target"] = goal_target
        return self.post(f"/api/projects/{name}/experiments", body)

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

    def run(self, name: str, rid: str) -> dict:
        return self.get(f"/api/projects/{name}/runs/{rid}").get("run", {})

    def run_report(self, name: str, rid: str) -> dict:
        return self.post(f"/api/projects/{name}/runs/{rid}/report")

    def experiment_ranking(self, name: str, eid: str,
                           metric: str = "", limit: int = 50) -> dict:
        q = f"?metric={urllib.parse.quote(metric)}&limit={limit}" if metric \
            else f"?limit={limit}"
        return self.get(f"/api/projects/{name}/experiments/{eid}/ranking{q}")

    def compare(self, name: str, run_a: str, run_b: str) -> dict:
        q = f"?run_a={urllib.parse.quote(run_a)}&run_b={urllib.parse.quote(run_b)}"
        return self.get(f"/api/projects/{name}/compare{q}").get("comparison", {})

    # ------------------------------------------------------------ management --
    def mgmt_repos(self) -> list:
        return self.get("/api/management/repos").get("repos", [])

    def mgmt_status(self) -> dict:
        return self.get("/api/management/status")

    def mgmt_link(self, github_repo: str) -> dict:
        return self.post("/api/management/link", {"github_repo": github_repo})

    def mgmt_commit(self, name: str, message: str = "") -> dict:
        return self.post(f"/api/projects/{name}/management/commit",
                         {"message": message})

    def mgmt_push(self, name: str) -> dict:
        return self.post(f"/api/projects/{name}/management/push", {})

    def mgmt_commit_and_push(self, name: str, message: str = "") -> dict:
        return self.post(f"/api/projects/{name}/management/commit-and-push",
                         {"message": message})

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

    def rkg_papers_search(self, q: str) -> list:
        papers = self.get(f"/api/rkg/papers/search?q={urllib.parse.quote(q)}")
        return papers if isinstance(papers, list) else papers.get("papers", [])

    def rkg_import(self, query: str, model: str = "") -> dict:
        body = {"query": query}
        if model:
            body["model"] = model
        return self.post("/api/rkg/import", body)

    def rkg_web_add(self, url: str, model: str = "") -> dict:
        body = {"url": url}
        if model:
            body["model"] = model
        return self.post("/api/rkg/web/add", body)

    def rkg_pool(self) -> dict:
        return self.get("/api/rkg/pool")

    def rkg_pool_topics(self) -> list:
        return self.get("/api/rkg/pool/topics").get("topics", [])

    def rkg_pool_topic_add(self, name: str, query: str) -> dict:
        return self.post("/api/rkg/pool/topics/add",
                         {"name": name, "query": query})

    def rkg_pool_topic_remove(self, name: str) -> dict:
        return self.post("/api/rkg/pool/topics/remove", {"name": name})

    def rkg_pool_import(self, arxiv_id: str) -> dict:
        return self.post("/api/rkg/pool/import", {"arxiv_id": arxiv_id})

    def rkg_scheduler_status(self) -> dict:
        return self.get("/api/rkg/scheduler/status")

    def rkg_jobs(self) -> list:
        jobs = self.get("/api/rkg/jobs")
        return jobs if isinstance(jobs, list) else jobs.get("jobs", [])

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
