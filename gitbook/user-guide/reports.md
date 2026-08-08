# Reports, export & next steps

The workbench turns its recorded research into documents you can keep and share.

## Project report

**📄 Report** (Experiments header) generates a comprehensive markdown write-up:

![Generated project report](../assets/screenshots/report.png)

- **Executive summary** (LLM, best-effort)
- **Experiments** — the cross-experiment leaderboard + per-experiment goals,
  plans, and best runs
- **Campaigns** — status + best metric across steps
- **Model benchmarks** — eval leaderboards
- **Learnings** — the project's accumulated findings
- **Recent runs** — with integrity + git commit
- **Audit** — event counts, open deviations, chain-verification status
- **Related work** — top corpus sources (when the RKG has content)

The report is saved as a text artifact and posted to chat. `GET /report`
returns the markdown; `POST /report` saves it.

## Export

**📦 Export** packages the project as a zip (`<project>-export.zip`):

- `report.md`, `provenance.json`
- `experiments.json`, `runs/<id>.json`, `learnings.json`, `campaigns.json`,
  `evals.json`, `suggestions.json`, `audit-summary.json`
- `artifacts/*` (byte copies)

## Next research

**▶ Next** computes a **next-research agenda** from the recorded gaps:
experiments below target, unfinished campaigns/benchmarks, no-gain learnings,
open goals, and what worked. Optionally an LLM frames a concrete **suggested
next campaign**. Use it to decide what to run next — and run it as a campaign.

## Run reports

`/report [run_id]` (or the Report button on a run) produces a lab-notebook
report for one run: prompt, metrics, environment, git commit, tool trace,
artifacts, review — with an LLM executive summary.
