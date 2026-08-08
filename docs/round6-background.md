# Round 6 — Background autonomous campaigns & monitoring

Round 5 campaigns run *inside* a chat turn: they block the socket, die on
disconnect, and can't be resumed after a restart. Round 6 makes them run in the
**background**: start one, walk away, watch it from any window (or poll after a
restart), stop it, and resume it. The campaign engine (`backend/campaign.py`
`run_campaign`) stays the same — this round adds the execution/monitoring
wrapper.

## Design

### 1. Project event bus (`backend/project_runtime.py`)
- `ProjectRuntime.subscribe_events(fn)` / `unsubscribe_events(fn)` /
  `async broadcast(event, payload)` — a small fan-out over per-connection emit
  callables (the workflow tracker already has this pattern for `workflow`
  events; this generalizes it so a background task can stream `status`,
  `user_message`, `assistant_message`, `notice`, `review` to every open window).
- `ws_chat` (`main.py:970`): register `emit` on the bus alongside
  `rt.workflow.subscribe(emit)`; unregister on close.

### 2. CampaignRunner (`backend/project_runtime.py` or `backend/campaign.py`)
- `rt.start_campaign(cid, plan_steps=None) -> bool` — spawns an `asyncio.Task`
  (one per project; refuses if already running):
  - builds a background `Coordinator` bound to the runtime
    (`rt.ctx(bus_emit, approval)` with a background `ApprovalBroker`,
    `record` = background run-writer incl. autocommit, `persist = add_message`,
    `check_abort` = `rt.campaign_stop_requested`),
  - runs `run_campaign(rt, coord, rt.build_llm_messages, cid, emit=rt.broadcast,
    workflow=rt.workflow, ...)` under `rt.lock` (shared kernel ⇒ serialized with
    chat turns, exactly like an in-turn campaign),
  - after each step calls `workflow.set_invoke(kind="campaign", campaign_id,
    step=idx+1)` so the resume point is durable (already persisted to the
    `workflow_latest` setting).
- `rt.stop_campaign()` — sets the stop flag (checked between steps); the running
  campaign marks itself `failed` with "stopped by user" and returns.
- `rt.recover_campaigns()` — called at `ProjectRuntime.__init__`: any campaign
  still `running` (server restarted mid-run) is marked `failed` with note
  "interrupted by restart" so the UI offers Resume.

### 3. Launch / control
- `intent == "campaign"` (`main.py`) now calls `rt.start_campaign(cid,
  plan_steps)` and returns immediately (emit `done`) instead of awaiting
  `run_campaign` inline. `run_campaign` stays the testable core.
- Routes: `POST /api/projects/{name}/campaigns/{cid}/stop`; `POST .../run`
  (create + start); keep `GET /campaigns` and `GET /campaigns/{cid}` (now include
  `running` status + steps). Resume reuses the existing `retry_stage`
  (`kind == "campaign"`) intent; a `/campaign resume` alias maps to it.

### 4. Frontend
- Campaigns panel: a list of campaigns (name, question, status, step progress)
  loaded from `GET /campaigns`, with buttons: **▶ Run** (background), **⏹ Stop**,
  **↻ Resume** (when failed/interrupted), and open-the-report.
- While any campaign is `running`, poll `GET /campaigns` + `GET /workflow`
  (~3 s) for live status; the workflow panel already renders live stages via the
  broadcast bus.
- Keep the 🧭 quick-action button (starts a background campaign).

## Files touched
- `backend/project_runtime.py`, `backend/main.py`, `backend/routers/runs.py`,
  `backend/campaign.py` (runner helpers), `frontend/app.js`,
  `frontend/index.html`, `frontend/styles.css`, `docs/round6-background.md`,
  `tests/test_round6.py` (new).

## Out of scope
- Multiple concurrent campaigns per project (shared kernel) — one at a time.
- Parallel campaign + chat on the same project (both serialize under `rt.lock`).
- Cross-project campaign scheduler.
