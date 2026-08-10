# Fine-tuning LLMs

The workbench is a **local, private, agentic** path for fine-tuning LLMs — from a
first baseline to a verified adapter, all on your own hardware. This page is the
how-to: launch training, watch it live, test the finetuned model with your own
questions, and read the verification report.

## The pipeline

A finetune is a four-stage pipeline, optionally extended with custom QA testing:

| Stage | Name | What it does |
|---|---|---|
| 1 | Ingest + chunk | Ingest corpus artifacts and diarized interview transcripts; chunk them for RAG |
| 2 | Dataset | Build a mixed QA + continued-pretrain training dataset |
| 3 | Train | Run LoRA/QLoRA fine-tuning (Unsloth or plain-Trainer) |
| 4 | Verify | RAG index → heldout eval set → **base vs adapter** comparison report |
| 5 | Custom QA | Test the finetuned LLM with your own questions (+ transcript-mined samples) |

## Launching training

- **From the UI** — the *Sweep & Finetune* panel records a finetune launch
  (base model, dataset, epochs/lr/batch/LoRA rank) as a run. See
  [Finetune launch](finetune-launch.md).
- **From chat** — ask the agent, or use the 🔧 **Finetune docs / Test finetuned
  LLM / Verify adapter** quick controls.
- **From a pipeline script** — e.g. the `quai-lora` project's
  `scripts/pipeline.py 3`.

Whichever way you launch, a 🔧 **LoRA finetune** card streams into the chat with
the live pipeline: stage states (○ queued / ◔ running / ✓ done / ✗ failed), a
progress bar, **per-stage ETA** and **s/it** rate, and a **debug-log console**
tailing the trainer's output (tqdm progress + `{'loss': …}` metric dicts).

## Watching it live

- **Chat card** — every stage, its ETA, and the streaming debug log.
- **Experiments → Finetune status** — per-job cards with step/total progress,
  last loss/epoch, ETA, and SVG charts of **loss / grad_norm / learning_rate /
  epoch** over steps.
- **Session history** — compact finetune messages are persisted (start / every
  ~100 steps / done / failed) and replay in the chat after refresh.

See [Finetune status & monitoring](finetune-status.md) for the full detail.

## Testing the finetuned LLM

Use **stage 5 — custom QA validation** to ask the finetuned model your own
questions:

- **Experiments → RAG verification → "Test adapter with custom questions"** —
  type one question per line (e.g. *"What is QI pegged to?"*, *"How does QUAI
  mining work?"*) and optionally let it **mine sample queries from diarized
  interview transcripts**.
- **Chat** — 🔧 **Test finetuned LLM** quick control does the same.

Each question is answered by the **actual finetuned model** (HF generation,
base + adapter), scored against the retrieved RAG evidence (faithfulness,
accuracy, hallucination, retention), and compared to the base model.

## Reading the verification report

When verification finishes, the **full markdown report** is:

- posted to the **chat** (with the aggregate table + per-question Q&A),
- stored on the verify run in the **Experiments tab** (rendered as formatted
  markdown),
- shown in the **RAG verification** section (toggleable, per run).

The report includes:

```
# Fine-Tune Validation Report — <run_id>
- Eval set / base model / adapter / questions / status

## Aggregate metrics
| Metric | Base | Adapter | Verdict |

## Top failure cases (worst faithfulness)

## Test / validation questions — answers
### Q1. <question>
**Finetuned (adapter):** <answer>
**Base:** <answer>
- **Scores:** faithfulness … (Δ …), accuracy … , …
- **Evidence:** <retrieved chunk>
```

## Quick controls (chat)

| Control | Action |
|---|---|
| 🔧 Test finetuned LLM | Queue stage 5 (custom questions + transcript mining, HF generation) |
| ⚖ Verify adapter | Queue stage 4 (full base-vs-adapter verification) |
| 📋 Finetune summary | Post the pipeline summary + latest report to the chat |
| 📖 Finetune docs | Open this page |

## Requirements

- **Local GPU** with enough memory for the base model + adapter (e.g. an 8B
  LoRA on ~24–32 GB, or a 2.7 GB bf16 adapter on unified memory).
- **Host venv** with `torch`, `unsloth` (or `transformers` + `peft`),
  `datasets`, `sentence-transformers` (RAG embeddings).
- **Base model** reachable on the HuggingFace hub (or cached).
- The **host worker** (`scripts/stage_worker.py` in the project) polls the
  workspace `requests/` dir so chat-triggered stages run with the full stack.

See [Finetune status & monitoring](finetune-status.md) and
[Finetune launch](finetune-launch.md) for the surrounding features.
