"""Session planner: workflow templates for starting a new session.

When the user creates a session, the planner shows a set of agentic workflow
templates (EDA, LoRA fine-tuning, Autoresearch, improve loop, campaigns, model
benchmarks, privacy, parameter sweeps, notebooks). Each template names the MCP
servers / tools it uses and a step-by-step flow, so the user can pick the right
tools and shortcuts up front — e.g. "new session → point at a dataset → choose
Agentic EDA → the workbench begins agentic EDA".

The templates are data-only; the frontend renders them and, on "Begin", sends
the template's prompt/intent through the normal chat pipeline.
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()

TEMPLATES = [
    {
        "id": "eda",
        "name": "Agentic EDA",
        "icon": "📊",
        "tagline": "Load a dataset, then Fox profiles, explores and reports on it.",
        "mcp": ["eda_profiler", "eda_univariate", "eda_multivariate",
                "eda_visualizer", "eda_report"],
        "tools": ["profile_basic", "detect_data_quality_issues",
                  "correlation_matrix", "univariate_numeric", "generate_plot",
                  "compile_report"],
        "needs_dataset": True,
        "steps": [
            "Upload your dataset(s) (CSV / TSV / JSON / Parquet / XLSX)",
            "Fox loads them into the Python kernel",
            "Profiling + quality checks (EDA suite)",
            "Univariate → multivariate → visualizations",
            "Compile an EDA report",
        ],
        "intent": "",
        "prompt": ("Run agentic EDA on {dataset}. Profile the data, check data "
                   "quality, explore distributions and relationships, generate "
                   "plots, and finish with a summary EDA report."),
    },
    {
        "id": "lora",
        "name": "LoRA fine-tuning",
        "icon": "🧠",
        "tagline": "Domain-knowledge LoRA/QLoRA fine-tune + RAG verification.",
        "mcp": ["dk_lora", "ft_validate"],
        "tools": ["dk_lora__ingest_artifacts", "dk_lora__chunk_artifacts",
                  "dk_lora__generate_dataset", "dk_lora__start_training",
                  "ft_validate__run_rag_verification",
                  "ft_validate__generate_custom_eval_set"],
        "needs_dataset": False,
        "steps": [
            "Point at the domain corpus / interview transcripts",
            "Ingest + chunk (stage 1) → dataset (stage 2)",
            "Train the adapter (stage 3, dk_lora)",
            "Verify base vs adapter (stage 4, ft_validate RAG)",
            "Test the finetuned LLM with your own questions (stage 5)",
        ],
        "intent": "finetune_summary",
        "prompt": ("Set up and monitor the LoRA fine-tune pipeline for this "
                   "project (dk_lora + ft_validate). Show the pipeline summary."),
    },
    {
        "id": "autoresearch",
        "name": "Autonomous research loop",
        "icon": "🤖",
        "tagline": "Karpathy-style loop: experiment, review, apply, rerun.",
        "mcp": ["autoresearch", "github"],
        "tools": ["run_python", "report_metric", "github__commit", "github__push"],
        "needs_dataset": True,
        "steps": [
            "Upload the dataset(s) your research script uses",
            "Fox runs research/experiment.py against the data",
            "Reviews the result and applies the best suggestion",
            "Loops until the goal metric improves or the budget runs out",
        ],
        "intent": "autoresearch",
        "extra": {"autoresearch": {"goal_metric": "accuracy",
                                    "higher_better": True, "max_iters": 6,
                                    "per_iter_budget": 30}},
        "prompt": ("Run the autonomous research loop on {dataset}. Load the "
                   "data, then run, review and improve toward the goal metric."),
    },
    {
        "id": "improve",
        "name": "Improve experiment",
        "icon": "🔁",
        "tagline": "Run → review → apply the best suggestion → rerun.",
        "mcp": ["science", "eda_profiler"],
        "tools": ["run_python", "report_metric", "science__welch_t_test"],
        "needs_dataset": True,
        "steps": [
            "Upload the dataset(s)",
            "Fox runs a baseline variant",
            "Reviewer proposes up to 3 improvements",
            "Apply + rerun with a regression check",
        ],
        "intent": "improve_loop",
        "prompt": ("Improve the experiment toward its goal. Start by loading "
                   "{dataset} and establishing a baseline."),
    },
    {
        "id": "campaign",
        "name": "Research campaign",
        "icon": "🧭",
        "tagline": "Plan and run a multi-step study in the background.",
        "mcp": ["arxiv", "graphrag", "github"],
        "tools": ["run_python", "report_metric", "arxiv__ingest_arxiv_paper",
                  "graphrag__graphrag_retrieve"],
        "needs_dataset": True,
        "steps": [
            "Upload the dataset(s) + describe the question",
            "Fox plans N experiment steps",
            "Each step runs in the background, live progress streams",
            "A synthesis report lands in chat + artifacts",
        ],
        "intent": "campaign",
        "extra": {"campaign": {"name": "Campaign", "goal_metric": "accuracy",
                                "higher_better": True}},
        "prompt": ("Plan and run a multi-step research campaign over {dataset}. "
                   "Start by loading the data and framing the question."),
    },
    {
        "id": "benchmark",
        "name": "Model benchmark",
        "icon": "⚖",
        "tagline": "Compare the workbench's LLMs on the same task.",
        "mcp": ["science"],
        "tools": ["run_python", "report_metric"],
        "needs_dataset": True,
        "steps": [
            "Upload the dataset(s) + task prompt",
            "One experiment per model, pinned to that model",
            "Each model reports the goal metric",
            "A ranked leaderboard is posted to chat",
        ],
        "intent": "eval",
        "extra": {"eval": {"name": "Eval", "models": [], "goal_metric": "accuracy",
                            "higher_better": True}},
        "prompt": ("Benchmark the models on {dataset}. Load the data and report "
                   "the goal metric per model."),
    },
    {
        "id": "privacy",
        "name": "Privacy & DP workflow",
        "icon": "🛡",
        "tagline": "PII scan, red-team, and differential-privacy assessment.",
        "mcp": ["privacy", "robustness"],
        "tools": ["privacy__detect_pii_in_text",
                  "privacy__assess_dataframe_privacy",
                  "privacy__membership_inference_eval",
                  "privacy__apply_laplace_dp",
                  "robustness__evaluate_sklearn_robustness"],
        "needs_dataset": True,
        "steps": [
            "Upload the dataset(s)",
            "PII detection + privacy assessment",
            "Membership-inference / re-identification evaluation",
            "Differential-privacy budgets + synthetic data",
            "Audit trail of every decision",
        ],
        "intent": "privacy_workflow",
        "prompt": ("Run the privacy workflow on {dataset}: PII scan, red-team, "
                   "DP robustness, and an audit trail."),
    },
    {
        "id": "sweep",
        "name": "Parameter sweep",
        "icon": "⚡",
        "tagline": "One code path, many configs, real parallel kernels.",
        "mcp": ["eda_profiler"],
        "tools": ["run_sweep", "report_metric"],
        "needs_dataset": True,
        "steps": [
            "Upload the dataset(s) + your experiment code",
            "Define a config grid (or explicit points)",
            "run_sweep runs each config on a parallel kernel",
            "One run per point; leaderboard + best config",
        ],
        "intent": "run_sweep",
        "prompt": ("Run a parameter sweep on {dataset}. Load the data, define "
                   "a grid, and report metrics per config."),
    },
    {
        "id": "notebook",
        "name": "Notebook run",
        "icon": "📓",
        "tagline": "Run a project notebook on the persistent kernel.",
        "mcp": [],
        "tools": ["run_notebook"],
        "needs_dataset": False,
        "steps": [
            "Upload the notebook or point at a project notebook",
            "Fox executes it cell-by-cell on the kernel",
            "Figures become tracked artifacts",
        ],
        "intent": "",
        "prompt": ("Run the project notebook and report what it produced."),
    },
]


@router.get("/api/planner/templates")
async def planner_templates():
    """The session-planner workflow templates."""
    return {"templates": TEMPLATES}
