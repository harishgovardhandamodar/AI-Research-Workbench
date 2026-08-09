# Research advisor

The **research advisor** (round 28) is a deterministic per-experiment analysis
that turns the recorded runs, configs, metrics, typed suggestions, learnings and
dataset tags into instant researcher-facing guidance. No LLM call is involved —
everything is computed from stored records, so the panel is always available.

## Where to find it

- **Experiment detail modal** (open any experiment card): a 🧭 **Research
  advisor** panel at the top.
- **Chat**: the experiment-controls strip shows a compact health line (gaps,
  next-steps, % of target) that opens the full advisor.

## What it tells you

- **🎯 Goal** — the goal metric/target, the best run's value, % of target with a
  progress bar, and a ✓ when the target is reached. If no goal metric is set, it
  proposes the most-measured metric from the runs (one-click **Use as goal**).
- **🧩 Missing elements** — a checklist of what would make the experiment
  well-formed: hypothesis, goal metric, goal target, plan, pinned model, runs,
  dataset tags, numeric metrics, and learnings.
- **🔬 Areas of improvement** — pending reviewer suggestions grouped by their
  typed category (🔧 hyperparameter · 🧬 data · 🧠 model · 🧪 method · 🎓
  finetune · 📊 eval · 🔒 reproducibility), plus a note when suggestions have
  produced no gain.
- **🔧 Suggested hyperparameters** — the best run's config plus concrete
  hyperparameter next-steps from the reviewer.
- **🧬 Data pipeline** — which datasets and data tools the runs actually used.
- **🤖 Model selection** — the pinned model vs the models used across runs.
- **🎓 Finetune setup** — a readiness checklist for a finetune/pre-train run
  (base model pinned, training data available, evaluation metric defined,
  baseline run exists).

## Typed suggestions

Reviewer suggestions are now first-class with a `category` tag, so the advisor
(and the Review panel) can group and reason about them. The reviewer is asked to
emit a category per suggestion; when a legacy or malformed suggestion lacks one,
a keyword classifier assigns the best match (e.g. `lr=` → hyperparameter,
`more training data` → data, `finetune/lora` → finetune).

## Goal proposal

When an experiment has no goal metric, the advisor proposes the metric measured
most often across its runs (with a one-click **Use as goal** that patches the
experiment). If a target is missing but the metric is known, it proposes one
slightly past the current best so there is a concrete direction to chase.
