"""Adversarial robustness evaluation samples for the Fox workbench.

Bundled datasets built from the obfuscation-study data generators
(SWIFT transactions + clinical cohort) and a small FGSM-style attack, so the
`robustness` MCP server's evaluation tools can be exercised end-to-end.

Notebooks: examples/notebooks/24_adversarial_swift_robustness.ipynb,
25_adversarial_clinical_robustness.ipynb,
26_adversarial_model_comparison.ipynb, 27_adversarial_fgsm_art.ipynb.
"""

from .adversarial_data import (  # noqa: F401
    clinical_binary_dataset,
    evaluate_robustness,
    fgsm_grad,
    perturb_batch,
    robustness_sweep,
    swift_binary_dataset,
    train,
    train_test,
)

__all__ = [
    "swift_binary_dataset", "clinical_binary_dataset", "train_test", "train",
    "fgsm_grad", "perturb_batch", "evaluate_robustness", "robustness_sweep",
]
