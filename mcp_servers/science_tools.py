"""Built-in scientific MCP server for the Fox workbench.

Exposes local, offline scientific tools over the Model Context Protocol so that
any MCP host (Fox itself, Claude, Cursor, VS Code, ...) can use them.

Run it standalone (stdio):

    .venv/bin/python mcp_servers/science_tools.py

It is also registered as the workbench's default MCP server
(Settings -> MCP). Set PYTHONPATH to this repo when running elsewhere.
"""

from __future__ import annotations

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

mcp = MCPServer("fox-science-tools", version="0.1.0")
RO = ToolAnnotations(readOnlyHint=True)


def _aa_mass(seq: str) -> float:
    masses = {"A": 89.09, "R": 174.20, "N": 132.12, "D": 133.10, "C": 121.16,
              "Q": 146.15, "E": 147.13, "G": 75.07, "H": 155.16, "I": 131.17,
              "L": 131.17, "K": 146.19, "M": 149.21, "F": 165.19, "P": 115.13,
              "S": 105.09, "T": 119.12, "W": 204.23, "Y": 181.19, "V": 117.15}
    return sum(masses.get(aa.upper(), 0.0) for aa in seq)


@mcp.tool(annotations=RO)
def sequence_gc_content(sequence: str) -> str:
    """Compute GC content (fraction of G/C bases) of a DNA/RNA sequence."""
    seq = sequence.upper()
    if not seq:
        return "empty sequence"
    gc = seq.count("G") + seq.count("C")
    return f"GC content: {gc / len(seq) * 100:.2f}%  ({gc}/{len(seq)} bases)"


@mcp.tool(annotations=RO)
def peptide_mass(sequence: str) -> str:
    """Estimate the average molecular weight (Da) of an amino-acid peptide."""
    seq = sequence.strip().upper()
    if not seq or any(a not in "ACDEFGHIKLMNPQRSTVWY" for a in seq):
        return "sequence must contain only standard amino-acid one-letter codes"
    mass = _aa_mass(seq) + 18.02  # + water
    return f"Average MW ~ {mass:.1f} Da for a {len(seq)}-residue peptide"


@mcp.tool(annotations=RO)
def protein_hydrophobicity(sequence: str) -> str:
    """Mean Kyte-Doolittle hydrophobicity of an amino-acid sequence."""
    kd = {"I": 4.5, "V": 4.2, "L": 3.8, "F": 2.8, "C": 2.5, "M": 1.9, "A": 1.8,
          "G": -0.4, "T": -0.7, "S": -0.8, "W": -0.9, "Y": -1.3, "P": -1.6,
          "H": -3.2, "D": -3.5, "E": -3.5, "N": -3.5, "Q": -3.5, "K": -3.9, "R": -4.5}
    seq = sequence.strip().upper()
    if not seq:
        return "empty sequence"
    vals = [kd.get(a, 0.0) for a in seq]
    return (f"Mean Kyte-Doolittle: {sum(vals) / len(vals):.2f} "
            f"(>0 hydrophobic, <0 hydrophilic) over {len(seq)} residues")


@mcp.tool(annotations=RO)
def uniprot_lookup(accession: str) -> str:
    """Look up a protein record by UniProt accession (offline mock connector).

    Demonstrates the scientific-database connector pattern. Swap the mock for a
    real HTTP call to https://rest.uniprot.org/uniprotkb/{accession}.json.
    """
    accession = accession.strip()
    mock = {
        "P69905": {"name": "Hemoglobin subunit alpha (HBA1_HUMAN)", "length": 142,
                   "organism": "Homo sapiens", "function": "Oxygen transport"},
        "P0DTC2": {"name": "Spike glycoprotein (SPIKE_SARS2)", "length": 1273,
                   "organism": "Severe acute respiratory syndrome coronavirus 2",
                   "function": "Host receptor binding / membrane fusion"},
        "P04637": {"name": "Cellular tumor antigen p53 (P53_HUMAN)", "length": 393,
                   "organism": "Homo sapiens", "function": "Tumor suppressor"},
    }
    rec = mock.get(accession.upper())
    if rec is None:
        return (f"No cached record for '{accession}'. Available mock accessions: "
                + ", ".join(sorted(mock)))
    return (f"UniProt {accession.upper()}: {rec['name']} ({rec['length']} aa)\n"
            f"Organism: {rec['organism']}\nFunction: {rec['function']}")


@mcp.tool(annotations=RO)
def welch_t_test(group_a: list[float], group_b: list[float]) -> str:
    """Run Welch's t-test on two numeric groups and report t, p and Cohen's d."""
    try:
        import numpy as np
        from scipy import stats
    except ImportError:
        return "numpy/scipy not available in this MCP server process"
    a, b = np.asarray(group_a, dtype=float), np.asarray(group_b, dtype=float)
    if a.size < 2 or b.size < 2:
        return "each group needs at least 2 values"
    t, p = stats.ttest_ind(a, b, equal_var=False)
    pooled = np.sqrt((a.var(ddof=1) + b.var(ddof=1)) / 2)
    d = (b.mean() - a.mean()) / pooled if pooled else 0.0
    return (f"n1={a.size} mean1={a.mean():.3f} | n2={b.size} mean2={b.mean():.3f}\n"
            f"Welch t = {t:.3f}, p = {p:.4g}, Cohen's d = {d:.2f}")


if __name__ == "__main__":
    mcp.run(transport="stdio")
