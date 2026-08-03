"""Lightweight GraphRAG (graph retrieval + RAG) MCP server for Fox.

Runs retrieval over the JSON knowledge graphs produced by the arXiv
replication server (build/merge_knowledge_graph_from_notes): score nodes against
a query, expand their neighbourhood 1–2 hops, and pack the subgraph into
LLM-ready context — with explainable provenance.

Tools (namespaced `graphrag__<tool>` when the server is named "graphrag"):
  - graphrag_retrieve       -> relevant subgraph + LLM-ready context text
  - graphrag_answer_prompt  -> a ready-to-send answer prompt citing nodes

Both are read-only (no network, no file writes). Uses only the stdlib (NetworkX
optional for a cleaner BFS/ego-graph expansion).
"""

from __future__ import annotations

import json
import re
from collections import defaultdict, deque
from datetime import datetime, timezone

try:
    from mcp.server import MCPServer
    from mcp.types import ToolAnnotations

    mcp = MCPServer("fox-graphrag-lite", version="0.1.0")
    RO = ToolAnnotations(read_only_hint=True)
except ImportError:  # allow importing the plain functions without the mcp package
    mcp = None
    RO = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(data) -> str:
    return json.dumps(data, indent=2)


# ---------------------------------------------------------------- helpers ----

def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def _tokenize(text: str) -> set[str]:
    return set(_normalize(text).split())


def _node_searchable_text(node: dict) -> str:
    parts = [str(node.get(k, "")) for k in
             ("id", "type", "name", "title", "description", "text", "arxiv_id")]
    return " ".join(p for p in parts if p)


def _score_node(query_tokens: set[str], node: dict) -> float:
    if not query_tokens:
        return 0.0
    node_tokens = _tokenize(_node_searchable_text(node))
    if not node_tokens:
        return 0.0
    overlap = query_tokens & node_tokens
    return len(overlap) / (len(query_tokens) + len(node_tokens) - len(overlap) + 1e-9)


def _build_adjacency(edges: list[dict]) -> dict[str, list[tuple[str, str, dict]]]:
    adj = defaultdict(list)
    for e in edges:
        src, rel, tgt = e.get("source"), e.get("relation"), e.get("target")
        props = {k: v for k, v in e.items() if k not in ("source", "relation", "target")}
        adj[src].append((tgt, rel, props))
        adj[tgt].append((src, f"REV_{rel}", props))  # undirected expansion
    return adj


def _expand_neighborhood(seed_ids, adj, max_hops, max_nodes):
    visited = set(seed_ids)
    traversed = []
    queue = deque((nid, 0) for nid in seed_ids)
    while queue and len(visited) < max_nodes:
        current, depth = queue.popleft()
        if depth >= max_hops:
            continue
        for neighbor, rel, props in adj.get(current, []):
            rec = {"source": current, "relation": rel, "target": neighbor, **props}
            if rec not in traversed:
                traversed.append(rec)
            if neighbor not in visited and len(visited) < max_nodes:
                visited.add(neighbor)
                queue.append((neighbor, depth + 1))
    return visited, traversed


def _retrieve_with_networkx(nodes, edges, seed_ids, max_hops, max_nodes):
    """Optional NetworkX ego-graph expansion; falls back to the plain BFS."""
    try:
        import networkx as nx
    except ImportError:
        return _expand_neighborhood(seed_ids, _build_adjacency(edges),
                                    max_hops, max_nodes)
    G = nx.Graph()
    for n in nodes.values():
        G.add_node(n["id"], **{k: v for k, v in n.items() if k != "id"})
    for e in edges:
        G.add_edge(e["source"], e["target"], relation=e.get("relation"),
                   **{k: v for k, v in e.items()
                      if k not in ("source", "relation", "target")})
    visited, traversed = set(), []
    for seed in seed_ids:
        if seed not in G:
            continue
        ego = nx.ego_graph(G, seed, radius=max_hops)
        for nid in ego.nodes:
            if len(visited) >= max_nodes:
                break
            visited.add(nid)
        for u, v, data in ego.edges(data=True):
            traversed.append({"source": u, "relation": data.get("relation", "RELATED"),
                              "target": v,
                              **{k: val for k, val in data.items() if k != "relation"}})
        if len(visited) >= max_nodes:
            break
    return visited, traversed


def _format_context(nodes: dict[str, dict], edge_list: list[dict],
                    seed_ids: list[str]) -> str:
    lines = ["### Retrieved Knowledge Graph Context\n"]
    by_type = defaultdict(list)
    for nid in nodes:
        by_type[nodes[nid].get("type", "Unknown")].append(nodes[nid])
    for ntype, group in sorted(by_type.items()):
        lines.append(f"**{ntype}s:**")
        for n in group:
            label = n.get("name") or n.get("title") or n.get("text") or n["id"]
            extra = []
            if n.get("description"):
                extra.append(str(n["description"])[:200])
            if n.get("arxiv_id"):
                extra.append(f"arXiv:{n['arxiv_id']}")
            suffix = f" — {'; '.join(extra)}" if extra else ""
            marker = " (seed)" if n["id"] in seed_ids else ""
            lines.append(f"- {label}{marker}{suffix}")
        lines.append("")
    if edge_list:
        lines.append("**Relations:**")
        for e in edge_list[:40]:
            src = nodes.get(e["source"], {}).get("name") or e["source"]
            tgt = nodes.get(e["target"], {}).get("name") or e["target"]
            rel = e["relation"].replace("REV_", "←")
            val = f" = {e['value']}" if "value" in e else ""
            lines.append(f"- {src} --[{rel}]--> {tgt}{val}")
        if len(edge_list) > 40:
            lines.append(f"  … and {len(edge_list) - 40} more relations")
        lines.append("")
    return "\n".join(lines)


# =============================================================== main tools ----

def graphrag_retrieve(corpus_graph_json: str, query: str, max_hops: int = 2,
                      max_nodes: int = 30, min_seed_score: float = 0.05,
                      top_k_seeds: int = 8) -> str:
    """Lightweight GraphRAG retrieval over a JSON knowledge graph.

    Scores all nodes against the query (token overlap), picks the top-k seeds,
    expands their neighbourhood up to max_hops / max_nodes, and returns the
    subgraph plus an LLM-ready context block.
    """
    try:
        graph = json.loads(corpus_graph_json)
    except Exception as e:  # noqa: BLE001
        return _json({"error": f"Invalid graph JSON: {e}"})

    nodes_list = graph.get("nodes") or []
    edges_list = graph.get("edges") or []
    if not nodes_list:
        return _json({"error": "Graph contains no nodes"})

    nodes = {n["id"]: n for n in nodes_list}
    query_tokens = _tokenize(query)

    scored = []
    for nid, node in nodes.items():
        s = _score_node(query_tokens, node)
        if s >= min_seed_score:
            scored.append((s, nid))
    scored.sort(reverse=True)
    seed_ids = [nid for _, nid in scored[:top_k_seeds]]

    if not seed_ids:
        paper_ids = [n["id"] for n in nodes_list if n.get("type") == "Paper"]
        seed_ids = paper_ids[: min(3, len(paper_ids))] or list(nodes.keys())[:3]

    visited_ids, traversed = _retrieve_with_networkx(
        nodes, edges_list, seed_ids, max_hops=max_hops, max_nodes=max_nodes)

    subgraph_nodes = {nid: nodes[nid] for nid in visited_ids if nid in nodes}
    sub_edges = [e for e in traversed
                 if e["source"] in subgraph_nodes and e["target"] in subgraph_nodes]

    return _json({
        "query": query,
        "seed_node_ids": seed_ids,
        "seed_scores": {nid: round(s, 4) for s, nid in scored[:top_k_seeds]},
        "subgraph": {"nodes": list(subgraph_nodes.values()), "edges": sub_edges,
                     "node_count": len(subgraph_nodes), "edge_count": len(sub_edges)},
        "context_text": _format_context(subgraph_nodes, sub_edges, seed_ids),
        "retrieval_params": {"max_hops": max_hops, "max_nodes": max_nodes,
                             "min_seed_score": min_seed_score, "top_k_seeds": top_k_seeds},
        "retrieved_at": _now(),
    })


def graphrag_answer_prompt(query: str, retrieval_result_json: str) -> str:
    """Build a ready-to-send prompt asking the LLM to answer using only the
    retrieved graph context (cites supporting nodes)."""
    try:
        retrieval = json.loads(retrieval_result_json)
    except Exception as e:  # noqa: BLE001
        return _json({"error": f"Invalid retrieval JSON: {e}"})

    context = retrieval.get("context_text", "")
    seed_ids = retrieval.get("seed_node_ids", [])
    prompt = (
        "You are a scientific research assistant. Answer the question using ONLY "
        "the knowledge-graph context below. If the context is insufficient, say so "
        "clearly. Cite supporting nodes by their names or IDs when possible.\n\n"
        f"Question:\n{query}\n\n{context}\n\n"
        "Instructions:\n"
        "- Prefer precise, quantitative statements when metrics are present.\n"
        "- Mention the paper / method / dataset nodes that support each claim.\n"
        "- If multiple papers are involved, make the comparison explicit.\n"
        "- Do not invent results that are not present in the context.\n"
    )
    return _json({
        "prompt": prompt,
        "seed_node_ids": seed_ids,
        "subgraph_node_count": retrieval.get("subgraph", {}).get("node_count"),
        "note": "Send the 'prompt' field to your LLM. Attach the subgraph as provenance.",
    })


if mcp is not None:
    mcp.tool(annotations=RO)(graphrag_retrieve)
    mcp.tool(annotations=RO)(graphrag_answer_prompt)
    __all__ = ["graphrag_retrieve", "graphrag_answer_prompt", "mcp"]
else:
    __all__ = ["graphrag_retrieve", "graphrag_answer_prompt"]

if __name__ == "__main__":
    if mcp is None:
        raise SystemExit("mcp package not installed; run: pip install mcp")
    mcp.run(transport="stdio")
