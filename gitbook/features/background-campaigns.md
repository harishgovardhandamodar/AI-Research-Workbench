# Background campaigns

Campaigns run as **background tasks** detached from the chat socket: start one,
walk away, watch it from any window (or after a restart), stop it, and resume it.

## Execution model

- One campaign per project at a time. It runs under the project's turn lock
  (serialized with chat turns — the shared kernel isn't contended), with its
  own `Coordinator` bound to the runtime.
- A **project event bus** broadcasts live events (`status`, `user_message`,
  `assistant_message`, `notice`, `review`) to every open chat window, so
  progress streams even if the launching connection closed.
- The workflow panel renders step stages live; the campaign row shows an
  animated progress bar while running.

## Durable resume

- After each step, the resume point (`invoke`) is persisted to the workflow
  snapshot in the project settings.
- On a server restart, campaigns left `running` are marked **failed /
  interrupted** and offer **Resume**, which continues from the last completed
  step (reconstructing state from the persisted transcript + run records — not
  from replayable checkpoints, because tool side-effects aren't idempotent).
- **⏹ Stop** halts gracefully at the next step boundary.

## Why it matters

Long-horizon autonomy without a babysitter: a multi-step study keeps running
and stays observable, and its synthesis report lands in chat + as an artifact
even if you close the tab.
