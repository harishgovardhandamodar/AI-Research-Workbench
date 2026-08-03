"""Training subpackage: incremental trainer + hyperparameter handling."""

from .trainer import REAL_AVAILABLE, Trainer, TrainingError  # noqa: F401
from .hyperparams import apply_hyperparams  # noqa: F401
