"""Fox - Experiment workbench as a Jupyter server extension.

Loading this extension inside a running `jupyter server` makes the workbench
available at ``/fox`` — chat + agent, persistent kernel, artifacts, reviewer and
notebook execution — as an addon inside Jupyter.

Enable it with:

    pip install -e .            # registers the entry point
    jupyter server extension enable fox
    jupyter server               # open http://localhost:8888/fox/
"""

from __future__ import annotations

from .server import _jupyter_server_extension_points, _load_jupyter_server_extension

__all__ = ["_jupyter_server_extension_points", "_load_jupyter_server_extension"]
