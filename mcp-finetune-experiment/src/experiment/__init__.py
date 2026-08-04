"""Experiment subpackage: state store + stage management + report export.

Note: ``ExperimentController`` is imported lazily (``from experiment.controller
import ...``) to avoid a circular import through ``eval.harness``.
"""

from .store import ExperimentError, ExperimentStore  # noqa: F401
