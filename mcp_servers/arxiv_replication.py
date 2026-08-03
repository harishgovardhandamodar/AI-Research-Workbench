"""arXiv Research Replication + Knowledge-Graph MCP server for Fox.

Turns an arXiv paper (ID or URL, or a local PDF) into a structured research
workflow: ingest -> summarize -> extract structured notes -> craft a runnable
experiment spec -> (the workbench kernel runs it) -> compare results against the
authors' reported numbers -> produce a provenance-linked replication report.

A knowledge-graph layer captures each paper as entities/relations (Paper,
Author, Method, Dataset, Metric, Experiment, Claim, CodeRepo) so ingested
papers become a durable, queryable memory that can be merged across papers.

Network calls (arXiv API / PDF download) and file writes are NOT read-only, so
they trigger the workbench's permission-gated approval flow. All analysis tools
are read-only and run freely.

Server name (namespacing): tools appear as ``arxiv__<tool>`` when registered as
"arxiv". Optional deps: pymupdf (PDF text extraction) — everything else uses
stdlib + httpx.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

try:
    from mcp.server import MCPServer
    from mcp.types import ToolAnnotations

    mcp = MCPServer("fox-arxiv-replication", version="0.1.0")
    RO = ToolAnnotations(read_only_hint=True)
except ImportError:  # allow importing the plain functions without the mcp package
    mcp = None
    RO = None

ARXIV_ABS = "https://arxiv.org/abs/{}"
ARXIV_PDF = "https://arxiv.org/pdf/{}.pdf"
ARXIV_API = "http://export.arxiv.org/api/query"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(data) -> str:
    return json.dumps(data, indent=2)


def extract_arxiv_id(text: str) -> str | None:
    """Accept a raw arXiv ID, or an abs/pdf URL."""
    patterns = [
        r"arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5})(v\d+)?",
        r"(\d{4}\.\d{4,5})(v\d+)?",
    ]
    for p in patterns:
        m = re.search(p, text or "")
        if m:
            return m.group(1) + (m.group(2) or "")
    return None


def _parse_atom_metadata(raw: str) -> dict:
    """Parse the arXiv Atom feed (single result) into a metadata dict."""
    ns = {"a": "http://www.w3.org/2005/Atom",
          "ar": "http://arxiv.org/schemas/atom"}
    root = ET.fromstring(raw)
    entry = root.find("a:entry", ns)
    title = (entry.findtext("a:title", namespaces=ns) if entry is not None else None)
    summary = (entry.findtext("a:summary", namespaces=ns) if entry is not None else None)
    authors = []
    if entry is not None:
        for name in entry.findall("a:author/a:name", ns):
            if name.text:
                authors.append(name.text.strip())
    published = entry.findtext("a:published", namespaces=ns) if entry is not None else None
    return {
        "title": (title or "").strip() if title else None,
        "abstract": (summary or "").strip() if summary else None,
        "authors": authors,
        "published": published,
    }


async def ingest_arxiv_paper(arxiv_id_or_url: str, download_pdf: bool = True,
                             work_dir: str = "./papers") -> str:
    """Download metadata (and optionally the PDF) for an arXiv paper.

    Network + file writes -> this tool asks the user for approval.
    """
    import httpx

    arxiv_id = extract_arxiv_id(arxiv_id_or_url)
    if not arxiv_id:
        return _json({"error": f"Could not parse arXiv ID from: {arxiv_id_or_url}"})

    base_id = arxiv_id.split("v")[0]
    work = Path(work_dir) / base_id
    work.mkdir(parents=True, exist_ok=True)

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0),
                                     follow_redirects=True) as client:
            r = await client.get(f"{ARXIV_API}?id_list={base_id}&max_results=1")
            r.raise_for_status()
            raw = r.text
    except Exception as e:  # noqa: BLE001
        return _json({"error": f"Failed to fetch arXiv metadata: {e}"})

    try:
        meta = _parse_atom_metadata(raw)
    except Exception as e:  # noqa: BLE001
        return _json({"error": f"Failed to parse arXiv metadata: {e}"})

    record = {
        "arxiv_id": base_id,
        "versioned_id": arxiv_id,
        "title": meta.get("title"),
        "abstract": meta.get("abstract"),
        "authors": meta.get("authors", []),
        "published": meta.get("published"),
        "abs_url": ARXIV_ABS.format(base_id),
        "pdf_url": ARXIV_PDF.format(base_id),
        "local_dir": str(work),
        "ingested_at": _now(),
    }
    (work / "metadata.json").write_text(json.dumps(record, indent=2))

    pdf_path = None
    if download_pdf:
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=15.0),
                                         follow_redirects=True) as client:
                r = await client.get(record["pdf_url"])
                r.raise_for_status()
                pdf_path = work / f"{base_id}.pdf"
                pdf_path.write_bytes(r.content)
                record["pdf_path"] = str(pdf_path)
        except Exception as e:  # noqa: BLE001
            record["pdf_error"] = str(e)

    return _json(record)


def extract_paper_text(pdf_path: str, max_pages: int = 30) -> str:
    """Extract plain text from a local PDF (PyMuPDF). Writes a .txt copy."""
    try:
        import fitz  # PyMuPDF
    except ImportError:
        return _json({"error": "PyMuPDF not installed. Run: docker compose exec fox "
                               "pip install pymupdf"})

    path = Path(pdf_path)
    if not path.exists():
        return _json({"error": f"PDF not found: {pdf_path}"})

    doc = fitz.open(path)
    n_pages = len(doc)
    pages = []
    for i, page in enumerate(doc):
        if i >= max_pages:
            break
        pages.append(page.get_text())
    doc.close()

    text = "\n\n".join(pages)
    out = path.with_suffix(".txt")
    out.write_text(text)
    return _json({
        "pdf_path": str(path),
        "text_path": str(out),
        "pages_extracted": min(n_pages, max_pages),
        "total_pages": n_pages,
        "char_count": len(text),
        "preview": text[:1500] + ("..." if len(text) > 1500 else ""),
    })


def extract_structured_notes(paper_text: str, focus: str = "methods_and_results") -> str:
    """Return a strict JSON schema the LLM should fill from the paper text."""
    schema = {
        "title": "",
        "one_sentence_summary": "",
        "problem_statement": "",
        "key_contributions": [],
        "methods": {
            "model_architecture": "",
            "training_procedure": "",
            "datasets": [],
            "hyperparameters": {},
            "implementation_details": "",
        },
        "experiments": [{
            "name": "", "description": "", "metrics": [],
            "reported_results": {}, "baselines": [],
        }],
        "main_claims": [],
        "limitations": [],
        "reproducibility_notes": {
            "code_available": None, "data_available": None,
            "seed_reported": None, "hardware": "", "missing_details": [],
        },
        "extracted_at": _now(),
        "focus": focus,
    }
    return _json({
        "schema": schema,
        "guidance": ("Fill the schema strictly from the paper text. Do not invent "
                     "numbers. If a field is missing write null or []. Pay special "
                     "attention to quantitative results and experimental setup."),
        "paper_text_length": len(paper_text or ""),
        "note": "Pass the paper text + this schema to the LLM to obtain filled notes.",
    })


def summarize_paper(title: str, abstract: str, notes_json: str | None = None) -> str:
    """Return the summary structure for the LLM to fill in."""
    return _json({
        "title": title,
        "abstract": abstract,
        "requested_summaries": [
            "one_sentence", "tldr_paragraph", "methods_summary",
            "key_results_bullet_list", "reproducibility_assessment",
        ],
        "notes_provided": notes_json is not None,
        "instruction": ("Using the title, abstract and any structured notes, produce "
                        "the requested summaries. Be faithful; mark uncertainty."),
        "generated_at": _now(),
    })


def craft_experiment_from_notes(structured_notes: str,
                                target_language: str = "python",
                                framework_preference: str = "pytorch") -> str:
    """Turn structured notes into an experiment specification."""
    try:
        notes = json.loads(structured_notes)
    except Exception:  # noqa: BLE001
        return _json({"error": "structured_notes must be valid JSON"})

    methods = notes.get("methods") or {}
    repro = notes.get("reproducibility_notes") or {}
    spec = {
        "experiment_name": (notes.get("title") or "replicated_experiment")[:80],
        "goal": "Reproduce the main quantitative result(s) described in the paper",
        "language": target_language,
        "framework": framework_preference,
        "datasets_required": methods.get("datasets", []),
        "model_description": methods.get("model_architecture", ""),
        "training_outline": methods.get("training_procedure", ""),
        "hyperparameters": methods.get("hyperparameters", {}),
        "metrics_to_compute": [],
        "author_reported_results": {},
        "steps": [],
        "missing_information": repro.get("missing_details", []),
        "crafted_at": _now(),
    }
    experiments = notes.get("experiments") or []
    if experiments:
        exp0 = experiments[0]
        spec["metrics_to_compute"] = exp0.get("metrics", [])
        spec["author_reported_results"] = exp0.get("reported_results", {})
        spec["steps"] = [
            "Prepare / download datasets listed above",
            "Implement or load the model described",
            "Apply the training procedure and hyperparameters",
            "Evaluate on the same metrics the authors used",
            "Record results in a machine-readable form for comparison",
        ]
    return _json({
        "experiment_spec": spec,
        "next_action_suggestion": ("Hand this spec to the code-execution agent to "
                                   "generate a concrete training/evaluation script, "
                                   "then run it under the workbench kernel."),
    })


def compare_results(author_results: str, own_results: str,
                    tolerance: float = 0.05) -> str:
    """Compare reported vs local quantitative results (configurable tolerance)."""
    try:
        authors = json.loads(author_results) if isinstance(author_results, str) else author_results
        own = json.loads(own_results) if isinstance(own_results, str) else own_results
    except Exception as e:  # noqa: BLE001
        return _json({"error": f"Invalid JSON: {e}"})

    comparison = []
    for metric, author_val in (authors or {}).items():
        own_val = own.get(metric)
        entry = {"metric": metric, "author": author_val, "own": own_val,
                 "status": "missing_in_own_results"}
        if (own_val is not None and isinstance(author_val, (int, float))
                and isinstance(own_val, (int, float))):
            rel = (abs(own_val - author_val) / abs(author_val)
                   if author_val != 0 else abs(own_val - author_val))
            entry["relative_difference"] = round(rel, 4)
            entry["status"] = "match" if rel <= tolerance else "discrepancy"
        comparison.append(entry)

    return _json({
        "comparison": comparison,
        "summary": {
            "matches": sum(1 for c in comparison if c["status"] == "match"),
            "discrepancies": sum(1 for c in comparison if c["status"] == "discrepancy"),
            "missing": sum(1 for c in comparison if c["status"] == "missing_in_own_results"),
            "tolerance_used": tolerance,
        },
        "assessed_at": _now(),
    })


def prepare_replication_report(paper_metadata: str, summary: str,
                               structured_notes: str, experiment_spec: str,
                               comparison: str, extra_observations: str = "") -> str:
    """Assemble a provenance-ready replication report."""
    def _ld(x):
        try:
            return json.loads(x) if isinstance(x, str) else x
        except Exception:  # noqa: BLE001
            return None
    return _json({
        "report_type": "arxiv_replication",
        "generated_at": _now(),
        "paper": _ld(paper_metadata),
        "summary": summary,
        "structured_notes": _ld(structured_notes),
        "experiment": _ld(experiment_spec),
        "result_comparison": _ld(comparison),
        "extra_observations": extra_observations,
        "reproducibility_verdict": None,
        "recommendations": [
            "Attach this report as an Artifact linked to the paper and the experiment run",
            "Record exact software versions, seeds, and hardware used",
            "If discrepancies exist, investigate missing details listed in the notes",
        ],
    })


# ============================================================== knowledge graph =

def build_knowledge_graph_from_notes(paper_metadata: str, structured_notes: str,
                                     graph_format: str = "json") -> str:
    """Build a knowledge graph (Paper / Author / Method / Dataset / Metric /
    Experiment / Claim) from paper metadata + structured notes."""
    try:
        meta = json.loads(paper_metadata) if isinstance(paper_metadata, str) else paper_metadata
        notes = json.loads(structured_notes) if isinstance(structured_notes, str) else structured_notes
    except Exception as e:  # noqa: BLE001
        return _json({"error": f"Invalid JSON input: {e}"})

    nodes, edges = [], []
    seen = set()

    def add_node(nid, ntype, **props):
        if nid not in seen:
            nodes.append({"id": nid, "type": ntype, **props})
            seen.add(nid)

    def add_edge(s, rel, t, **props):
        edges.append({"source": s, "relation": rel, "target": t, **props})

    paper_id = f"paper:{meta.get('arxiv_id', 'unknown')}"
    add_node(paper_id, "Paper", title=meta.get("title"), arxiv_id=meta.get("arxiv_id"),
             abs_url=meta.get("abs_url"), ingested_at=meta.get("ingested_at"))

    for author in meta.get("authors") or []:
        aid = f"author:{author.strip().lower().replace(' ', '_')}"
        add_node(aid, "Author", name=author)
        add_edge(aid, "AUTHORED", paper_id)

    methods = notes.get("methods") or {}
    arch = methods.get("model_architecture") or notes.get("one_sentence_summary")
    if arch:
        mid = "method:main_architecture"
        add_node(mid, "Method", description=str(arch)[:300])
        add_edge(paper_id, "PROPOSES", mid)

    for ds in methods.get("datasets") or []:
        did = f"dataset:{str(ds).lower().replace(' ', '_')[:60]}"
        add_node(did, "Dataset", name=str(ds))
        add_edge(paper_id, "USES", did)

    for i, exp in enumerate(notes.get("experiments") or []):
        eid = f"experiment:{i}"
        add_node(eid, "Experiment", name=exp.get("name") or f"experiment_{i}",
                 description=str(exp.get("description", ""))[:300])
        add_edge(paper_id, "CONTAINS", eid)
        for m in exp.get("metrics") or []:
            mid = f"metric:{str(m).lower().replace(' ', '_')}"
            add_node(mid, "Metric", name=str(m))
            add_edge(eid, "MEASURES", mid)
        for metric_name, value in (exp.get("reported_results") or {}).items():
            mid = f"metric:{str(metric_name).lower().replace(' ', '_')}"
            add_node(mid, "Metric", name=str(metric_name))
            add_edge(eid, "REPORTS", mid, value=value)

    for i, claim in enumerate(notes.get("main_claims") or []):
        cid = f"claim:{i}"
        add_node(cid, "Claim", text=str(claim)[:400])
        add_edge(paper_id, "CLAIMS", cid)

    graph = {"paper_id": paper_id, "nodes": nodes, "edges": edges,
             "stats": {"node_count": len(nodes), "edge_count": len(edges),
                       "node_types": sorted({n["type"] for n in nodes})},
             "built_at": _now()}

    if graph_format == "triples":
        return _json({"triples": [{"subject": e["source"], "predicate": e["relation"],
                                   "object": e["target"]} for e in edges],
                      "nodes": nodes, "stats": graph["stats"]})
    if graph_format == "networkx_node_link":
        return _json({"directed": True, "multigraph": False, "graph": {},
                      "nodes": nodes,
                      "links": [{"source": e["source"], "target": e["target"],
                                 "relation": e["relation"]} for e in edges]})
    return _json(graph)


def query_knowledge_graph(graph_json: str, query_type: str = "summary",
                          entity_id: str | None = None) -> str:
    """Query a built knowledge graph: summary / neighbors / metrics / datasets / claims."""
    try:
        g = json.loads(graph_json)
    except Exception as e:  # noqa: BLE001
        return _json({"error": f"Invalid graph JSON: {e}"})

    nodes = {n["id"]: n for n in g.get("nodes", [])}
    edges = g.get("edges", [])

    if query_type == "summary":
        return _json(g.get("stats", {}))
    if query_type == "datasets":
        return _json([n for n in nodes.values() if n["type"] == "Dataset"])
    if query_type == "metrics":
        return _json([n for n in nodes.values() if n["type"] == "Metric"])
    if query_type == "claims":
        return _json([n for n in nodes.values() if n["type"] == "Claim"])
    if query_type == "neighbors" and entity_id:
        related = []
        for e in edges:
            if e["source"] == entity_id:
                related.append({"direction": "out", "relation": e["relation"],
                                "target": e["target"], "node": nodes.get(e["target"])})
            if e["target"] == entity_id:
                related.append({"direction": "in", "relation": e["relation"],
                                "source": e["source"], "node": nodes.get(e["source"])})
        return _json(related)
    return _json({"error": f"Unsupported query_type or missing entity_id: {query_type}"})


def merge_knowledge_graphs(graphs: list[str]) -> str:
    """Merge multiple paper-level knowledge graphs into one corpus graph."""
    all_nodes, all_edges, seen_edges = {}, [], set()
    for g_str in graphs or []:
        try:
            g = json.loads(g_str)
        except Exception:  # noqa: BLE001
            continue
        for n in g.get("nodes", []):
            all_nodes[n["id"]] = n
        for e in g.get("edges", []):
            key = (e["source"], e["relation"], e["target"])
            if key not in seen_edges:
                all_edges.append(e)
                seen_edges.add(key)
    return _json({
        "nodes": list(all_nodes.values()), "edges": all_edges,
        "stats": {"node_count": len(all_nodes), "edge_count": len(all_edges),
                  "papers": sum(1 for n in all_nodes.values() if n["type"] == "Paper")},
        "merged_at": _now(),
    })


def export_knowledge_graph(graph_json: str, format: str = "json",
                           output_path: str | None = None) -> str:
    """Export the graph as JSON / triples / Cypher; optionally write to a file."""
    try:
        g = json.loads(graph_json)
    except Exception as e:  # noqa: BLE001
        return _json({"error": str(e)})

    if format == "triples":
        content = "\n".join(
            f"{e['source']} --{e['relation']}--> {e['target']}"
            + (f"  (value={e['value']})" if "value" in e else "")
            for e in g.get("edges", []))
    elif format == "cypher_snippets":
        lines = []
        for n in g.get("nodes", []):
            props = ", ".join(f"{k}: '{v}'" for k, v in n.items()
                              if k not in ("id", "type") and v is not None)
            lines.append(f"MERGE (n:{n['type']} {{id: '{n['id']}'}}) SET n += {{{props}}};")
        for e in g.get("edges", []):
            lines.append(f"MATCH (a {{id: '{e['source']}'}}), (b {{id: '{e['target']}'}}) "
                         f"MERGE (a)-[:{e['relation']}]->(b);")
        content = "\n".join(lines)
    else:
        content = json.dumps(g, indent=2)

    result = {"format": format, "size_chars": len(content),
              "preview": content[:1000] + ("..." if len(content) > 1000 else "")}
    if output_path:
        Path(output_path).write_text(content)
        result["written_to"] = output_path
    return _json(result)


# ========================================================= resources / prompts =

_HELP = """arXiv Replication Tools
------------------------
arxiv__ingest_arxiv_paper(id_or_url)        - download metadata + PDF
arxiv__extract_paper_text(pdf_path)         - PDF -> text
arxiv__extract_structured_notes(text)       - schema for methods/results extraction
arxiv__summarize_paper(...)                 - multi-level summaries
arxiv__craft_experiment_from_notes(notes)   - notes -> runnable experiment spec
arxiv__compare_results(author, own)         - quantitative comparison
arxiv__prepare_replication_report(...)      - final provenance-linked report
arxiv__build_knowledge_graph_from_notes(...) - Paper -> knowledge graph
arxiv__query_knowledge_graph(...)           - query the graph (summary/neighbors/...)
arxiv__merge_knowledge_graphs([...])        - merge papers into a corpus graph
arxiv__export_knowledge_graph(...)          - export JSON/triples/Cypher
"""

if mcp is not None:
    _READONLY = [
        extract_structured_notes, summarize_paper, craft_experiment_from_notes,
        compare_results, prepare_replication_report,
        build_knowledge_graph_from_notes, query_knowledge_graph,
        merge_knowledge_graphs,
    ]
    for _fn in _READONLY:
        mcp.tool(annotations=RO)(_fn)
    # network / file-write tools -> approval-gated (not read-only)
    for _fn in [ingest_arxiv_paper, extract_paper_text, export_knowledge_graph]:
        mcp.tool()(_fn)

    mcp.resource("arxiv://help", name="arXiv replication help",
                 description="List of arXiv replication + KG tools")(lambda: _HELP)

    mcp.prompt(name="full_replication_workflow",
               title="Replicate an arXiv paper end-to-end",
               description="Ingest, understand, re-implement, verify, report")(
        lambda arxiv_id: (
            "You are a research replication assistant.\n\n"
            f"Target paper: {arxiv_id}\n\n"
            "1. Call ingest_arxiv_paper\n"
            "2. Extract text with extract_paper_text\n"
            "3. Fill structured notes (extract_structured_notes schema)\n"
            "4. Summarize the paper\n"
            "5. Build a knowledge graph (build_knowledge_graph_from_notes)\n"
            "6. Craft an experiment specification\n"
            "7. Run the experiment with the workbench code-execution tools\n"
            "8. Compare results (compare_results)\n"
            "9. Generate the replication report (prepare_replication_report) and "
            "store it + the knowledge graph as Artifacts."))

    __all__ = [f.__name__ for f in (_READONLY + [ingest_arxiv_paper,
                                                 extract_paper_text,
                                                 export_knowledge_graph])] + ["mcp"]
else:
    __all__ = ["ingest_arxiv_paper", "extract_paper_text", "extract_structured_notes",
               "summarize_paper", "craft_experiment_from_notes", "compare_results",
               "prepare_replication_report", "build_knowledge_graph_from_notes",
               "query_knowledge_graph", "merge_knowledge_graphs",
               "export_knowledge_graph"]

if __name__ == "__main__":
    if mcp is None:
        raise SystemExit("mcp package not installed; run: pip install mcp")
    mcp.run(transport="stdio")
