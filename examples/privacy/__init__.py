"""Privacy red-team / DP / synthetic-data examples for the Fox workbench.

Uses the privacy MCP server functions (mcp_servers/privacy_tools.py), which the
agent can also call as ``privacy__<tool>`` tools.
"""

from .clinical_cohort import build_cohort  # noqa: F401

__all__ = ["build_cohort"]
