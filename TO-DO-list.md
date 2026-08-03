# FOX Experiment Workbench — TO-DO List

Trackable roadmap toward an **Agentic Experimentation workbench**: the agent
learns from a user request, crafts experiments, tracks results and their
improvement over time, suggests improvements toward a goal, manages experiment
runs, and writes comparison reports — with full traceability and best-in-class
UX.

Rules: each unchecked item below is a unit of work. Implement it, verify it
(compile checks, endpoint tests, browser test where relevant), **commit it on
its own**, then check the box.

Branch: `agentic-experimentation` (base `8344264`).

---

## Vision

- The agent crafts experiments **from the user's request** (not hardcoded scripts).
- Every agent turn that produces a result is recorded as a **run** (prompt →
  tool sequence → metrics → artifacts → reviewer findings).
- Runs are **comparable**: deltas vs. previous/best runs, in a graph + table.
- **Goals** track the target metric; the agent/reviewer suggests improvements.
- The user can generate a **lab-notebook report** per run and compare runs.
- **Nothing important is lost on restart**: workflow state, runs, reviewer
  findings, kernel results all persist.

---

## Phase 1 — Traceability foundation (highest impact)

- [x] **T1 · Workflow persistence + always-visible idle panel**
  - `WorkflowTracker` gains `persist`/`record` callbacks; snapshots saved to
    SQLite every broadcast; `finish()` archives a run to `workflow_runs`.
  - Runtime wires `persist`→`store.set_setting("workflow_latest", …)` and
    `record`→`store.add_workflow_run`; restores latest on startup.
  - New endpoint `GET /api/projects/{name}/workflow/history`.
  - Frontend: panel always visible, shows idle state instead of hiding.
  - Verify: backend unit test, `curl` endpoints, chromium panel test, WS e2e.

- [x] **T2 · Runs table: record every agent turn as a run**
  - New `runs` SQLite table (id, prompt, reply, started/finished, status,
    tool_sequence JSON, metrics JSON, artifact_ids, reviewer JSON).
  - Coordinator turn records a run row on completion (and on failure/exception).
  - New endpoint `GET /api/projects/{name}/runs` (+ `GET …/runs/{id}`).
  - Verify: run a chat turn → curl `/runs` shows the row with tool sequence.

- [x] **T3 · Link artifacts to runs/messages**
  - Thread `run_id`/`message_id` through `ToolContext` into `_run_python`,
    `_save_artifact`, `_notebook_artifact_cb` (replaces `message_id=""`).
  - Verify: produce an artifact via agent turn → artifact row has the ids.

- [x] **T4 · Run-to-run comparison for arbitrary runs**
  - `experiments.py`: metric-delta edges between comparable runs; new
    `GET /api/projects/{name}/compare?run_a&run_b` returning a delta table.
  - Frontend: show comparison (delta) rows in the Experiments panel.
  - Verify: curl compare for two privacy runs and two generic runs.

## Phase 2 — Agentic experimentation features

- [x] **T5 · Goal tracking + improvement suggestions**
  - `goals` table + `POST/GET /api/projects/{name}/goals`; on each new run,
    compute delta vs. best-known metric and emit "improvement vs run #N (+x%)".
  - Reviewer emits **next-step suggestions** (not only issues).
  - Verify: set a goal, run twice, observe improvement message + suggestion.

- [x] **T6 · Automatic run report generation**
  - `POST /api/projects/{name}/runs/{id}/report` assembles prompt + decisions +
    artifacts + metrics into a markdown lab-notebook entry (LLM-assisted),
    saved as a `text` artifact; visible in chat.
  - Verify: endpoint returns a markdown report; artifact appears.

- [x] **T7 · Persist reviewer findings**
  - Store review output (per turn) in DB; attach to run rows; surface in UI.
  - Verify: restart server → findings still visible.

## Phase 3 — UX & robustness

- [x] **T8 · Artifact serving across restarts** — `/artifacts/{id}` falls back to
  scanning project dirs instead of 404 when runtime not loaded.

- [x] **T9 · File upload endpoint** — `POST /api/projects/{name}/files`
  (multipart → project dir), surfaces in the file picker.

- [x] **T10 · Approval resilience + audit** — reject pending approvals on WS
  disconnect; persist approval decisions (allow/deny/temporary); timeout message.

- [x] **T11 · Context compaction** — summarize old turns into a persistent
  system summary once messages exceed a limit.

- [x] **T12 · Consistent experiment recording** — `run_notebook` tool records
  experiments the same way as the chat rerun path.

- [x] **T13 · Test suite** — kernel protocol round-trip, coordinator tool loop
  (fake LLM), store round-trips, `build_graph`, approval timeout.

## Phase 4 — Stretch

- [x] **T14 · WAL + single SQLite connection**; serialize `privacy_runs.json`.
- [x] **T15 · Project lifecycle** — `DELETE /api/projects/{name}`,
  `POST …/fork` (session forking).
- [x] **T16 · Persistent knowledge graph** — auto-export arXiv graph per paper.
- [x] **T17 · Persistent R kernel** (or document limitation in UI).
- [x] **T18 · Replace brittle keyword intents** with an LLM intent classifier or
  explicit UI buttons.

---

## Notes

- All verification is local-first: `py_compile`, `curl`, WS e2e script
  (`/tmp/opencode/wf_ws_test.py`), chromium panel test (`/tmp/opencode/wf_browser.js`).
- Deploy fast-loop: `docker compose cp <file> fox:/app/<file>` then
  `docker compose restart fox`; full image rebuild via `docker compose up -d --build`.
- Bump `FOX_VER` on each frontend change.
- Run the test suite with `.venv/bin/python -m unittest discover -s tests -v`
  (stdlib `unittest`, no extra dependency; includes kernel protocol round-trip,
  coordinator fake-LLM loop, store/approval/graph round-trips).
