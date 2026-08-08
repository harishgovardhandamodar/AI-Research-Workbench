# Testing

The project has a substantial pytest suite that grew with each feature round.

## Running tests

```bash
.venv/bin/python -m pytest -q                 # full suite (500+ tests)
.venv/bin/python -m pytest tests/test_round9.py -q   # one file
```

Frontend syntax check:

```bash
node --check frontend/app.js
node --check vscode/extension.js && node --check vscode/media/tracking.js
```

## Test layout

| File | Covers |
|---|---|
| `test_coordinator.py` | The agent loop, tool execution, run recording, variants |
| `test_experiment_loop.py` | The improve loop (goal detection, budget, resume) |
| `test_goal_steering.py` | Round 2: objective editing, focus, ranking targets, context |
| `test_round3.py` | Suggestions + regression check, model pinning, run diffs, sweeps |
| `test_round4.py` | Git lineage, full-code capture, per-run env, restore |
| `test_round5.py` | Campaigns (data model, plan parsing, execute→synthesize, resume) |
| `test_round6.py` | Event bus, background runner, stop/recover, full background run |
| `test_round7.py` | Learnings (store, capture, context injection) |
| `test_round8.py` | Integrity hashes, run↔trace linkage, tamper detection |
| `test_round9.py` | Leaderboards, N-run comparison, model benchmarks, route order |
| `test_round10.py` | Project report + export bundle |
| `test_round11.py` | Literature grounding (RKG helper, degradation) |
| `test_round12.py` | LLM retry-with-backoff + next-research agenda |
| `test_orchestrator.py` | LangGraph parity with the classic loop |

## Patterns

- Tests build `ProjectStore(Path(tempfile.mkdtemp()))` for isolated state.
- Loops are tested with a `FakeLLM`/`ScriptedReviewer` and a coordinator whose
  `record` closure writes to the store.
- Real background runs are tested against a `ProjectRuntime` built in a temp
  `PROJECTS_DIR` with a stub LLM.
- Git operations are tested against a real temp git repo.
- Route-ordering regressions use `TestClient` with a stubbed `get_runtime`.

## Conventions

- Add a `tests/test_roundN.py` for each feature round.
- Keep store reads on the event loop in tests (as in production) to avoid
  SQLite cross-thread errors.
