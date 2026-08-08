# Chat UI / UX Redesign — Problems, Gaps, and Plan

Branch: `feat/chat-ui-ux`. This document records what was found wrong with the
chat experience, what is missing for smooth experimentation, and the concrete
redesign that this branch implements.

## 1. Findings — existing problems

### Rendering and streaming
- `streamDelta()` re-renders the **entire markdown buffer on every token** and
  replaces `innerHTML` (`frontend/app.js:464-469`). Long replies are O(n²) and
  flicker on half-formed tables / code fences.
- The hand-rolled `renderMarkdown()` (`app.js:86-148`) lacks strikethrough,
  task lists, bare-URL auto-linking, and does not normalise `\r\n` (Windows
  line endings leak raw `\r` into rendered output).
- The blinking `#cursor` span is appended *after* block-level HTML, so it can
  sit oddly under tables and lists.

### Message management
- No retry / regenerate for assistant replies. The only "edit" affordance is
  the hidden ArrowUp-at-column-0 hack (`app.js:2081-2091`).
- No message delete (the backend has no delete-message route at all).
- No per-message model override or "rerun this with model X".
- Copy exists per message, but there is no quick way to copy a whole turn as
  markdown.

### Model selection
- `refreshModels()` (`app.js:2044-2056`) renders **raw model ids only** — no
  friendly name, family grouping, size/capability tags, or "recommended"
  marker. `owned_by` from `/api/models` is fetched and then discarded.
- Changing the model silently closes and reopens the WebSocket
  (`app.js:2194-2195`) with no confirmation of effect.

### Data awareness ("data schemes")
- Files can only be attached from the side-panel Files tab; the composer has
  no attach affordance, and uploaded files never reach the model's context
  automatically.
- There is **no schema / dataset preview anywhere** in the chat: no column
  names, dtypes, or sample rows surfaced, and no structured way to ask for it.
- The model is told to "load the CSV" blind — it has to guess paths and column
  semantics.

### Experiment co-design
- The active experiment (name, hypothesis, goal metric/target, best run) lives
  only in the UI pill; `build_llm_messages()` (`backend/project_runtime.py:110-143`)
  never hands it to the model, so the agent works toward the goal only if the
  user happens to restate it.
- Ad-hoc turns are not tagged with `experiment_id` (`backend/main.py:1158-1161`),
  so the chat↔experiment linkage is patchy outside improve loops.
- The "New experiment" quick action is just free text — no structured
  co-design flow.
- The composer placeholder is static and never reflects the active goal.

### Suggested next steps
- Reviewer suggestions are rendered only in the side-panel Review tab
  (`app.js:864-906`); nothing surfaces the next best action inline under an
  assistant reply, right where the user is looking.

### Quick actions
- 17 hardcoded chips (`frontend/index.html:297-330`) with no user
  configurability; they stay clickable while the agent is busy (silently
  ignored by the `state.busy` guard in `sendChat`).

### Status / error handling
- `onError()` (`app.js:1031-1038`) can leave `curAssistantEl` and
  `state.streaming` in a stale state after an error.

## 2. Redesign — what this branch implements

### A. Streaming & markdown (chat UX core)
- Throttled streaming: append deltas to the raw buffer and schedule full
  markdown re-renders on a short debounce instead of per token; final render
  on `assistant_message`. Cursor stays inline while streaming.
- `renderMarkdown()` gains: `\r\n` normalisation, strikethrough `~~x~~`,
  task-list `- [ ]` / `- [x]`, and bare `https?://` auto-linking.

### B. Message actions (hover bar)
- Retry (assistant & user) — resends the turn's user prompt as a new message.
- Edit (user) — loads the message into the composer (replaces the ArrowUp hack).
- Delete — new backend endpoint deletes the message; chat reloads.
- Copy (existing) stays.

### C. Model suggestions
- `modelMeta()` classifies each model id into family / size / capability tags
  and a short hint. The dropdown renders grouped `<optgroup>`s with labels and
  marks a "recommended" default (the current config model first, then the
  fastest local default). Raw id stays the option value.

### D. Data schemes
- Composer attach button (📎) uploads a file via the existing upload endpoint
  and inserts a data-hint into the composer that names the uploaded path.
- `@schema <file>` command renders an **inline schema card** in the chat
  (column names, dtypes, sample rows, row count) from a new backend endpoint
  `GET /api/projects/{name}/files/schema?name=...` (CSV/TSV/Parquet/JSON/XLSX).

### E. Experiment co-design
- `build_llm_messages()` injects a compact **active-experiment context block**
  (name, hypothesis, goal metric/target, higher-is-better, best run so far,
  run count) into the system prompt so the agent works toward the goal.
- Composer placeholder + a goal chip above the composer reflect the active
  experiment's goal metric / target / best value.

### F. Next-step suggestions inline
- After each turn, reviewer suggestions render as a "Next steps" block under
  the assistant reply, each with a Run button that applies the suggestion
  (`intent="rerun_suggestion"` so the branch lineage is preserved).

### G. Quick-actions tray
- Configurable tray: defaults seeded from the previous hardcoded list, custom
  actions stored in `localStorage` (`fox.quickActions`), removable, plus a
  reset-to-defaults. Buttons are disabled while the agent is busy.

### H. Error/status hygiene
- `onError()` clears streaming state and the active assistant element.

## 3. Files touched

- `frontend/index.html` — composer attach button, goal chip, quick-action tray container, message action wiring hooks.
- `frontend/app.js` — all frontend logic above.
- `frontend/styles.css` — styles for message actions, schema card, model groups, goal chip, tray.
- `backend/store.py` — `delete_message()`.
- `backend/routers/runs.py` — `DELETE /api/projects/{name}/messages/{mid}`.
- `backend/routers/artifacts.py` — `GET /api/projects/{name}/files/schema`.
- `backend/project_runtime.py` — active-experiment context injection.
- `tests/test_chat_ux.py` — backend tests for the new routes + context injection.
