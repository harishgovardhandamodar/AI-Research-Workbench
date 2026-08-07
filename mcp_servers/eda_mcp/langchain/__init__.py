"""LangChain / LangGraph orchestration layer for the EDA MCP servers.

Enables an agent (or a deterministic LangGraph pipeline) to drive the five
servers while keeping the LLM strictly local. All heavy dependencies
(langchain, langchain-mcp-adapters, langgraph) are imported lazily.
"""

from .client import EDA_MCP_CONFIG, get_client, mcp_env
from .agent import create_eda_agent, local_model_kwargs, run_eda
from .prompts import EDA_SYSTEM_PROMPT, workflow_system_prompt

__all__ = [
    "EDA_MCP_CONFIG", "get_client", "mcp_env",
    "create_eda_agent", "local_model_kwargs", "run_eda",
    "EDA_SYSTEM_PROMPT", "workflow_system_prompt",
]
