# Finetune launch

The **finetune launch flow** (round 29) turns a finetune idea into a recorded,
reproducible setup in one click — no chat prompt needed.

## Launching

The Experiments tab's **Sweep & Finetune** section has a finetune panel:

1. Pick the experiment the finetune run attaches to.
2. Give the **base model** (a HuggingFace id, e.g. `distilbert-base-uncased`).
3. Point at a **dataset file** in the project (datalist of uploaded files).
4. Set **epochs · learning rate · batch size · LoRA rank** (`0` = full
   finetune, `>0` = LoRA adapter).
5. **Launch finetune setup**.

The backend builds a normalized finetune config, records a `kind="finetune"`
run under the experiment (config + generated training script as its code +
dataset tag), and streams a summary into chat — so the pipeline view, branch
graph and research advisor all treat the finetune like any other variant.

## The generated script

A ready-to-run HuggingFace training script is attached to the run: loads the
CSV, tokenizes a text/label pair, builds the model with an optional LoRA
adapter, and reports per-epoch `eval_loss` + `accuracy` via `report_metric`.
The setup is **recorded but not executed** — run the script with `run_python`
(or ask the agent) to actually train and produce a checkpoint. The agent also
has a first-class `run_finetune` tool it can call mid-conversation.

## Readiness

The research advisor's **Finetune setup** checklist surfaces what a launch
needs (pinned base model, training data, evaluation metric, baseline run), so
you know before you launch whether the experiment is ready to finetune.

## Watching a running job

Once a training job is running (launched here or via a pipeline script), the
**Finetune status** panel in the Experiments tab and the 🔧 **LoRA finetune**
live pipeline card + debug log in the chat stream its progress — see
[Finetune status & monitoring](finetune-status.md).

