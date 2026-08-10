"""Register the workbench's deterministic experiments with the planner registry.

Wraps the pure `run_*_experiment` functions from the existing routers and the
built-in catalog so the planner can plan + execute them without an LLM loop.
"""

from __future__ import annotations

from . import experiment_planner as ep
from .exp_catalog import catalog as catalog_mod
from .routers import peer as peer_router


def _register_peer() -> None:
    def run(df, seed=42):
        res = peer_router.run_peer_share_experiment(df, seed=seed)
        # Fold the headline metrics into a flat `metrics` dict.
        res["metrics"] = {
            "identification_accuracy": res["identification"]["overall_accuracy"],
            "segment_mae": res["segments_error"]["mae"],
            "type_mae": res["types_error"]["mae"],
        }
        return res

    ep.register_experiment({
        "id": "peer",
        "name": "Bank peer identification & market-share",
        "description": ("Each major UPI bank acts as a peer that only sees its "
                        "own customers' data; it identifies an unseen "
                        "transaction's origin bank and estimates other banks' "
                        "market share per segment (merchant category) and per "
                        "payment type."),
        "needs_dataset": True,
        "requires_columns": ["sender_bank"],
        "plan_steps": [
            "Load the UPI/banking dataset (must have a sender_bank column)",
            "Build a Naive-Bayes-style fingerprint per bank from its own rows",
            "Classify a held-out sample -> identification accuracy + confusion matrix",
            "Estimate other banks' market share per segment and per payment type (MAE)",
            "Render 3 figures (confusion, share-error, bank volumes) + a report",
        ],
        "expected_outputs": [
            "identification accuracy", "confusion matrix",
            "share MAE per segment", "share MAE per payment type",
            "3 figures + report",
        ],
        "run": run,
        "render_report": peer_router.render_report,
        "render_figures": peer_router.render_figures,
    })


def _register_catalog() -> None:
    for defn in catalog_mod.CATALOG:
        ep.register_experiment(defn)


def register_all() -> None:
    _register_peer()
    _register_catalog()


register_all()
