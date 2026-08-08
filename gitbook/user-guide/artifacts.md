# Artifacts & provenance

Every generated figure, table, text report, notebook output, and imported data
becomes an **Artifact** — saved with the exact code that produced it, the
environment snapshot (python/platform/package versions), and links to the run
and message that created it.

## Artifact kinds

| Kind | Example |
|---|---|
| `figure` | matplotlib PNG (auto-saved on every run) |
| `table` | data tables |
| `text` | run reports, campaign/eval reports, project report |
| `notebook` | notebook outputs |
| `data` | imported datasets |

## Per-run provenance (rounds 4–8)

Every run records:

- **Full executed code** per tool call (`runs.code`) — the ≤200-char transcript
  is preserved, and the full source is retained for diffs.
- **Environment snapshot** (`runs.env`) — python version, platform, ~17 curated
  package versions, captured at run time.
- **Git commit** (`runs.git_commit`) — the management-repo commit that
  snapshotted the run; a `/commits` endpoint and a **restore** action check the
  run's artifacts back out of git.
- **Integrity hash** (`runs.integrity_hash`) — sha256 over the canonical run
  record; **Verify integrity** recomputes it (tamper-evident).

## Management repo

A sibling git repo snapshots `fox/<project>/` (experiments, per-run JSON,
artifacts, data) and auto-commits after each experiment run (best-effort, off
the event loop). `/commit` and `/push` do it manually; commits carry web URLs.

## Branch history

The **branch graph** shows runs as commit-like nodes with parent→child edges
(improve loops, reruns, campaigns), so you can see the evolution and revert to a
previous run (↶ revert re-runs its prompt; ↩ restore checks out its artifacts).
