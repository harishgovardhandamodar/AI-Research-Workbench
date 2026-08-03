"""MCP server package: category servers + the combined server + launcher."""

from .base import ToolServer  # noqa: F401
from . import (  # noqa: F401
    all_server,
    dataset_server,
    eval_server,
    experiment_server,
    train_server,
)

SERVER_BUILDERS = {
    "all": all_server.build,
    "dataset": dataset_server.build,
    "train": train_server.build,
    "eval": eval_server.build,
    "experiment": experiment_server.build,
}
