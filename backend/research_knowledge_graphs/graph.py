from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from .hive_datatype import (
    Edge,
    HiveGraph,
    Node,
    NodeType,
)

from .config import Config

logger = logging.getLogger(__name__)


class KnowledgeGraph:
    def __init__(self, config: Config, graph_id: str = "main") -> None:
        self.config = config
        self.graph_id = graph_id
        self.graph_dir = Path(config.graph_dir)
        self.graph_dir.mkdir(parents=True, exist_ok=True)
        self._hive = self._load()

    def _path(self) -> Path:
        return self.graph_dir / f"{self.graph_id}.json"

    def _load(self) -> HiveGraph:
        path = self._path()
        if path.exists():
            try:
                hive = HiveGraph.from_json_file(str(path))
                valid_ids = {n.id for n in hive.nodes}
                before = len(hive.edges)
                hive.edges = [e for e in hive.edges if e.source in valid_ids and e.target in valid_ids]
                if len(hive.edges) < before:
                    logger.warning("Removed %d edges with invalid node refs", before - len(hive.edges))
                return hive
            except Exception as e:
                logger.warning("Failed to load graph, starting fresh: %s", e)
        return HiveGraph(id=self.graph_id)

    def save(self) -> None:
        self._hive.to_json_file(str(self._path()))

    @property
    def hive(self) -> HiveGraph:
        return self._hive

    def add_paper(
        self,
        paper_id: str,
        title: str,
        authors: str = "",
        published: str = "",
        abstract: str = "",
        categories: list[str] | None = None,
        affiliations: str = "",
    ) -> Node:
        existing = self._hive.get_node(paper_id)
        if existing:
            if affiliations and not existing.affiliations:
                existing.affiliations = affiliations
                self.save()
            return existing
        node = Node(
            id=paper_id,
            type=NodeType.PAPER,
            label=title,
            graph_id=self.graph_id,
            arxiv_id=paper_id,
            authors=authors,
            published=published,
            abstract=abstract,
            categories=categories or [],
            affiliations=affiliations,
        )
        self._hive.nodes.append(node)
        return node

    def add_concept(
        self,
        concept_id: str,
        label: str,
        definition: str = "",
        concept_type: str = "concept",
    ) -> Node:
        existing = self._hive.get_node(concept_id)
        if existing:
            return existing
        node = Node(
            id=concept_id,
            type=NodeType.CONCEPT,
            label=label,
            graph_id=self.graph_id,
            definition=definition,
            concept_type=concept_type,
        )
        self._hive.nodes.append(node)
        return node

    def add_edge(
        self,
        source: str,
        target: str,
        relation: str = "related_to",
    ) -> Edge:
        for e in self._hive.edges:
            if e.source == source and e.target == target and e.relation == relation:
                return e
        edge = Edge(source=source, target=target, relation=relation)
        self._hive.edges.append(edge)
        return edge

    def find_similar_concept(
        self,
        label: str,
        threshold: float | None = None,
    ) -> Node | None:
        threshold = threshold if threshold is not None \
            else self.config.graph_similarity_threshold
        label_lower = label.lower()
        label_tokens = set(label_lower.split())
        best_score = 0.0
        best_node: Node | None = None
        for node in self._hive.concepts:
            node_tokens = set(node.label.lower().split())
            if not node_tokens or not label_tokens:
                continue
            intersection = label_tokens & node_tokens
            union = label_tokens | node_tokens
            score = len(intersection) / len(union) if union else 0.0
            if score > best_score:
                best_score = score
                best_node = node
        if best_score >= threshold and best_node is not None:
            return best_node
        return None

    def get_paper(self, paper_id: str) -> Node | None:
        return self._hive.get_node(paper_id)

    def get_concept(self, concept_id: str) -> Node | None:
        return self._hive.get_node(concept_id)

    @property
    def papers(self) -> list[Node]:
        return self._hive.papers

    @property
    def concepts(self) -> list[Node]:
        return self._hive.concepts

    @property
    def edges(self) -> list[Edge]:
        return self._hive.edges

    def stats(self) -> dict[str, int]:
        s = self._hive.stats
        return {
            "papers": s.papers,
            "graph_papers": s.graph_papers,
            "concepts": s.concepts,
            "graph_refs": s.graph_refs,
            "relations": s.relations,
            "cross_edges": s.cross_edges,
        }

    def to_node_link(self) -> dict[str, Any]:
        data = self._hive.to_node_link_dict()
        valid_ids = {n["id"] for n in data.get("nodes", [])}
        data["links"] = [
            l for l in data.get("links", [])
            if l.get("source") in valid_ids and l.get("target") in valid_ids
        ]
        return data

    def detail_graph(self, llm: Any) -> int:
        node_map = {n.id: n.label for n in self._hive.nodes}
        batch: list[dict[str, str]] = []
        edges_to_detail: list[Edge] = []
        for e in self._hive.edges:
            src_label = node_map.get(e.source, e.source)
            tgt_label = node_map.get(e.target, e.target)
            if not e.detail.strip():
                batch.append({
                    "source_id": e.source,
                    "source_label": src_label,
                    "target_label": tgt_label,
                    "relation": e.relation,
                })
                edges_to_detail.append(e)
        if not batch:
            return 0

        system_msg = "You are a research knowledge graph assistant."
        user_prompt = (
            "For each triple below, write ONE short sentence describing the relationship "
            "between the two nodes. Be specific and informative. "
            "Return ONLY a JSON array of strings in the same order:\n"
            + "\n".join(
                f"- {b['source_label']} --[{b['relation']}]--> {b['target_label']}"
                for b in batch
            )
        )
        raw = llm.chat(
            [{"role": "system", "content": system_msg},
             {"role": "user", "content": user_prompt}],
            temperature=0.1,
        )
        content = raw.get("content", "") if isinstance(raw, dict) else str(raw)
        try:
            details = json.loads(content)
            if not isinstance(details, list):
                details = [str(details)]
        except json.JSONDecodeError:
            import re
            matches = re.findall(r'"(.*?)"', content)
            details = matches if matches else [content]
        count = 0
        for e, d in zip(edges_to_detail, details):
            if isinstance(d, str) and d.strip():
                e.detail = d.strip()[:200]
                count += 1
        self.save()
        return count
