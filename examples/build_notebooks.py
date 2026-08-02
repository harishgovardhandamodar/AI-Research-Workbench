"""Build the demo experiment notebooks in examples/notebooks/.

Run:  python examples/build_notebooks.py

Each notebook is a valid nbformat-v4 .ipynb with markdown narration + code cells.
They run in the Fox persistent kernel (numpy/scipy/pandas/matplotlib/sklearn) and
can be opened directly from the workbench UI (Notebooks tab) or executed with the
run_notebook agent tool. Scales: tiny -> simple -> mid-scale x2 -> large x2.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.notebooks import new_notebook  # noqa: E402

OUT_DIR = Path(__file__).resolve().parent / "notebooks"
OUT_DIR.mkdir(parents=True, exist_ok=True)

M = "markdown"
C = "code"


def write(name: str, cells: list[tuple[str, str]]) -> None:
    nb = new_notebook(
        [{"cell_type": t, "source": src} for t, src in cells], name)
    (OUT_DIR / f"{name}.ipynb").write_text(json.dumps(nb, indent=1))
    print(f"  wrote {name}.ipynb")


NOTEBOOKS = {
    # ------------------------------------------------------------- TINY -------
    "00_tiny_quick_stats": [
        (M, "# Quick stats — comparing two groups\n\nA tiny, fast experiment: "
            "summary statistics, a Welch t-test and an effect size for two "
            "simulated groups, with one histogram figure."),
        (C, "import numpy as np\n"
            "from scipy import stats\n"
            "import matplotlib.pyplot as plt\n\n"
            "rng = np.random.default_rng(0)\n"
            "group_a = rng.normal(loc=100.0, scale=12.0, size=60)\n"
            "group_b = rng.normal(loc=108.0, scale=14.0, size=55)"),
        (C, "print('group A: mean=%.2f sd=%.2f n=%d' % (group_a.mean(), group_a.std(ddof=1), len(group_a)))\n"
            "print('group B: mean=%.2f sd=%.2f n=%d' % (group_b.mean(), group_b.std(ddof=1), len(group_b)))\n\n"
            "t, p = stats.ttest_ind(group_a, group_b, equal_var=False)\n"
            "pooled = np.sqrt((group_a.var(ddof=1) + group_b.var(ddof=1)) / 2)\n"
            "d = (group_b.mean() - group_a.mean()) / pooled   # Cohen's d\n"
            "print(f'Welch t = {t:.2f}, p = {p:.4g}, Cohen\\'s d = {d:.2f}')"),
        (C, "fig, ax = plt.subplots(figsize=(5.5, 3.2))\n"
            "ax.hist(group_a, bins=14, alpha=0.6, label='group A')\n"
            "ax.hist(group_b, bins=14, alpha=0.6, label='group B')\n"
            "ax.set_xlabel('measurement'); ax.set_ylabel('count')\n"
            "ax.set_title('Two groups'); ax.legend()"),
    ],

    # ----------------------------------------------------------- SIMPLE -------
    "01_simple_decay_fit": [
        (M, "# Exponential decay fit & half-life\n\nSimulate a decay time course, "
            "fit a single exponential, and estimate the half-life with confidence "
            "bounds. One figure: data, fit and residuals."),
        (C, "import numpy as np\n"
            "from scipy.optimize import curve_fit\n"
            "from scipy import stats\n\n"
            "rng = np.random.default_rng(42)\n"
            "t = np.linspace(0, 40, 24)\n"
            "A0, k_true = 100.0, 0.12\n"
            "meas = A0 * np.exp(-k_true * t) + rng.normal(0, 6, size=t.size)\n\n"
            "def decay(t, A0, k): return A0 * np.exp(-k * t)"),
        (C, "popt, pcov = curve_fit(decay, t, meas, p0=(80, 0.1))\n"
            "A0f, kf = popt\n"
            "kerr = np.sqrt(np.diag(pcov))[1]\n"
            "t_half = np.log(2) / kf\n"
            "t_half_err = np.log(2) * kerr / kf**2\n"
            "ci = stats.t.ppf(0.975, df=t.size - 2) * t_half_err\n"
            "print(f'k      = {kf:.4f} ± {kerr:.4f} /h')\n"
            "print(f't1/2   = {t_half:.2f} ± {t_half_err:.2f} h')\n"
            "print(f'95% CI = [{t_half - ci:.2f}, {t_half + ci:.2f}] h')"),
        (C, "tfine = np.linspace(0, 40, 300)\n"
            "fig, (ax, axr) = plt.subplots(2, 1, figsize=(7, 5), sharex=True,\n"
            "                             gridspec_kw={'height_ratios': [3, 1], 'hspace': 0.1})\n"
            "ax.errorbar(t, meas, yerr=6, fmt='o', ms=4, color='#4f8cff', label='data')\n"
            "ax.plot(tfine, decay(tfine, A0f, kf), color='#35c4b6', lw=2, label='fit')\n"
            "ax.axvline(t_half, color='#d9a441', ls='--', label='t1/2')\n"
            "ax.legend(); ax.set_ylabel('activity')\n"
            "res = meas - decay(t, A0f, kf)\n"
            "axr.scatter(t, res, s=12, color='#e05b5b')\n"
            "axr.axhline(0, color='#8b97a5', lw=0.8); axr.set_xlabel('time (h)')"),
    ],

    # ------------------------------------------------------------- MID --------
    "02_midscale_cell_clustering": [
        (M, "# Synthetic single-cell clustering\n\nSimulate a 500-cell RNA-seq "
            "dataset (3 cell types), then run normalize → PCA → KMeans → t-SNE, "
            "check the Adjusted Rand Index and inspect marker genes."),
        (C, "import numpy as np\nimport pandas as pd\n"
            "from sklearn.decomposition import PCA\n"
            "from sklearn.cluster import KMeans\n"
            "from sklearn.manifold import TSNE\n"
            "from sklearn.metrics import adjusted_rand_score\n\n"
            "rng = np.random.default_rng(7)\n"
            "n_cells, n_genes, n_types = 500, 150, 3\n"
            "sizes = [int(n_cells * s) for s in (0.40, 0.35, 0.25)]\n"
            "sizes[-1] = n_cells - sum(sizes[:-1])\n"
            "labels = np.repeat(np.arange(n_types), sizes)\n"
            "rng.shuffle(labels)\n\n"
            "base = np.exp(rng.uniform(-5, -0.5, n_genes))\n"
            "rates = np.repeat(base[None], n_cells, axis=0).copy()\n"
            "markers = []\n"
            "for t in range(n_types):\n"
            "    idx = rng.choice(n_genes, 8, replace=False); markers.append(idx)\n"
            "    rates[np.ix_(labels == t, idx)] *= rng.uniform(8, 20, size=idx.size)\n"
            "counts = np.zeros((n_cells, n_genes), dtype=np.int32)\n"
            "for i in range(n_cells):\n"
            "    counts[i] = rng.multinomial(int(rng.uniform(800, 4000)), rates[i] / rates[i].sum())"),
        (C, "cpm = counts / counts.sum(axis=1, keepdims=True) * 1e4\n"
            "norm = np.log1p(cpm)\n"
            "pc = PCA(n_components=15, random_state=7).fit_transform(norm)\n"
            "cluster = KMeans(n_clusters=3, n_init=10, random_state=7).fit_predict(pc)\n"
            "emb = TSNE(n_components=2, perplexity=30, random_state=7).fit_transform(pc)\n\n"
            "ari = adjusted_rand_score(labels, cluster)\n"
            "print(f'Adjusted Rand Index = {ari:.3f}')"),
        (C, "fig, axes = plt.subplots(1, 2, figsize=(10, 4.2))\n"
            "axes[0].scatter(emb[:, 0], emb[:, 1], c=cluster, cmap='tab10', s=9)\n"
            "axes[0].set_title('KMeans clusters')\n"
            "axes[1].scatter(emb[:, 0], emb[:, 1], c=labels, cmap='Set1', s=9)\n"
            "axes[1].set_title('Ground truth')"),
        (C, "order = np.argsort(norm.std(axis=0))[::-1][:12]\n"
            "idx = np.linspace(0, n_cells - 1, 80).astype(int)\n"
            "fig, ax = plt.subplots(figsize=(5.5, 6))\n"
            "im = ax.imshow(norm[np.ix_(idx, order)].T, aspect='auto', cmap='viridis')\n"
            "ax.set_yticks(range(12)); ax.set_yticklabels([f'g{int(g)}' for g in order], fontsize=7)\n"
            "ax.set_title('Top variable genes'); fig.colorbar(im)"),
    ],

    "04_midscale_epidemiology": [
        (M, "# SIR epidemic model\n\nIntegrate the classic susceptible–infected–"
            "recovered ODEs for different R0 values and compare outbreak size and "
            "peak timing."),
        (C, "import numpy as np\n"
            "from scipy.integrate import solve_ivp\n\n"
            "def sir(t, y, beta, gamma):\n"
            "    s, i, r = y\n"
            "    return [-beta * s * i, beta * s * i - gamma * i, gamma * i]"),
        (C, "def run(R0, N=1_000_000, gamma=0.2, days=200):\n"
            "    beta = R0 * gamma\n"
            "    sol = solve_ivp(sir, (0, days), [0.999, 0.001, 0.0],\n"
            "                    args=(beta, gamma), dense_output=False, max_step=1)\n"
            "    s, i, r = sol.y\n"
            "    peak = int(np.argmax(i))\n"
            "    attack = 1 - s[-1]\n"
            "    return sol.t, i, r, peak, attack\n\n"
            "results = {}\n"
            "for R0 in (1.5, 2.5, 4.0):\n"
            "    results[R0] = run(R0)\n"
            "    t, i, r, peak, attack = results[R0]\n"
            "    print(f'R0={R0}: peak infected {i[peak]/1e3:.1f}k at day {peak}, '\n"
            "          f'attack rate {attack*100:.1f}%')"),
        (C, "import matplotlib.pyplot as plt\n"
            "fig, ax = plt.subplots(figsize=(7, 4))\n"
            "for R0, (t, i, r, peak, attack) in results.items():\n"
            "    ax.plot(t, i / 1e3, label=f'R0 = {R0}')\n"
            "ax.set_xlabel('days'); ax.set_ylabel('infected (thousands)')\n"
            "ax.set_title('SIR outbreak — infected over time'); ax.legend()"),
    ],

    # ------------------------------------------------------------ LARGE -------
    "03_large_protein_pipeline": [
        (M, "# Protein structure analysis pipeline\n\nBuild a mini protein's "
            "backbone from φ/ψ angles using internal-coordinate geometry, write a "
            "PDB file, recover dihedrals, and analyze the structure."),
        (C, "import numpy as np\n\n"
            "B_NCA, B_CAC, B_CN = 1.458, 1.525, 1.329\n"
            "A_NCAC, A_CACN, A_CNCA = 111.0, 116.2, 121.7\n\n"
            "def place(a, b, c, bond, angle, dih):\n"
            "    ba, bc = a - b, c - b\n"
            "    n = np.cross(bc, ba); n /= np.linalg.norm(n)\n"
            "    m = np.cross(n, bc); m /= np.linalg.norm(m)\n"
            "    ar, dr = np.radians(angle), np.radians(dih)\n"
            "    u = bc / np.linalg.norm(bc)\n"
            "    return c + bond * (u*np.cos(ar) + m*np.sin(ar)*np.cos(dr) + n*np.sin(ar)*np.sin(dr))\n\n"
            "def dihedral(p0, p1, p2, p3):\n"
            "    b0 = -(p1 - p0); b1 = p2 - p1; b2 = p3 - p2\n"
            "    b1 /= np.linalg.norm(b1)\n"
            "    v = b0 - np.dot(b0, b1)*b1; w = b2 - np.dot(b2, b1)*b1\n"
            "    return np.degrees(np.arctan2(np.dot(np.cross(b1, v), w), np.dot(v, w)))"),
        (C, "rng = np.random.default_rng(11)\n"
            "seq = 'MKTAYIAKQRQISFVKSHFSRQDILDLWQKAHALEVNEKQLAARLKELGYVESGTLEDVDE'[:60]\n"
            "segs = [(0,20,-57,-47,6.0),(20,28,-65,140,25.0),(28,45,-120,130,12.0),(45,60,-57,-47,6.0)]\n"
            "phis, psis = [], []\n"
            "for s,e,p0,q0,nz in segs:\n"
            "    for _ in range(s,e):\n"
            "        phis.append(p0 + rng.normal(0,nz)); psis.append(q0 + rng.normal(0,nz))\n"
            "phis[0], psis[-1] = -60.0, 130.0\n"
            "n = len(phis)\n"
            "atoms = {'N':[None]*n, 'CA':[None]*n, 'C':[None]*n, 'O':[None]*n}\n"
            "atoms['N'][0] = np.zeros(3); atoms['CA'][0] = np.array([B_NCA, 0, 0])\n"
            "atoms['C'][0] = atoms['CA'][0] + np.array([B_CAC*np.cos(np.radians(69)), B_CAC*np.sin(np.radians(69)), 0])\n"
            "for i in range(n-1):\n"
            "    Ni, CAi, Ci = atoms['N'][i], atoms['CA'][i], atoms['C'][i]\n"
            "    Nn = place(Ni, CAi, Ci, B_CN, A_CACN, psis[i])\n"
            "    CAn = place(CAi, Ci, Nn, B_NCA, A_CNCA, 180.0)\n"
            "    Cn = place(Ci, Nn, CAn, B_CAC, A_NCAC, phis[i+1])\n"
            "    atoms['N'][i+1], atoms['CA'][i+1], atoms['C'][i+1] = Nn, CAn, Cn\n"
            "rphis = [dihedral(atoms['C'][i-1] if i else atoms['CA'][i], atoms['N'][i], atoms['CA'][i], atoms['C'][i]) for i in range(n)]\n"
            "rpsis = [dihedral(atoms['N'][i], atoms['CA'][i], atoms['C'][i], atoms['N'][i+1] if i<n-1 else atoms['CA'][i]) for i in range(n)]"),
        (C, "import matplotlib.pyplot as plt\n"
            "fig, ax = plt.subplots(figsize=(5.2, 4.8))\n"
            "ax.scatter(rphis, rpsis, s=22, c='#4f8cff')\n"
            "ax.axhline(0, color='#8b97a5', lw=0.6, ls=':'); ax.axvline(0, color='#8b97a5', lw=0.6, ls=':')\n"
            "ax.add_patch(plt.Rectangle((-90,-90),60,60,facecolor='#35c4b6',alpha=0.12))\n"
            "ax.add_patch(plt.Rectangle((-170,90),130,90,facecolor='#d9a441',alpha=0.10))\n"
            "ax.set_xlim(-180,180); ax.set_ylim(-180,180)\n"
            "ax.set_xlabel('phi (deg)'); ax.set_ylabel('psi (deg)')\n"
            "ax.set_title('Ramachandran plot')"),
        (C, "from collections import Counter\n"
            "def ss(p,q):\n"
            "    if -90 <= p <= -30 and -90 <= q <= -30: return 'alpha'\n"
            "    if -170 <= p <= -40 and 90 <= q <= 180: return 'beta'\n"
            "    return 'coil'\n"
            "ssc = Counter(ss(p,q) for p,q in zip(rphis,rpsis))\n"
            "comp = Counter(seq)\n"
            "print('secondary structure:', dict(ssc))\n"
            "print('most common residues :', comp.most_common(5))\n"
            "print(f'residues: {n}, alpha={ssc[\"alpha\"]}, beta={ssc[\"beta\"]}, coil={ssc[\"coil\"]}')"),
    ],

    "05_large_model_benchmark": [
        (M, "# Model benchmark with cross-validation\n\nGenerate a synthetic "
            "classification problem, train three models (logistic regression, "
            "random forest, SVM) with 5-fold CV, and compare ROC curves."),
        (C, "import numpy as np\n"
            "from sklearn.datasets import make_classification\n"
            "from sklearn.model_selection import cross_val_score, StratifiedKFold\n"
            "from sklearn.linear_model import LogisticRegression\n"
            "from sklearn.ensemble import RandomForestClassifier\n"
            "from sklearn.svm import SVC\n"
            "from sklearn.metrics import roc_curve, auc\n\n"
            "X, y = make_classification(n_samples=800, n_features=20, n_informative=8,\n"
            "                            n_redundant=4, random_state=3)"),
        (C, "models = {\n"
            "    'LogisticRegression': LogisticRegression(max_iter=2000),\n"
            "    'RandomForest': RandomForestClassifier(n_estimators=100, random_state=3),\n"
            "    'SVM': SVC(probability=True, random_state=3),\n"
            "}\n"
            "cv = StratifiedKFold(5, shuffle=True, random_state=3)\n"
            "for name, m in models.items():\n"
            "    scores = cross_val_score(m, X, y, cv=cv, scoring='accuracy')\n"
            "    print(f'{name:18s} acc = {scores.mean():.3f} ± {scores.std():.3f}')"),
        (C, "import matplotlib.pyplot as plt\n"
            "fig, ax = plt.subplots(figsize=(6, 5))\n"
            "for name, m in models.items():\n"
            "    m.fit(X, y)\n"
            "    fpr, tpr, _ = roc_curve(y, m.predict_proba(X)[:, 1])\n"
            "    ax.plot(fpr, tpr, label=f'{name} (AUC={auc(fpr, tpr):.3f})')\n"
            "ax.plot([0,1],[0,1], ls='--', color='#8b97a5')\n"
            "ax.set_xlabel('false positive rate'); ax.set_ylabel('true positive rate')\n"
            "ax.set_title('ROC curves'); ax.legend()"),
    ],
}


def main() -> None:
    print(f"Building {len(NOTEBOOKS)} demo notebooks -> {OUT_DIR}")
    for name, cells in NOTEBOOKS.items():
        write(name, cells)


if __name__ == "__main__":
    main()
