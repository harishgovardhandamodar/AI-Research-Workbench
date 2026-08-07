"""LangChain EDA agent factory.

Builds a tool-calling agent that drives the five EDA MCP servers. The LLM is
**always a local model** — an OpenAI-compatible endpoint (defaults to the local
Ollama tool endpoint ``http://127.0.0.1:11434/v1``, model from ``FOX_MODEL``).
No cloud APIs are contacted.

Requires: ``pip install langchain langchain-mcp-adapters langchain-openai``
"""

from __future__ import annotations

import os


def local_model_kwargs() -> dict:
    """Endpoint + model for a local-only tool-calling model."""
    base = os.environ.get(
        "FOX_TOOL_BASE_URL", "http://127.0.0.1:11434/v1").rstrip("/")
    model = os.environ.get("FOX_MODEL", "").strip()
    if not model:
        raise RuntimeError(
            "FOX_MODEL is not set — a local model is required (e.g. "
            "FOX_MODEL=qwen3.6:latest). Local-only means no cloud model is used.")
    return {"base_url": base, "model": model, "temperature": 0}


def create_eda_agent(client=None, llm=None, system_prompt: str | None = None):
    """Create (agent_executor, client) driving the EDA MCP servers.

    ``client`` is a MultiServerMCPClient (see ``langchain.client.get_client``);
    when omitted one is created lazily. ``llm`` defaults to a local model.
    """
    try:
        from langchain.agents import AgentExecutor, create_tool_calling_agent
        from langchain_core.prompts import ChatPromptTemplate
    except ImportError as e:  # pragma: no cover
        raise RuntimeError(
            "langchain is not installed. Run: pip install langchain "
            "langchain-mcp-adapters langchain-openai"
        ) from e

    from .client import get_client
    from .prompts import EDA_SYSTEM_PROMPT

    if client is None:
        client = get_client()
    tools = client.get_tools()  # all tools from all five servers

    if llm is None:
        try:
            from langchain_openai import ChatOpenAI
        except ImportError as e:  # pragma: no cover
            raise RuntimeError(
                "langchain-openai is not installed. Run: pip install langchain-openai"
            ) from e
        llm = ChatOpenAI(**local_model_kwargs())

    prompt = ChatPromptTemplate.from_messages([
        ("system", system_prompt or EDA_SYSTEM_PROMPT),
        ("human", "{input}"),
        ("placeholder", "{agent_scratchpad}"),
    ])
    agent = create_tool_calling_agent(llm, tools, prompt)
    executor = AgentExecutor(agent=agent, tools=tools, verbose=True,
                             handle_parsing_errors=True)
    return executor, client


async def run_eda(dataset_path: str, client=None, system_prompt: str | None = None) -> str:
    """Minimal end-to-end: run the EDA workflow on a dataset and return the agent
    output (typically the compiled report path + findings)."""
    executor, client = create_eda_agent(client=client, system_prompt=system_prompt)
    try:
        result = await executor.ainvoke({
            "input": f"Perform a complete exploratory data analysis on the dataset "
                     f"at {dataset_path} and generate a professional Markdown report."
        })
        return result.get("output", "")
    finally:
        await client.close()
