# mcp-finetune-experiment

An **MCP-native, incremental fine-tuning experiment** for LLM **tool-using
agents**: recreate or beat a target paper's MCP results entirely from a
Jupyter notebook by calling MCP tools.

```
mcp-finetune-experiment/
├── notebooks/
│   ├── mcp_client.py                  # thin client: connects to the servers, `await mcp.call(...)`
│   └── 01_incremental_finetune.ipynb  # main control-plane notebook
├── src/
│   ├── mcp_servers/                   # dataset | train | eval | experiment  (4 MCP servers)
│   ├── training/                      # incremental LoRA trainer (simulate + real)
│   ├── data/                          # dataset pipeline (generate/split/validate/…)
│   ├── eval/                          # harness, paper comparison, failure cases, LLM judge
│   ├── experiment/                    # stage store, controller, reports
│   └── common/                        # JSON / hashing / seeded RNG helpers
├── configs/                           # YAML configs (default + per-stage)
├── adapters/  checkpoints/  reports/  # incremental artifacts
├── paper.json                         # authors' reported numbers (fill these in)
├── pyproject.toml / requirements.txt
└── tests/
```

## Quick start (no GPU needed)

```bash
pip install -r requirements.txt

# 1) Scaffold an experiment directory (optional but recommended):
mkdir exp && cp paper.json exp/paper.json

# 2) Serve the four MCP servers over stdio for this project:
cd exp
python -m mcp_servers --all --project .   # runs dataset/train/eval/experiment as stdio servers
```

## Drive everything from the notebook

```bash
cd notebooks
jupyter lab 01_incremental_finetune.ipynb
```

Cell 1 connects to the servers and **lists every available MCP tool**; the rest
drive the loop:

```python
await mcp.call("train.start_stage", {
    "stage_id": "stage_0", "new_data": "mcp_tool_use_train",
    "epochs": 1, "lr": 5e-5, "lora_rank": 8,
})
```

## The MCP tool surface

| Category        | Tools                                                                 |
| --------------- | --------------------------------------------------------------------- |
| Data            | `mcp.dataset.list / inspect / generate / add_incremental / split / validate` |
| Training        | `mcp.train.start_stage / resume / list_checkpoints / load_adapter / get_status / get_metrics / set_hyperparams` |
| Evaluation      | `mcp.eval.run / compare_to_paper / failure_cases / llm_judge`         |
| Experiment      | `mcp.experiment.create / list_stages / rollback_to_stage / export_report / get_config / update_config` |

All tools return structured JSON. Every dataset file and stage records a
content SHA-256 so the whole experiment is reproducible.

## Incremental fine-tuning contract

1. **Stage 0** — base model + initial SFT/RFT on the paper's (or approximated) data.
2. **Stage N** — load the previous adapter (`from_adapter`), optionally add data /
   change hyperparams, run a short stage, evaluate, compare to `paper.json`,
   decide the next stage.
3. Rollback / branching is a first-class operation
   (`mcp.experiment.rollback_to_stage`).
4. Decoupled adapters / RFT / GRPO are supported via `training.method`
   (`decoupled`, `rft`, `grpo`, …) — see `src/training/hyperparams.py`.

## Simulate vs. real training

`mcp.train.*` runs in **deterministic simulate mode** by default (hash-seeded
loss curves + metrics), so the whole loop is exercisable without a GPU. To do
real LoRA/QLoRA:

```bash
pip install -r requirements-train.txt
# then via MCP:
await mcp.call("mcp.experiment.update_config", {"updates": {"training": {"real": True, "method": "lora", "quantization": "4bit"}}})
```

The real path (`src/training/trainer.py::_real_lora_sft`) is a compact
transformers+PEFT SFT loop; swap in TRL/SFTTrainer or GRPO as your paper needs.

## Comparing to the paper

Put the authors' numbers in `paper.json` (`reported_metrics`). After **every**
training stage, `mcp.eval.compare_to_paper` returns a side-by-side table
(paper vs. ours vs. delta), and `mcp.experiment.export_report` writes a
markdown lab-notebook entry into `reports/`.

## Tests

```bash
cd mcp-finetune-experiment
PYTHONPATH=src python -m unittest discover -s tests -v
```
