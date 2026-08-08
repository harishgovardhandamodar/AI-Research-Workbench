# Round 4 — Reproducibility & provenance: git-backed run lineage, full-code capture, per-run env

Rounds 1–3 made the loop steered (focus, objectives), measurable (suggestions,
regression checks, deltas), and self-improving (sweeps, retries). Round 4 makes
every run **reproducible and traceable**: it records *why* a run happened (its
full code + environment) and *where* its state lives (the management-repo commit),
so any run can be inspected, diffed, and restored.

## Findings (audit, 3 explore agents)
- **Run ↔ commit linkage is dropped.** `autocommit` (`experiment_repo.py:315`)
  returns only `{ok, message}` (no HEAD hash, unlike `commit_project` at :420),
  and `maybe_autocommit` (:377) discards the result. The `runs` table has no
  `git_commit` column. The committing snapshot writes `fox/<project>/runs/<id>.json`,
  so a path-based `git log -- fox/<project>/runs/<id>.json` always resolves a
  run's commit — the data is there, just never linked.
- **Full executed code is lost.** `_exec_tool_call` truncates args to 200 chars
  (`coordinator.py:467-471`, `_snippet`). The kernel never echoes code
  (`worker.py:283-285`). Only artifacts keep full code (`Artifact.code`) — runs
  that produce no artifact lose it entirely.
- **Env is per-artifact only, never per-run.** `_env_snapshot` (`worker.py:31`)
  returns python/platform + ~17 curated package versions, cached per session
  (`manager.py:46`); it's stored on artifacts but never on the run. Run reports
  attach the *current* env, not the run-time env (`runs.py:476-480`).

## Design

### 1. Git-backed run lineage
- `backend/store.py`: `runs.git_commit TEXT` (migration), `add_run(..., git_commit=None)`,
  `set_run_git_commit(rid, commit)`, `_row_run` exposes it.
- `backend/experiment_repo.py`:
  - `autocommit` returns `_head_info` (hash/full/date/url) after a successful commit
    (and resolves the existing commit via path-log on "nothing to commit").
  - `maybe_autocommit` writes `store.set_run_git_commit(...)` on the event loop
    after the worker thread returns (store must stay off the thread).
  - new `run_commit_info(repo, project, rid)` → commit hash/full/date/message/url +
    changed files (`git log -1 -- fox/<project>/runs/<rid>.json` + `git show --name-only`).
  - new `restore_run(rt, rid)` → resolve commit (stored hash or path-log), checkout
    `fox/<project>/artifacts/` from that commit into the project's artifact dir
    (`git archive <commit> fox/<project>/artifacts | tar -x`), return restored list.
- `backend/routers/runs.py`: `GET /runs/{rid}/commits` (falls back to path-log for
  legacy runs); `POST /runs/{rid}/restore` (runs `restore_run` in a thread; audit
  event `run.restore`).

### 2. Full-code capture + code diffs
- `backend/agents/coordinator.py`: `self._run_code` list, reset in `run_turn`,
  appended in `_exec_tool_call` (full `code`/`command` before truncation,
  index-aligned with `_run_seq`), passed to `record` as `code`.
- `backend/store.py`: `runs.code TEXT` (JSON `[{name, code}...]`, migration,
  `add_run(..., code=None)`, `_row_run`). Keep it **out of bulk rows**
  (`_row_run(include_code=False)` default; only `get_run`/diff pass True) to avoid
  bloating `/runs` + history + branch graph.
- `backend/main.py`: `_record_run` closure passes `code`.
- `backend/agents/tools.py`: `_sweep_point` stores `code` + `env` on sweep runs.
- `backend/experiments.py`: `run_diff` gains `"code": {diffs: [{tool, added, removed,
  patch}]}` via stdlib `difflib.unified_diff`.
- `backend/routers/runs.py`: `/runs/{rid}/diff` fetches code (include_code=True).

### 3. Per-run environment snapshot
- `backend/store.py`: `runs.env TEXT` (migration, `add_run(..., env=None)`,
  `_row_run`).
- `backend/agents/coordinator.py`: fetch `env = await kernels.get_env()` at turn
  start (cached — cheap), store as `self._run_env`, pass to `record`.
- `backend/main.py`: `_record_run` closure passes `env`.
- `backend/routers/runs.py` `build_run_report`: add an `## Environment` section and
  **fix the current-env bug** — use the run's stored env, fall back to live only
  when absent.

### 4. UI
- Branch detail (`showBranchDetail`): commit chip (`#abc123` + url) per node,
  "↩ restore" button, and a "Reproducibility" block rendering the run's env.
- Diff view (`renderRunDiff`): "Code" section with per-tool unified diffs
  (reuse `.bd-diff-add/.bd-diff-del`).
- Run rows (`renderRuns`): commit chip when `run.git_commit` present.

## Files touched
- `backend/store.py`, `backend/agents/coordinator.py`, `backend/agents/tools.py`,
  `backend/experiments.py`, `backend/experiment_repo.py`, `backend/main.py`,
  `backend/routers/runs.py`, `frontend/app.js`, `frontend/styles.css`,
  `tests/test_round4.py` (new).

## Out of scope
- Full workspace/DB restore via git (repo mirrors only JSON + small artifacts/data).
- `pip freeze`-level env (stays the small curated snapshot).
- R version capture (availability flag only).
