"""Prompt templates — one per Feynman slash-command, executed via local LLM."""

SYSTEM = """You are Hive Research, a local-only research assistant (Feynman clone).
You run on Ollama / LM Studio, no cloud. Provide source-grounded answers.
Always cite papers with title, authors, year, and URL/DOI when available.
Be concise, transparent about evidence strength, and list limitations."""

DEEP_RESEARCH = """You are doing a source-heavy multi-agent investigation.

Topic: {topic}

You have these papers (ranked, with scores):
{papers_block}

Full-text evidence (top {n} papers, section-aware excerpts):
{fulltext_block}

Task: Synthesize a deep research report with:
1. Executive summary (3-5 bullets)
2. Background & definitions
3. Key findings (with citations)
4. Methods & reproducibility notes
5. Gaps, controversies, and open questions
6. Checklist rubric (evidence quality per claim, 0-3)
7. What to read next (ranked reading list)
8. Limitations of this synthesis

Cite every non-trivial claim as [Author Year] or DOI link. If evidence is thin, say so."""

LIT_REVIEW = """Create a literature review for: {topic}

Papers:
{papers_block}

Write sections: Introduction, Thematic clusters, Methods comparison matrix, Results, Gaps & future work, References.
Use academic tone, ground each section in the papers above. If lab/PI is mentioned, map their trajectory."""

REVIEW = """You are a rigorous peer reviewer. Artifact: {artifact}

Papers/context:
{papers_block}

Produce severity-labeled review:
- Summary
- Major issues (severity: critical/major)
- Minor issues
- Revision plan (checklist)
- Score 0-10 with justification
"""

AUDIT = """Audit paper vs codebase mismatch.

Paper: {paper}
Codebase notes: {notes}

Compare claims vs implementation. List mismatches, missing ablations, irreproducible hyperparams, data-leak risks.
"""

REPLICATE = """Plan replication checks for paper: {paper}
Context: {context}
Papers: {papers_block}

Output: feasibility (0-10), required compute/data, step-by-step replication plan, risk table, verification checklist. Do not execute; plan only."""

RECIPE = """Ranked ML training recipes for: {task}

Papers:
{papers_block}

Output ranked recipes with: dataset, method, code repo, hyperparameters, verification status (reproduced/unverified), compute estimate, pros/cons.
"""

COMPARE = """Source comparison matrix for: {topic}

Papers:
{papers_block}

Build a table: rows = papers, cols = problem | method | dataset | metrics | results | limitations | reproducibility. Then synthesize contrastive takeaways."""

DRAFT = """Draft a paper-style manuscript from findings.

Topic: {topic}
Evidence:
{papers_block}
Prior synthesis: {prior}

Structure: Title, Abstract, Introduction, Related Work, Methods, Experiments, Results, Discussion, Limitations, References.
Use placeholder \\cite where needed but ground in provided papers."""

AUTORESEARCH = """Bounded experiment loop idea: {idea}
Papers: {papers_block}
Simulate a benchmark-evidence loop: propose hypothesis, experiment, expected metric, falsification criteria. Keep it executable locally (Python, small data). Provide code skeleton."""

WATCH = """Research watch baseline for: {topic}
Papers snapshot:
{papers_block}

Summarize current state, watch queries to schedule, and baseline metrics to track (citation velocity, new preprints per month)."""

RANK_RESCORING = """Rescore papers with section-aware evidence.

Topic: {topic}
Papers with initial scores:
{papers_block}
Full-text evidence:
{fulltext_block}

Rescore 0-100 with rubric: citations (0-20), methods transparency (0-20), reproducibility (0-20), provenance (0-20), query fit (0-20).
Return JSON list with id, new_score, breakdown, evidence quote.
"""
