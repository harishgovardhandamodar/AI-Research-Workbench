"""Experiment 03 — Large: protein structure analysis pipeline.

A full multi-stage bioinformatics pipeline, end to end:

  1. Design a mini protein (2 alpha helices + a beta strand) and BUILD its backbone
     coordinates from phi/psi angles using internal-coordinate (bond length / bond
     angle / dihedral) geometry.
  2. Write a PDB file (a scientific artifact).
  3. Recover phi/psi from the coordinates and draw a Ramachandran plot.
  4. Compute amino-acid composition and a Kyte-Doolittle hydrophobicity profile.
  5. Assign secondary structure from phi/psi and plot a per-residue track.
  6. Emit a markdown report summarizing every number (reproducible / auditable).

    python examples/experiments/03_large_protein_pipeline.py
"""

from __future__ import annotations

import numpy as np

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

# -------------------------------------------------------------- parameters ---
SEED = 11
RESIDUES = "".join([  # ~60-residue mini-domain
    "MKTAYIAKQRQISFVKSHFSRQDILDLWQKAHALEVNEKQLAARLKELGYVESGTLEDVDE",
])[:60]

# (start, end, kind) with canonical backbone angles + noise
SEGMENTS = [
    (0, 20, "alpha",  (-57.0, -47.0), 6.0),   # helix 1
    (20, 28, "coil",  (-65.0, 140.0), 25.0),  # turn / coil
    (28, 45, "beta",  (-120.0, 130.0), 12.0), # beta strand
    (45, 60, "alpha", (-57.0, -47.0), 6.0),   # helix 2
]

# bond geometry (angstroms / degrees)
B_NCA, B_CAC, B_CN = 1.458, 1.525, 1.329
A_NCAC, A_CACN, A_CNCA = 111.0, 116.2, 121.7
B_CO = 1.231
A_CACO = 120.5


# ------------------------------------------------------------- geometry ------
def place(a: np.ndarray, b: np.ndarray, c: np.ndarray,
          bond: float, angle: float, dihedral: float) -> np.ndarray:
    """Place a new atom D after atoms a-b-c given bond length |D-c|, the bond
    angle a-b-c-D at c, and the dihedral a-b-c-D."""
    ba, bc = a - b, c - b
    n = np.cross(bc, ba)
    n /= np.linalg.norm(n)
    n_mbc = np.cross(n, bc)
    n_mbc /= np.linalg.norm(n_mbc)
    ar, dr = np.radians(angle), np.radians(dihedral)
    bc_unit = bc / np.linalg.norm(bc)
    return c + bond * (bc_unit * np.cos(ar)
                       + n_mbc * np.sin(ar) * np.cos(dr)
                       + n * np.sin(ar) * np.sin(dr))


def dihedral(p0, p1, p2, p3) -> float:
    """Signed dihedral (degrees) around the p1-p2 bond."""
    b0, b1 = -1.0 * (p1 - p0), p2 - p1
    b2 = p3 - p2
    b1 /= np.linalg.norm(b1)
    v = b0 - np.dot(b0, b1) * b1
    w = b2 - np.dot(b2, b1) * b1
    x = np.dot(v, w)
    y = np.dot(np.cross(b1, v), w)
    return np.degrees(np.arctan2(y, x))


def build_backbone(phis: list[float], psis: list[float]):
    """Build N/CA/C/O coordinates from per-residue phi/psi (internal coordinates)."""
    n = len(phis)
    atoms = {"N": [None] * n, "CA": [None] * n, "C": [None] * n, "O": [None] * n}

    # seed residue 1
    n1 = np.array([0.0, 0.0, 0.0])
    ca1 = np.array([B_NCA, 0.0, 0.0])
    c1 = ca1 + np.array([B_CAC * np.cos(np.radians(180 - A_NCAC)),
                         B_CAC * np.sin(np.radians(180 - A_NCAC)), 0.0])
    atoms["N"][0], atoms["CA"][0], atoms["C"][0] = n1, ca1, c1

    for i in range(n - 1):
        N_i, CA_i, C_i = atoms["N"][i], atoms["CA"][i], atoms["C"][i]
        # psi_i -> N_{i+1}
        N_ip = place(N_i, CA_i, C_i, B_CN, A_CACN, psis[i])
        # omega = 180 (trans) -> CA_{i+1}
        CA_ip = place(CA_i, C_i, N_ip, B_NCA, A_CNCA, 180.0)
        # phi_{i+1} -> C_{i+1}
        C_ip = place(C_i, N_ip, CA_ip, B_CAC, A_NCAC, phis[i + 1])
        atoms["N"][i + 1], atoms["CA"][i + 1], atoms["C"][i + 1] = N_ip, CA_ip, C_ip

    # carbonyl oxygens (dihedral N-CA-C-O = 180)
    for i in range(n):
        atoms["O"][i] = place(atoms["N"][i], atoms["CA"][i], atoms["C"][i],
                              B_CO, A_CACO, 180.0)
    return atoms


def recover_dihedrals(atoms) -> tuple[list[float], list[float]]:
    n = len(atoms["N"])
    phis, psis = [], []
    for i in range(n):
        phis.append(dihedral(atoms["C"][i - 1] if i > 0 else atoms["CA"][i],
                             atoms["N"][i], atoms["CA"][i], atoms["C"][i]))
        psis.append(dihedral(atoms["N"][i], atoms["CA"][i], atoms["C"][i],
                             atoms["N"][i + 1] if i < n - 1 else atoms["CA"][i]))
    return phis, psis


# ---------------------------------------------------------------- analysis ----
KYTE_DOOLITTLE = {
    "I": 4.5, "V": 4.2, "L": 3.8, "F": 2.8, "C": 2.5, "M": 1.9, "A": 1.8,
    "G": -0.4, "T": -0.7, "S": -0.8, "W": -0.9, "Y": -1.3, "P": -1.6,
    "H": -3.2, "D": -3.5, "E": -3.5, "N": -3.5, "Q": -3.5, "K": -3.9, "R": -4.5,
}


def assign_ss(phi: float, psi: float) -> str:
    if -90 <= phi <= -30 and -90 <= psi <= -30:
        return "alpha"
    if -170 <= phi <= -40 and 90 <= psi <= 180:
        return "beta"
    return "coil"


def hydrophobicity_profile(seq: str, window: int = 9):
    vals = np.array([KYTE_DOOLITTLE.get(aa, 0.0) for aa in seq])
    prof = np.convolve(vals, np.ones(window) / window, mode="same")
    return vals, prof


def write_pdb(atoms, seq: str, path: str) -> None:
    lines = ["HEADER    FOX DEMO PROTEIN STRUCTURE"]
    element = {"N": "N", "CA": "C", "C": "C", "O": "O"}
    for i in range(len(seq)):
        for aname in ("N", "CA", "C", "O"):
            x, y, z = atoms[aname][i]
            lines.append(
                "ATOM  %5d  %-3s %3s %s%4d    %8.3f%8.3f%8.3f  1.00  0.00          %2s"
                % (i * 4 + 1, aname, "ALA" if False else "GLY", "A", i + 1, x, y, z, element[aname]))
    lines.append("END")
    with open(path, "w") as fh:
        fh.write("\n".join(lines) + "\n")


# ---------------------------------------------------------------- plotting ----
def plot_ramachandran(phis, psis, path=None):
    fig, ax = plt.subplots(figsize=(5.2, 4.8))
    ax.scatter(phis, psis, s=22, c="#4f8cff", alpha=0.8, edgecolors="none")
    ax.axhline(0, color="#8b97a5", lw=0.6, ls=":")
    ax.axvline(0, color="#8b97a5", lw=0.6, ls=":")
    # canonical regions
    ax.add_patch(plt.Rectangle((-90, -90), 60, 60, facecolor="#35c4b6", alpha=0.12))
    ax.add_patch(plt.Rectangle((-170, 90), 130, 90, facecolor="#d9a441", alpha=0.10))
    ax.set_xlim(-180, 180); ax.set_ylim(-180, 180)
    ax.set_xlabel("phi (deg)"); ax.set_ylabel("psi (deg)")
    ax.set_title("Ramachandran plot")
    fig.tight_layout()
    return fig


def plot_composition(seq, path=None):
    fig, ax = plt.subplots(figsize=(6.2, 3.2))
    counts = {aa: seq.count(aa) for aa in sorted(set(seq))}
    aas = list(counts)
    ax.bar(aas, [counts[a] for a in aas], color="#4f8cff")
    ax.set_ylabel("count"); ax.set_xlabel("residue")
    ax.set_title(f"Amino-acid composition (n={len(seq)})")
    fig.tight_layout()
    return fig


def plot_hydrophobicity(seq, vals, prof, ss, path=None):
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(7.0, 4.6), sharex=True,
                                   gridspec_kw={"height_ratios": [3, 1], "hspace": 0.1})
    res = np.arange(len(seq))
    ax1.plot(res, vals, lw=1.0, color="#8b97a5", label="raw")
    ax1.plot(res, prof, lw=2.0, color="#35c4b6", label="window-9 mean")
    ax1.axhline(0, color="#8b97a5", lw=0.6, ls=":")
    ax1.set_ylabel("Kyte-Doolittle"); ax1.legend(frameon=False, fontsize=8)
    colors = {"alpha": "#35c4b6", "beta": "#d9a441", "coil": "#8b97a5"}
    ax2.bar(res, np.ones(len(seq)), color=[colors[s] for s in ss], width=1.0)
    ax2.set_yticks([]); ax2.set_xlabel("residue index")
    ax2.set_title("Secondary structure (alpha / beta / coil)", fontsize=9)
    fig.tight_layout()
    return fig


# --------------------------------------------------------------------- main ---
def main():
    rng = np.random.default_rng(SEED)
    phis, psis = [], []
    for start, end, kind, (p0, q0), noise in SEGMENTS:
        for i in range(start, end):
            phis.append(p0 + rng.normal(0, noise))
            psis.append(q0 + rng.normal(0, noise))
    # fix terminal residues to sensible values
    phis[0], psis[-1] = -60.0, 130.0

    atoms = build_backbone(phis, psis)
    rphis, rpsis = recover_dihedrals(atoms)
    ss = [assign_ss(phi, psi) for phi, psi in zip(rphis, rpsis)]
    vals, prof = hydrophobicity_profile(RESIDUES)

    write_pdb(atoms, RESIDUES, "examples/experiments/03_demo_protein.pdb")

    fig1 = plot_ramachandran(rphis, rpsis)
    fig2 = plot_composition(RESIDUES)
    fig3 = plot_hydrophobicity(RESIDUES, vals, prof, ss)
    if __name__ == "__main__":
        fig1.savefig("examples/experiments/03_ramachandran.png", dpi=150)
        fig2.savefig("examples/experiments/03_composition.png", dpi=150)
        fig3.savefig("examples/experiments/03_hydrophobicity.png", dpi=150)

    from collections import Counter
    comp = Counter(RESIDUES)
    ss_count = Counter(ss)
    mean_hydro = float(prof.mean())

    # ---- markdown report (a reproducible summary artifact) ----
    report = f"""# Experiment 03 — Protein structure analysis pipeline

**Sequence** ({len(RESIDUES)} residues): `{RESIDUES}`

## Secondary structure
- alpha: {ss_count['alpha']} ({ss_count['alpha'] / len(ss) * 100:.1f}%)
- beta:  {ss_count['beta']} ({ss_count['beta'] / len(ss) * 100:.1f}%)
- coil:  {ss_count['coil']} ({ss_count['coil'] / len(ss) * 100:.1f}%)

## Amino-acid composition
| Residue | Count | Fraction |
|---------|-------|----------|
""" + "\n".join(
        f"| {aa} | {comp[aa]} | {comp[aa] / len(RESIDUES):.3f} |" for aa in sorted(comp)
    ) + f"""

## Hydrophobicity (Kyte-Doolittle)
- Mean window-9 score: **{mean_hydro:.2f}** (positive = hydrophobic core tendency)
- Hydrophobic residues (>1.8): {sum(1 for aa in RESIDUES if KYTE_DOOLITTLE.get(aa, 0) > 1.8)}

## Ramachandran
- Residues in alpha basin: {sum(1 for p, q in zip(rphis, rpsis) if -90 <= p <= -30 and -90 <= q <= -30)}
- Residues in beta basin:  {sum(1 for p, q in zip(rphis, rpsis) if -170 <= p <= -40 and 90 <= q <= 180)}

*Generated deterministically (seed={SEED}); every figure and this report are auditable artifacts.*
"""
    print(report)

    print("=" * 64)
    print("EXPERIMENT 03 — Protein pipeline complete")
    print("  PDB written to     : examples/experiments/03_demo_protein.pdb")
    print("  Residues / basins  : %d residues, alpha=%d beta=%d coil=%d"
          % (len(ss), ss_count["alpha"], ss_count["beta"], ss_count["coil"]))
    print("  Mean hydrophobicity: %.2f" % mean_hydro)
    print("=" * 64)


if __name__ == "__main__":
    main()
