"""Model Risk Management (MRM) MCP server for banking data simulations.

Implements the SR 11-7 / 2026-interagency-aligned MRM framework for
simulation-heavy environments: a governed inventory of models, simulations and
datasets; synthetic-data generation with fidelity + privacy gates; Monte Carlo
/ scenario / stress simulation; mandatory Train-Synthetic-Test-Real (TSTR)
validation; drift monitoring; maker-checker approvals; and an immutable audit
log — all callable by any MCP host (Fox chat, Claude, Cursor, ...) as
``mrm__<tool>``.

See ``README.md`` for architecture, security model, deployment and example
agent prompts.
"""

__version__ = "0.1.0"

MRM_STORE_ENV = "FOX_MRM_STORE"
