"""Experiment 02 — Mid-scale: synthetic single-cell RNA-seq clustering.

Simulates a small single-cell RNA-seq dataset (3 cell types, ~500 cells, 150 genes),
then runs a realistic analysis pipeline: count simulation -> normalization -> PCA ->
KMeans clustering -> t-SNE embedding -> marker heatmap -> cluster summaries.

This is the kind of workflow the workbench is designed for: several steps, an
interactive plot, an evaluation metric against ground truth, and a results table.

    python examples/experiments/02_midscale_cell_clustering.py
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from sklearn.decomposition import PCA
from sklearn.cluster import KMeans
from sklearn.manifold import TSNE
from sklearn.metrics import adjusted_rand_score

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

# ------------------------------------------------------------------ settings --
SEED = 7
N_CELLS = 500
N_GENES = 150
N_TYPES = 3
CELL_TYPES = ["macrophage", "tcell", "epithelial"]
N_MARKERS_PER_TYPE = 8          # marker genes that distinguish each type
N_PCS = 15
PERPLEXITY = 30

# ------------------------------------------------------------- simulation ----
def simulate_counts():
    """Draw UMI-ish counts: NB(lib_size * rate) with per-type marker upregulation."""
    rng = np.random.default_rng(SEED)

    # ground-truth labels, 40/35/25% split
    sizes = np.array([int(N_CELLS * s) for s in (0.40, 0.35, 0.25)])
    sizes[-1] = N_CELLS - sizes[:-1].sum()
    labels = np.repeat(np.arange(N_TYPES), sizes)
    rng.shuffle(labels)

    # baseline expression rates (log-uniform, mostly low)
    base = np.exp(rng.uniform(-5.0, -0.5, size=N_GENES))
    rates = np.repeat(base[None, :], N_CELLS, axis=0).copy()

    # assign marker genes to each type and upregulate
    marker_sets = []
    for t in range(N_TYPES):
        idx = rng.choice(N_GENES, size=N_MARKERS_PER_TYPE, replace=False)
        marker_sets.append(idx)
        cells = labels == t
        rates[np.ix_(cells, idx)] *= rng.uniform(8.0, 20.0, size=idx.size)

    lib_size = rng.uniform(800, 4000, size=N_CELLS).astype(int)
    counts = np.zeros((N_CELLS, N_GENES), dtype=np.int32)
    for i in range(N_CELLS):
        p = rates[i] / rates[i].sum()
        counts[i] = rng.multinomial(lib_size[i], p)
    return counts, labels, marker_sets


def normalize(counts: np.ndarray) -> np.ndarray:
    """Library-size normalize (CPM) and log1p."""
    cpm = counts / counts.sum(axis=1, keepdims=True) * 1e4
    return np.log1p(cpm)


def marker_scores(data: np.ndarray, marker_sets: list) -> pd.DataFrame:
    """Mean expression of each type's markers per cell, as a tidy frame."""
    rows = []
    for t, idx in enumerate(marker_sets):
        rows.append(pd.DataFrame({
            "cell": range(data.shape[0]),
            "cell_type": CELL_TYPES[t],
            "marker_score": data[:, idx].mean(axis=1),
        }))
    return pd.concat(rows, ignore_index=True)


# ------------------------------------------------------------------ analysis --
def run_pipeline(counts: np.ndarray, labels: np.ndarray):
    norm = normalize(counts)

    # PCA
    pca = PCA(n_components=min(N_PCS, counts.shape[1]), random_state=SEED)
    pc = pca.fit_transform(norm)

    # clustering
    km = KMeans(n_clusters=N_TYPES, n_init=10, random_state=SEED)
    cluster = km.fit_predict(pc)

    # embedding
    tsne = TSNE(n_components=2, perplexity=PERPLEXITY, random_state=SEED,
                learning_rate="auto", init="pca")
    emb = tsne.fit_transform(pc)

    return {"norm": norm, "pc": pc, "cluster": cluster, "emb": emb,
            "explained": pca.explained_variance_ratio_, "pca": pca}


# ------------------------------------------------------------------ plotting --
def plot_embedding(emb, cluster, labels, path=None):
    fig, axes = plt.subplots(1, 2, figsize=(10, 4.2))
    sc = axes[0].scatter(emb[:, 0], emb[:, 1], c=cluster, cmap="tab10", s=9, alpha=0.85)
    axes[0].set_title("KMeans clusters (t-SNE)")
    axes[0].set_xlabel("tSNE-1"); axes[0].set_ylabel("tSNE-2")
    axes[1].scatter(emb[:, 0], emb[:, 1], c=labels, cmap="Set1", s=9, alpha=0.85)
    axes[1].set_title("Ground-truth cell types")
    axes[1].set_xlabel("tSNE-1"); axes[1].set_ylabel("tSNE-2")
    fig.tight_layout()
    return fig


def plot_heatmap(norm, marker_sets, path=None):
    # top 12 genes with highest variance as rows; cells ordered by cluster mean
    order = np.argsort(norm.std(axis=0))[::-1][:12]
    # sample up to 80 cells evenly
    n_show = min(norm.shape[0], 80)
    idx = np.linspace(0, norm.shape[0] - 1, n_show).astype(int)
    fig, ax = plt.subplots(figsize=(5.5, 6))
    im = ax.imshow(norm[np.ix_(idx, order)].T, aspect="auto", cmap="viridis",
                   interpolation="nearest")
    ax.set_yticks(range(len(order)))
    ax.set_yticklabels([f"g{int(g)}" for g in order], fontsize=7)
    ax.set_xlabel(f"{n_show} cells (subsample)")
    ax.set_title("Top variable genes")
    fig.colorbar(im, label="log1p(CPM)")
    fig.tight_layout()
    return fig


def plot_cluster_sizes(cluster):
    fig, ax = plt.subplots(figsize=(4.5, 3.2))
    counts = np.bincount(cluster, minlength=N_TYPES)
    ax.bar([f"cluster {i}" for i in range(N_TYPES)], counts, color="#4f8cff")
    ax.set_ylabel("cells")
    ax.set_title("Cluster sizes")
    fig.tight_layout()
    return fig


# --------------------------------------------------------------------- main ---
def main():
    counts, labels, marker_sets = simulate_counts()
    res = run_pipeline(counts, labels)
    cluster = res["cluster"]

    fig1 = plot_embedding(res["emb"], cluster, labels)
    fig2 = plot_heatmap(res["norm"], marker_sets)
    fig3 = plot_cluster_sizes(cluster)
    if __name__ == "__main__":
        fig1.savefig("examples/experiments/02_cell_embedding.png", dpi=150)
        fig2.savefig("examples/experiments/02_marker_heatmap.png", dpi=150)
        fig3.savefig("examples/experiments/02_cluster_sizes.png", dpi=150)

    ari = adjusted_rand_score(labels, cluster)
    explained = res["explained"][:3]

    # cluster summary table (the agent can persist this as a save_artifact)
    summary = pd.DataFrame({
        "cluster": range(N_TYPES),
        "n_cells": np.bincount(cluster, minlength=N_TYPES),
    })
    summary["fraction"] = summary["n_cells"] / summary["n_cells"].sum()

    print("=" * 64)
    print("EXPERIMENT 02 — Synthetic single-cell clustering")
    print("=" * 64)
    print(f"  Cells / genes / types : {N_CELLS} / {N_GENES} / {N_TYPES}")
    print(f"  Explained variance    : PC1={explained[0]*100:.1f}% "
          f"PC2={explained[1]*100:.1f}% PC3={explained[2]*100:.1f}%")
    print(f"  Clustering quality    : Adjusted Rand Index = {ari:.3f}")
    print("  Cluster summary:")
    print(summary.to_string(index=False))
    print("  Top markers per type  :")
    print(marker_scores(res["norm"], marker_sets).groupby("cell_type")
          .mean().round(3).to_string())
    print("=" * 64)


if __name__ == "__main__":
    main()
