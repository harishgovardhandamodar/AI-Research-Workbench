"""Optional LangGraph workflow for a deterministic EDA pipeline.

Requires: ``pip install langgraph``. Nodes mirror the design's stages
(profile → quality → univariate → multivariate → visualize → report), passing the
``dataset_id`` through state so every tool call references the same dataset.
"""

from __future__ import annotations

from typing import Annotated, Any, TypedDict

import operator


class EDAState(TypedDict, total=False):
    dataset_id: str
    dataset_path: str
    notes: Annotated[list, operator.add]
    report_id: str | None
    report_path: str | None
    error: str | None


def build_eda_graph(toolbox: dict[str, Any]):
    """Build a LangGraph that runs the EDA stages in order.

    ``toolbox`` maps stage names to async callables of ``(state) -> dict`` that
    mutate the state (e.g. set ``dataset_id``, append notes). Uses the standard
    ``StateGraph`` when ``langgraph`` is installed.
    """
    try:
        from langgraph.graph import END, StateGraph
    except ImportError as e:  # pragma: no cover
        raise RuntimeError(
            "langgraph is not installed. Run: pip install langgraph"
        ) from e

    g = StateGraph(EDAState)
    stages = ["profile", "quality", "univariate", "multivariate",
              "visualize", "report"]
    for stage in stages:
        if stage in toolbox:
            g.add_node(stage, toolbox[stage])
    first = next((s for s in stages if s in toolbox), None)
    if first is None:
        raise ValueError("toolbox must provide at least one stage")
    g.set_entry_point(first)
    prev = None
    for stage in stages:
        if stage not in toolbox:
            continue
        if prev is not None:
            g.add_edge(prev, stage)
        prev = stage
    if prev is not None:
        g.add_edge(prev, END)
    return g.compile()


__all__ = ["EDAState", "build_eda_graph"]
