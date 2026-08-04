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

    # ------------------------------------------------------------- MORE -------
    "06_tiny_clt_demo": [
        (M, "# Central Limit Theorem demo\n\nDraw sample means from an exponential "
            "population and watch them become normal; measure how the standard "
            "error shrinks with sample size."),
        (C, "import numpy as np\n"
            "from scipy import stats\n"
            "import matplotlib.pyplot as plt\n"
            "rng = np.random.default_rng(1)\n"
            "means = [rng.exponential(1.0, 30).mean() for _ in range(5000)]\n\n"
            "fig, ax = plt.subplots(figsize=(5.5, 3.5))\n"
            "ax.hist(means, bins=40, density=True, alpha=0.6, label='sample means')\n"
            "xs = np.linspace(0.3, 1.7, 200)\n"
            "ax.plot(xs, stats.norm.pdf(xs, 1.0, 1/np.sqrt(30)), color='#e05b5b', label='N(1, 1/sqrt(30))')\n"
            "ax.set_xlabel('mean'); ax.set_ylabel('density'); ax.legend()\n"
            "print(f'mean of means = {np.mean(means):.3f}, sd = {np.std(means):.3f} (theory 0.183)')"),
        (C, "sizes = np.array([2, 5, 10, 20, 50, 100])\n"
            "stds = [np.std([rng.exponential(1.0, s).mean() for _ in range(2000)]) for s in sizes]\n\n"
            "fig, ax = plt.subplots(figsize=(5.5, 3.5))\n"
            "ax.plot(sizes, stds, 'o-', color='#4f8cff', label='empirical')\n"
            "ax.plot(sizes, 1/np.sqrt(sizes), '--', color='#e05b5b', label='1/sqrt(n)')\n"
            "ax.set_xlabel('sample size'); ax.set_ylabel('std of the mean')\n"
            "ax.legend(); ax.set_title('Convergence of the standard error')"),
    ],

    "07_simple_heat_diffusion": [
        (M, "# 1D heat diffusion\n\nSolve the heat equation with an explicit "
            "finite-difference scheme: an initially hot spot spreading over time."),
        (C, "import numpy as np\nimport matplotlib.pyplot as plt\n"
            "alpha, L, nx = 0.01, 1.0, 50\n"
            "dx = L / (nx - 1)\n"
            "dt = 0.0002                    # dt <= dx^2 / (2 alpha) for stability\n"
            "steps = 600\n"
            "u = np.zeros(nx); u[nx//2 - 3:nx//2 + 3] = 1.0   # hot spot\n"
            "history = [u.copy()]\n"
            "c = alpha * dt / dx**2\n"
            "for _ in range(steps):\n"
            "    u = u + c * (np.roll(u, -1) + np.roll(u, 1) - 2*u)\n"
            "    u[0] = u[-1] = 0\n"
            "    if _ % 120 == 0: history.append(u.copy())"),
        (C, "x = np.linspace(0, L, nx)\n"
            "fig, ax = plt.subplots(figsize=(6, 4))\n"
            "for i, prof in enumerate(history):\n"
            "    ax.plot(x, prof, label=f't = {i*steps//len(history)*dt:.3f}')\n"
            "ax.set_xlabel('x'); ax.set_ylabel('temperature'); ax.legend()\n"
            "ax.set_title('Temperature profiles')"),
        (C, "fig, ax = plt.subplots(figsize=(6, 4))\n"
            "im = ax.imshow(np.array(history), aspect='auto', origin='lower',\n"
            "               extent=[0, L, 0, steps*dt], cmap='inferno')\n"
            "ax.set_xlabel('x'); ax.set_ylabel('time');\n"
            "fig.colorbar(im, label='temperature')\n"
            "ax.set_title('Spacetime heat map')"),
    ],

    "08_simple_logistic_growth": [
        (M, "# Logistic population growth\n\nCompare growth curves for different "
            "intrinsic rates and inspect the growth-vs-density relationship."),
        (C, "import numpy as np\nfrom scipy.integrate import solve_ivp\n"
            "import matplotlib.pyplot as plt\n\n"
            "def logistic(t, N, r, K): return r * N * (1 - N / K)\n"
            "K = 1000.0\n"
            "sol = solve_ivp(logistic, (0, 40), [10.0], args=(0.3, K), max_step=0.1)\n"
            "sol_fast = solve_ivp(logistic, (0, 40), [10.0], args=(0.8, K), max_step=0.1)"),
        (C, "fig, ax = plt.subplots(figsize=(6, 4))\n"
            "ax.plot(sol.t, sol.y[0], label='r = 0.3')\n"
            "ax.plot(sol_fast.t, sol_fast.y[0], label='r = 0.8')\n"
            "ax.axhline(K, color='#8b97a5', ls='--', label='K')\n"
            "ax.set_xlabel('time'); ax.set_ylabel('N'); ax.legend()\n"
            "ax.set_title('Logistic growth')"),
        (C, "N = np.linspace(0, 1.2*K, 200)\n"
            "fig, ax = plt.subplots(figsize=(6, 4))\n"
            "ax.plot(N, 0.5 * N * (1 - N / K), color='#4f8cff')\n"
            "ax.axhline(0, color='#8b97a5', lw=0.8)\n"
            "ax.set_xlabel('N'); ax.set_ylabel('dN/dt')\n"
            "ax.set_title('Per-capita growth rate'); ax.set_xlim(0, 1.2*K)"),
    ],

    "09_midscale_regression_diagnostics": [
        (M, "# Linear regression diagnostics\n\nFit a two-predictor OLS model on "
            "synthetic data and inspect fit quality, residuals and influence."),
        (C, "import numpy as np\nimport matplotlib.pyplot as plt\n"
            "rng = np.random.default_rng(5)\n"
            "n = 120\n"
            "x1 = rng.normal(50, 10, n)\n"
            "x2 = rng.uniform(0, 5, n)\n"
            "y = 2.0 + 0.4 * x1 - 3.2 * x2 + rng.normal(0, 6, n)\n"
            "X = np.column_stack([np.ones(n), x1, x2])\n"
            "beta, *_ = np.linalg.lstsq(X, y, rcond=None)\n"
            "yhat = X @ beta\n"
            "res = y - yhat\n"
            "sigma2 = res @ res / (n - 3)\n"
            "cov = sigma2 * np.linalg.inv(X.T @ X)\n"
            "se = np.sqrt(np.diag(cov))\n"
            "for i, name in enumerate(['intercept', 'x1', 'x2']):\n"
            "    print(f'{name:9s} beta={beta[i]:+.3f} ± {se[i]:.3f}')"),
        (C, "fig, axes = plt.subplots(1, 2, figsize=(10, 4))\n"
            "lo, hi = y.min(), y.max()\n"
            "axes[0].scatter(yhat, y, s=14, alpha=0.6)\n"
            "axes[0].plot([lo, hi], [lo, hi], color='#e05b5b', ls='--')\n"
            "axes[0].set_xlabel('fitted'); axes[0].set_ylabel('observed')\n"
            "axes[0].set_title('Observed vs fitted')\n"
            "axes[1].scatter(yhat, res, s=14, alpha=0.6)\n"
            "axes[1].axhline(0, color='#e05b5b', ls='--')\n"
            "axes[1].set_xlabel('fitted'); axes[1].set_ylabel('residual')\n"
            "axes[1].set_title('Residuals')"),
        (C, "H = X @ np.linalg.inv(X.T @ X) @ X.T\n"
            "lev = np.diag(H)\n"
            "cooks = res**2 / (3 * sigma2) * lev / (1 - lev)**2\n\n"
            "fig, axes = plt.subplots(1, 2, figsize=(10, 4))\n"
            "axes[0].bar(range(n), lev, color='#4f8cff')\n"
            "axes[0].axhline(2 * 3 / n, color='#e05b5b', ls='--', label='2p/n')\n"
            "axes[0].set_title('Leverage'); axes[0].legend()\n"
            "axes[1].bar(range(n), cooks, color='#d9a441')\n"
            "axes[1].set_title(\"Cook's distance\")\n"
            "print(f'high-leverage points: {int((lev > 2*3/n).sum())}')"),
    ],

    "10_midscale_ar_forecast": [
        (M, "# AR(2) time series & forecasting\n\nSimulate an autoregressive "
            "series, inspect its autocorrelation structure and forecast ahead."),
        (C, "import numpy as np\nimport matplotlib.pyplot as plt\n"
            "rng = np.random.default_rng(9)\n"
            "n = 300; phi1, phi2 = 0.6, -0.25\n"
            "x = np.zeros(n)\n"
            "for t in range(2, n):\n"
            "    x[t] = phi1*x[t-1] + phi2*x[t-2] + rng.normal(0, 1)\n"
            "print(f'variance = {x.var():.3f} (theory ~1.4)')"),
        (C, "def acf(x, lag):\n"
            "    x = x - x.mean()\n"
            "    c0 = x @ x\n"
            "    return np.array([(x[:-k] @ x[k:]) / c0 for k in range(1, lag+1)])\n"
            "ac = acf(x, 20)\n"
            "fig, axes = plt.subplots(1, 2, figsize=(10, 3.5))\n"
            "axes[0].stem(range(1, 21), ac)\n"
            "axes[0].axhline(1.96/np.sqrt(n), color='#e05b5b', ls=':'); axes[0].axhline(-1.96/np.sqrt(n), color='#e05b5b', ls=':')\n"
            "axes[0].set_title('ACF')\n"
            "axes[1].plot(x); axes[1].set_title('Series')\n"
            "axes[1].set_xlabel('t')"),
        (C, "p = 10\n"
            "Xm = np.column_stack([x[p-1-i : n-1-i] for i in range(p)])\n"
            "pac = np.linalg.lstsq(Xm, x[p:], rcond=None)[0]\n\n"
            "fig, ax = plt.subplots(figsize=(6, 3.5))\n"
            "ax.stem(range(1, p+1), pac)\n"
            "ax.set_title('PACF (regression-based)'); ax.set_xlabel('lag')\n\n"
            "# 1-step-ahead recursive forecast\n"
            "steps = 30\n"
            "f = list(x[-2:])\n"
            "for _ in range(steps):\n"
            "    f.append(phi1*f[-1] + phi2*f[-2])\n"
            "fig2, ax2 = plt.subplots(figsize=(7, 3.5))\n"
            "ax2.plot(range(n), x, color='#4f8cff', label='observed')\n"
            "ax2.plot(range(n, n+steps), f[2:], color='#e05b5b', ls='--', label='forecast')\n"
            "ax2.set_xlabel('t'); ax2.legend(); ax2.set_title('AR(2) forecast')"),
    ],

    "11_midscale_volcano_ma": [
        (M, "# Differential expression: volcano & MA plots\n\nSimulate two groups "
            "of samples across many genes, run a per-gene t-test and visualize the "
            "log-fold-change vs significance."),
        (C, "import numpy as np\nfrom scipy import stats\n"
            "import matplotlib.pyplot as plt\n"
            "rng = np.random.default_rng(13)\n"
            "n_genes, n_samples = 4000, 12\n"
            "base = np.exp(rng.normal(0, 1.2, n_genes))[:, None]\n"
            "ctrl = rng.gamma(shape=base*5, scale=1/5, size=(n_genes, n_samples//2))\n"
            "treat = rng.gamma(shape=base*5, scale=1/5, size=(n_genes, n_samples//2))\n"
            "de = rng.choice(n_genes, 300, replace=False)\n"
            "treat[de] *= rng.uniform(2, 6, size=(len(de), 1))"),
        (C, "logfc = np.log2((treat.mean(axis=1) + 1e-6) / (ctrl.mean(axis=1) + 1e-6))\n"
            "pvals = [stats.ttest_ind(ctrl[g], treat[g], equal_var=False).pvalue for g in range(n_genes)]\n"
            "pvals = np.array(pvals)\n\n"
            "fig, axes = plt.subplots(1, 2, figsize=(10, 4.2))\n"
            "sig = pvals < 0.05/n_genes\n"
            "axes[0].scatter(logfc, -np.log10(pvals + 1e-12), s=3, alpha=0.5)\n"
            "axes[0].scatter(logfc[sig], -np.log10(pvals[sig] + 1e-12), s=5, color='#e05b5b')\n"
            "axes[0].axhline(-np.log10(0.05/n_genes), color='#8b97a5', ls='--')\n"
            "axes[0].set_xlabel('log2 fold change'); axes[0].set_ylabel('-log10 p')\n"
            "axes[0].set_title(f'Volcano — {sig.sum()} DE genes (BH)')\n"
            "meanexpr = np.log2((treat.mean(axis=1) + ctrl.mean(axis=1)) / 2 + 1e-6)\n"
            "axes[1].scatter(meanexpr, logfc, s=3, alpha=0.5)\n"
            "axes[1].axhline(0, color='#8b97a5', lw=0.8)\n"
            "axes[1].set_xlabel('mean expression (log2)'); axes[1].set_ylabel('log2 fold change')\n"
            "axes[1].set_title('MA plot')"),
    ],

    "12_midscale_monte_carlo_pi": [
        (M, "# Monte Carlo estimation of pi\n\nThrow random points into the unit "
            "square and see the estimate converge as the number of samples grows."),
        (C, "import numpy as np\nimport matplotlib.pyplot as plt\n"
            "rng = np.random.default_rng(2)\n"
            "n = 4000\n"
            "pts = rng.uniform(0, 1, (n, 2))\n"
            "inside = (pts[:, 0]**2 + pts[:, 1]**2) <= 1\n"
            "fig, ax = plt.subplots(figsize=(5, 5))\n"
            "ax.scatter(pts[inside, 0], pts[inside, 1], s=3, color='#4f8cff')\n"
            "ax.scatter(pts[~inside, 0], pts[~inside, 1], s=3, color='#e05b5b')\n"
            "ax.set_aspect('equal'); ax.set_title(f'n = {n}, pi ~ {4*inside.mean():.4f}')\n"
            "print(f'estimate after {n} samples: {4*inside.mean():.4f}')"),
        (C, "cum = 4 * np.cumsum(inside) / np.arange(1, n+1)\n"
            "fig, ax = plt.subplots(figsize=(6, 3.5))\n"
            "ax.plot(cum, color='#35c4b6', lw=1)\n"
            "ax.axhline(np.pi, color='#e05b5b', ls='--', label='pi')\n"
            "ax.set_xlabel('samples'); ax.set_ylabel('estimate')\n"
            "ax.legend(); ax.set_title('Convergence of the estimate')"),
    ],

    "13_midscale_lotka_volterra": [
        (M, "# Lotka–Volterra predator–prey\n\nIntegrate the classic coupled ODEs "
            "and look at cycles in both time and phase space."),
        (C, "import numpy as np\nfrom scipy.integrate import solve_ivp\n"
            "import matplotlib.pyplot as plt\n\n"
            "def lv(t, y, a, b, c, d):\n"
            "    x, z = y\n"
            "    return [a*x - b*x*z, c*x*z - d*z]\n"
            "a, b, c, d = 1.1, 0.4, 0.1, 0.4\n"
            "sol = solve_ivp(lv, (0, 60), [10, 5], args=(a, b, c, d), max_step=0.05)"),
        (C, "fig, ax = plt.subplots(figsize=(6, 4))\n"
            "ax.plot(sol.t, sol.y[0], label='prey')\n"
            "ax.plot(sol.t, sol.y[1], label='predator')\n"
            "ax.set_xlabel('time'); ax.legend(); ax.set_title('Populations over time')"),
        (C, "fig, ax = plt.subplots(figsize=(5.5, 5))\n"
            "for ic in ([10, 5], [15, 8], [6, 12]):\n"
            "    s = solve_ivp(lv, (0, 60), ic, args=(a, b, c, d), max_step=0.05)\n"
            "    ax.plot(s.y[0], s.y[1], label=f'start {ic}')\n"
            "ax.set_xlabel('prey'); ax.set_ylabel('predator')\n"
            "ax.legend(); ax.set_title('Phase plane')"),
    ],

    "14_midscale_hierarchical_clustering": [
        (M, "# Hierarchical clustering of expression profiles\n\nBuild synthetic "
            "samples across features, cluster them hierarchically and inspect the "
            "resulting tree + heatmap."),
        (C, "import numpy as np\nimport matplotlib.pyplot as plt\n"
            "from scipy.cluster.hierarchy import linkage, dendrogram\n"
            "from scipy.spatial.distance import pdist\n"
            "rng = np.random.default_rng(17)\n"
            "n_samples, n_feat = 40, 25\n"
            "types = np.repeat([0, 1, 2], [15, 12, 13])\n"
            "X = rng.normal(0, 1, (n_samples, n_feat))\n"
            "for t in range(3):\n"
            "    X[types == t] += rng.uniform(0.8, 2.0, n_feat)"),
        (C, "Z = linkage(pdist(X), method='ward')\n"
            "fig, ax = plt.subplots(figsize=(7, 3.5))\n"
            "dendrogram(Z, ax=ax, color_threshold=8, no_labels=True)\n"
            "ax.set_title('Ward dendrogram')"),
        (C, "order = dendrogram(Z, no_plot=True)['leaves']\n"
            "fig, ax = plt.subplots(figsize=(4.5, 6))\n"
            "im = ax.imshow(X[order].T, aspect='auto', cmap='coolwarm', vmin=-2, vmax=3)\n"
            "fig.colorbar(im, label='expression')\n"
            "ax.set_xlabel('sample'); ax.set_ylabel('feature')\n"
            "ax.set_title('Heatmap (dendrogram order)')"),
    ],

    "15_large_metabolomics_pipeline": [
        (M, "# Metabolomics pipeline\n\nSimulate a metabolomics matrix (3 groups, "
            "many features), run PCA, a top-variable heatmap and a fold-change / "
            "significance volcano with multiple-testing control."),
        (C, "import numpy as np\nfrom scipy import stats\n"
            "from sklearn.decomposition import PCA\n"
            "import matplotlib.pyplot as plt\n"
            "rng = np.random.default_rng(21)\n"
            "n_feat, n_per = 300, 15\n"
            "groups = np.repeat([0, 1, 2], n_per)\n"
            "base = np.exp(rng.normal(0, 1, n_feat))[:, None]\n"
            "X = rng.gamma(shape=base*3, scale=1/3, size=(n_feat, n_per*3))\n"
            "for g in (1, 2):\n"
            "    idx = rng.choice(n_feat, 60, replace=False)\n"
            "    X[np.ix_(idx, np.where(groups == g)[0])] *= rng.uniform(2, 5, size=(len(idx), 1))"),
        (C, "logX = np.log1p(X / X.mean(axis=1, keepdims=True) * 1e3)\n"
            "pc = PCA(n_components=3, random_state=0).fit_transform(logX.T)\n"
            "fig, ax = plt.subplots(figsize=(6, 5))\n"
            "for g in range(3):\n"
            "    ax.scatter(pc[groups == g, 0], pc[groups == g, 1], label=f'group {g}', s=25)\n"
            "ax.set_xlabel('PC1'); ax.set_ylabel('PC2'); ax.legend()\n"
            "ax.set_title('PCA score plot')"),
        (C, "order = np.argsort(logX.std(axis=1))[::-1][:30]\n"
            "fig, ax = plt.subplots(figsize=(6, 7))\n"
            "im = ax.imshow(logX[order], aspect='auto', cmap='viridis')\n"
            "fig.colorbar(im, label='log intensity')\n"
            "ax.set_ylabel('feature'); ax.set_xlabel('sample')\n"
            "ax.set_title('Top-variable features')"),
        (C, "def bh(p):\n"
            "    p = np.asarray(p); order = np.argsort(p)\n"
            "    adj = np.empty_like(p); adj[order] = p[order]*len(p)/np.arange(1, len(p)+1)\n"
            "    return np.minimum.accumulate(adj[::-1])[::-1]\n"
            "ctrl = X[:, groups == 0]; trt = X[:, groups == 1]\n"
            "logfc = np.log2((trt.mean(axis=1)+1e-6)/(ctrl.mean(axis=1)+1e-6))\n"
            "pv = np.array([stats.ttest_ind(ctrl[g], trt[g], equal_var=False).pvalue for g in range(n_feat)])\n"
            "q = bh(pv)\n"
            "sig = q < 0.05\n"
            "fig, ax = plt.subplots(figsize=(6, 4.5))\n"
            "ax.scatter(logfc, -np.log10(q+1e-12), s=4, alpha=0.6)\n"
            "ax.scatter(logfc[sig], -np.log10(q[sig]+1e-12), s=8, color='#e05b5b')\n"
            "ax.set_xlabel('log2 FC (group1 vs 0)'); ax.set_ylabel('-log10 q')\n"
            "ax.set_title(f'Volcano — {sig.sum()} features q<0.05')"),
    ],

    "16_large_double_pendulum": [
        (M, "# Double pendulum\n\nIntegrate the chaotic double pendulum and check "
            "energy conservation while watching the trajectory."),
        (C, "import numpy as np\nfrom scipy.integrate import solve_ivp\n"
            "import matplotlib.pyplot as plt\n"
            "g, m, L = 9.81, 1.0, 1.0\n\n"
            "def dp(t, y):\n"
            "    t1, w1, t2, w2 = y\n"
            "    d = t1 - t2\n"
            "    a = (m*L*L); b = m*L*L*np.cos(d)\n"
            "    c = m*L*L; f = m*L*L*np.cos(d)\n"
            "    e = -m*g*L*2*np.sin(t1) - m*g*L*np.sin(t2)\n"
            "    g0 = m*L*L*(w1**2*np.sin(d) + w2**2*np.sin(d)) - m*g*L*np.sin(t2)\n"
            "    return [w1, w2,\n"
            "            (e*c - b*g0) / (a*c - b*b),\n"
            "            (a*g0 - e*f) / (a*c - b*b)]\n"
            "sol = solve_ivp(dp, (0, 30), [np.pi/2, 0, np.pi/2, 0], max_step=0.02)"),
        (C, "t1, w1, t2, w2 = sol.y\n"
            "x2 = L*np.sin(t1) + L*np.sin(t2)\n"
            "y2 = -L*np.cos(t1) - L*np.cos(t2)\n"
            "fig, ax = plt.subplots(figsize=(6, 6))\n"
            "ax.plot(x2, y2, lw=0.8, color='#4f8cff')\n"
            "ax.set_aspect('equal'); ax.set_title('Trajectory of the second bob')"),
        (C, "energy = (0.5*(w1**2 + 2*w1*w2*np.cos(t1-t2) + w2**2)\n"
            "         - g/L*(2*np.cos(t1) + np.cos(t2)))   # per unit m, L\n"
            "fig, axes = plt.subplots(1, 2, figsize=(10, 3.8))\n"
            "axes[0].plot(sol.t, energy, color='#35c4b6')\n"
            "axes[0].set_xlabel('t'); axes[0].set_ylabel('E / mgL')\n"
            "axes[0].set_title(f'Energy drift {100*abs(energy.max()-energy.min())/abs(energy.mean()):.3f}%')\n"
            "axes[1].scatter(t1, t2, s=1, color='#d9a441')\n"
            "axes[1].set_xlabel('theta1'); axes[1].set_ylabel('theta2')\n"
            "axes[1].set_title('theta1 vs theta2')"),
    ],

    "17_large_image_convolution": [
        (M, "# Image filtering: blur & edge detection\n\nBuild a synthetic image, "
            "apply a Gaussian blur and Sobel edge detection with scipy.ndimage."),
        (C, "import numpy as np\nimport matplotlib.pyplot as plt\n"
            "from scipy import ndimage\n"
            "rng = np.random.default_rng(4)\n"
            "img = rng.normal(0, 0.1, (128, 128))\n"
            "img[30:50, 40:80] += 1.5      # bright rectangle\n"
            "img[70:90, 30:60] -= 1.0      # dark block\n"
            "yy, xx = np.ogrid[:128, :128]\n"
            "img += 0.8 * np.exp(-((xx-100)**2 + (yy-20)**2) / 200)\n"
            "img += rng.normal(0, 0.15, img.shape)"),
        (C, "blur = ndimage.gaussian_filter(img, sigma=2.0)\n"
            "fig, axes = plt.subplots(1, 3, figsize=(12, 4))\n"
            "for ax, im, t in zip(axes, [img, blur, img - blur],\n"
            "                      ['original', 'Gaussian blur', 'difference']):\n"
            "    ax.imshow(im, cmap='gray'); ax.set_title(t); ax.axis('off')"),
        (C, "gx = ndimage.sobel(blur, axis=0); gy = ndimage.sobel(blur, axis=1)\n"
            "edges = np.hypot(gx, gy)\n"
            "fig, axes = plt.subplots(1, 3, figsize=(12, 4))\n"
            "for ax, im, t in zip(axes, [edges, edges > 0.4, edges > 0.8],\n"
            "                      ['Sobel magnitude', 'threshold 0.4', 'threshold 0.8']):\n"
            "    ax.imshow(im, cmap='gray'); ax.set_title(t); ax.axis('off')"),
    ],

    # ------------------------------------------------ OBFUSCATION ------------
    "18_obfuscation_techniques": [
        (M, "# Data obfuscation techniques on credit-card transaction data\n\n"
            "Generate synthetic credit-card records, then apply each "
            "obfuscation technique from the obfuscation study: field-level "
            "masking, tokenization, fuzzy range blurring, noisy aggregation, "
            "metadata sanitization and k-anonymity. Every figure becomes a "
            "workbench artifact."),
        (C, "import sys\nfrom pathlib import Path\n"
            "sys.path.insert(0, str(Path.cwd()))   # repo root (kernel cwd)\n\n"
            "from examples.obfuscation.credit_card_data import generate_credit_card\n"
            "from examples.obfuscation import obfuscate as obf\n\n"
            "df = generate_credit_card(2000, seed=42)\n"
            "print(df.shape)\n"
            "df[['card_number', 'card_bin', 'cardholder_name',\n"
            "    'cardholder_city', 'transaction_amount_usd']].head(3)"),
        (C, "masked = obf.apply_masking(df, mask=['card_number', 'cardholder_name',\n"
            "                                        'merchant_name', 'cardholder_city'])\n"
            "masked[['card_number', 'cardholder_name',\n"
            "        'merchant_name', 'cardholder_city']].head(3)"),
        (C, "tok = obf.tokenize(df, columns=['card_number', 'merchant_account', 'card_bin'])\n"
            "print('Card -> token:', df['card_number'][0], '->', tok['card_number'][0])\n"
            "print('Amount exact:', df['transaction_amount_usd'][0],\n"
            "      '-> fuzzy:', obf.fuzzy_bucket(df['transaction_amount_usd'][0], width=5000))\n\n"
            "anon, risk = obf.k_anonymize(df, ['transaction_date', 'cardholder_city', 'transaction_amount_usd'], k=5)\n"
            "print(f'Rows still in k<5 quasi-id classes after k-anonymity: {risk:.1%}')"),
        (C, "import matplotlib.pyplot as plt\n"
            "rows = df.head(6)\n"
            "x = range(len(rows))\n"
            "fig, axes = plt.subplots(1, 2, figsize=(10.5, 3.8))\n"
            "axes[0].bar([i-0.2 for i in x], rows['card_number'].str.len(), width=0.4, label='original', color='#e05b5b')\n"
            "axes[0].bar([i+0.2 for i in x], obf.apply_masking(df, mask=['card_number']).head(6)['card_number'].str.len(),\n"
            "            width=0.4, label='masked', color='#35c4b6')\n"
            "axes[0].set_ylabel('Card number length (chars)'); axes[0].set_title('Masking preserves structure')\n"
            "axes[0].legend()\n"
            "axes[1].bar(['exact', 'fuzzy $5K'], [1.0, 1/8], color=['#e05b5b', '#35c4b6'])\n"
            "axes[1].set_yscale('log'); axes[1].set_ylabel('guessing probability')\n"
            "axes[1].set_title('Fuzzy ranges crush KBA guessing odds')"),
    ],

    "19_obfuscation_threat_scenarios": [
        (M, "# The 8 obfuscation threat scenarios\n\nRun all adversarial scenarios "
            "from the obfuscation study on synthetic credit-card transaction "
            "data — BEC/fraud, insider threat, supply-chain leakage, sanctions "
            "evasion, corporate espionage, test-environment exposure, account "
            "takeover and re-identification — and compare the risk before vs "
            "after each obfuscation control."),
        (C, "import sys\nfrom pathlib import Path\n"
            "sys.path.insert(0, str(Path.cwd()))   # repo root (kernel cwd)\n\n"
            "from examples.obfuscation.credit_card_data import generate_credit_card\n"
            "from examples.obfuscation import experiments as exp\n\n"
            "df = generate_credit_card(2000, seed=42)\n"
            "print(f'{len(df):,} synthetic credit-card records ready')"),
        (C, "print(exp.experiment1_bec_fraud(df))"),
        (C, "print(exp.experiment2_insider_threat(df))\n"
            "print(exp.experiment3_supply_chain(df))"),
        (C, "print(exp.experiment4_sanctions_evasion(df))\n"
            "print(exp.experiment5_corporate_espionage(df))\n"
            "print(exp.experiment6_test_environment(df))"),
        (C, "print(exp.experiment7_ato_security(df))\n"
            "print(exp.experiment8_reidentification(df))\n"
            "print(exp.experiment9_counterparty(df))"),
        (C, "report = exp.run_all(df)\n"
            "open('examples/obfuscation/obfuscation_report.md', 'w').write(report)\n"
            "print('Wrote examples/obfuscation/obfuscation_report.md with', len(report), 'chars')"),
    ],

    # ------------------------------------------------- PRIVACY MCP TOOLS -----
    "20_privacy_assessment": [
        (M, "# Privacy assessment & red-teaming (privacy MCP tools)\n\nBuild a "
            "small clinical cohort and run the privacy server's detection, "
            "assessment, membership-inference and re-identification tools — the "
            "same functions the agent calls as `privacy__*` MCP tools."),
        (C, "import sys\nfrom pathlib import Path\n"
            "sys.path.insert(0, str(Path.cwd()))   # repo root (kernel cwd)\n\n"
            "from examples.privacy.clinical_cohort import build_cohort\n"
            "from mcp_servers import privacy_tools as pt\n\n"
            "cohort = build_cohort(200, seed=7)\n"
            "cohort.to_csv('examples/privacy/clinical_cohort.csv', index=False)\n"
            "print(cohort.shape)"),
        (C, "print(pt.detect_pii_in_text(\n"
            "    'Contact d.smith@example.org or +1 555-010-1234. Card 4111-1111-1111-1111. '\n"
            "    'SSN 123-45-6789.'))"),
        (C, "print(pt.assess_dataframe_privacy('examples/privacy/clinical_cohort.csv'))"),
        (C, "print(pt.privacy_redteam_checklist('clinical cohort', has_model=False, public_release=True))\n"
            "import numpy as np\n"
            "rng = np.random.default_rng(7)\n"
            "preds = np.concatenate([rng.uniform(0.6, 0.99, 400), rng.uniform(0.01, 0.4, 400)])\n"
            "labels = [True]*400 + [False]*400\n"
            "print(pt.membership_inference_eval(preds.tolist(), labels, 0.5))"),
        (C, "eq = cohort.groupby(['age', 'zip_prefix', 'condition']).size().values\n"
            "print(pt.reidentification_scenario(['age', 'zip_prefix', 'condition'], 50000, eq.tolist()))\n\n"
            "import matplotlib.pyplot as plt\n"
            "fig, ax = plt.subplots(figsize=(6.4, 3.6))\n"
            "ax.hist(eq, bins=30, color='#4f8cff', edgecolor='#161c24')\n"
            "ax.axvline(1, color='#e05b5b', ls='--', label=f\"{(eq == 1).mean()*100:.0f}% singletons\")\n"
            "ax.set_yscale('log'); ax.set_xlabel('equivalence class size'); ax.set_ylabel('classes')\n"
            "ax.legend(); ax.set_title('Re-identification risk — small cohort')"),
    ],

    "21_differential_privacy": [
        (M, "# Differential privacy with the privacy MCP tools\n\nApply Laplace / "
            "Gaussian mechanisms to real aggregate queries, track the privacy "
            "budget, and read off the (ε, δ) guarantee."),
        (C, "import sys\nfrom pathlib import Path\n"
            "sys.path.insert(0, str(Path.cwd()))   # repo root (kernel cwd)\n\n"
            "import pandas as pd\n"
            "from mcp_servers import privacy_tools as pt\n\n"
            "# Ensure the cohort CSV exists (generated by 20_privacy_assessment).\n"
            "if not Path('examples/privacy/clinical_cohort.csv').exists():\n"
            "    from examples.privacy.clinical_cohort import build_cohort\n"
            "    build_cohort(200, seed=7).to_csv('examples/privacy/clinical_cohort.csv', index=False)\n"
            "cohort = pd.read_csv('examples/privacy/clinical_cohort.csv')\n"
            "counts = cohort.groupby(cohort['admission_date'].str[:7]).size().sort_index()\n"
            "print(counts)"),
        (C, "import json\n"
            "dp = json.loads(pt.apply_laplace_dp(counts.tolist(), epsilon=0.5, sensitivity=1.0, seed=7))\n"
            "print('scale =', dp['scale'])\n"
            "print('noisy =', [round(v, 1) for v in dp['noisy_values']])\n"
            "print('guarantee =', dp['privacy_guarantee'])\n"
            "print(pt.dp_guarantee_summary(0.5, 1e-6))"),
        (C, "print(pt.dp_privacy_budget_report([\n"
            "    {'epsilon': 0.5, 'delta': 0.0, 'description': 'admission histogram'},\n"
            "    {'epsilon': 0.3, 'delta': 0.0, 'description': 'mean visit amount'},\n"
            "    {'epsilon': 0.2, 'delta': 0.0, 'description': 'per-condition counts'},\n"
            "]))\n\n"
            "print('Total ε budget spent: 1.0 of a recommended 1.0 max.')"),
        (C, "import numpy as np\nimport matplotlib.pyplot as plt\n"
            "dp = json.loads(pt.apply_laplace_dp(counts.tolist(), epsilon=0.5, sensitivity=1.0, seed=7))\n"
            "x = np.arange(len(counts))\n"
            "fig, ax = plt.subplots(figsize=(8, 3.8))\n"
            "ax.bar(x - 0.2, counts.values, width=0.4, label='original', color='#e05b5b')\n"
            "ax.bar(x + 0.2, dp['noisy_values'], width=0.4, label='Laplace ε=0.5', color='#35c4b6')\n"
            "ax.set_xticks(x); ax.set_xticklabels(counts.index, rotation=45, fontsize=8)\n"
            "ax.set_ylabel('admissions / month'); ax.legend()\n"
            "ax.set_title('Original vs differentially-private histogram')"),
    ],

    "22_synthetic_data": [
        (M, "# Synthetic data from the privacy MCP tools\n\nGenerate a "
            "schema-preserving synthetic version of the clinical cohort, then "
            "compare utility (means, spreads, distributions) against the real "
            "data before sharing."),
        (C, "import sys\nfrom pathlib import Path\n"
            "sys.path.insert(0, str(Path.cwd()))   # repo root (kernel cwd)\n\n"
            "from mcp_servers import privacy_tools as pt\n\n"
            "# Ensure the cohort CSV exists (generated by 20_privacy_assessment).\n"
            "if not Path('examples/privacy/clinical_cohort.csv').exists():\n"
            "    from examples.privacy.clinical_cohort import build_cohort\n"
            "    build_cohort(200, seed=7).to_csv('examples/privacy/clinical_cohort.csv', index=False)\n"
            "print(pt.generate_synthetic_tabular(\n"
            "    'examples/privacy/clinical_cohort.csv', num_rows=1000, method='smoothed', seed=7))"),
        (C, "print(pt.synthetic_data_quality_report(\n"
            "    'examples/privacy/clinical_cohort.csv',\n"
            "    'examples/privacy/synthetic_clinical_cohort_1000.csv'))"),
        (C, "import pandas as pd\nimport matplotlib.pyplot as plt\n"
            "real = pd.read_csv('examples/privacy/clinical_cohort.csv')\n"
            "synth = pd.read_csv('examples/privacy/synthetic_clinical_cohort_1000.csv')\n"
            "fig, axes = plt.subplots(1, 2, figsize=(10, 3.8))\n"
            "for ax, col in zip(axes, ['age', 'visit_amount_usd']):\n"
            "    ax.hist(real[col], bins=25, alpha=0.6, label='real', color='#4f8cff')\n"
            "    ax.hist(synth[col], bins=25, alpha=0.6, label='synthetic', color='#35c4b6')\n"
            "    ax.set_xlabel(col); ax.set_ylabel('count'); ax.legend()\n"
            "axes[0].set_title('Age — real vs synthetic')\n"
            "axes[1].set_title('Visit amount — real vs synthetic')"),
    ],

    "23_privacy_peer_workflow": [
        (M, "# Privacy workflow — peer exploitation · red team · DP robustness\n\n"
            "This is the end-to-end pipeline triggered by the researcher's privacy "
            "workflow prompt. Stage 1 models an attacker who is a *peer in the "
            "distribution* (holding their own data from the same population at "
            "different coverage levels); Stage 2 hunts corner cases with the "
            "red-team MCP tools; Stage 3 releases the exploited aggregates under "
            "Laplace DP and re-runs the attacks to measure robustness. A full "
            "audit trail is written to examples/privacy/reports/."),
        (C, "import sys\nfrom pathlib import Path\n"
            "sys.path.insert(0, str(Path.cwd()))\n\n"
            "exec(open('examples/privacy/run_peer_exploitation.py').read())"),
    ],

    # ------------------------------------------------ ADVERSARIAL ROBUSTNESS --
    "24_adversarial_creditcard_robustness": [
        (M, "# Adversarial robustness — credit-card fraud\n\nBuild a binary "
            "classifier (FRAUD_FLAGGED vs not) on synthetic credit-card "
            "transaction data from the obfuscation-study generator, then attack "
            "it with an FGSM-style gradient perturbation and measure robust "
            "accuracy / ASR across perturbation budgets."),
        (C, "import sys\nfrom pathlib import Path\n"
            "sys.path.insert(0, str(Path.cwd()))\n\n"
            "import pandas as pd\n"
            "from examples.adversarial import credit_card_binary_dataset, train_test, train, robustness_sweep\n"
            "from mcp_servers import robustness_tools as rt\n\n"
            "import os\nX, y, feats, target = credit_card_binary_dataset(2000, seed=int(os.environ.get('FOX_RUN_SEED', '42')))\n"
            "Xtr, Xte, ytr, yte = train_test(X, y)\n"
            "print('features:', feats, '| target:', target)\n"
            "print('train/test:', Xtr.shape[0], '/', Xte.shape[0])"),
        (C, "model = train('lr', Xtr, ytr)\n"
            "clean = model.predict(Xte)\n"
            "print('clean accuracy: %.3f' % (clean == yte).mean())\n"
            "print(rt.adversarial_robustness_checklist('sklearn', 'tabular', high_stakes=False))"),
        (C, "epsilons = [0.0, 0.1, 0.25, 0.5, 1.0, 2.0]\n"
            "rows = robustness_sweep(model, Xte, yte, epsilons)\n"
            "df = pd.DataFrame(rows)\n"
            "print(df.to_string(index=False))\n\n"
            "import matplotlib.pyplot as plt\n"
            "fig, ax = plt.subplots(figsize=(7, 4))\n"
            "ax.plot(df['eps'], df['clean_accuracy'], 'o-', label='clean accuracy', color='#4f8cff')\n"
            "ax.plot(df['eps'], df['robust_accuracy'], 'o-', label='robust accuracy', color='#e05b5b')\n"
            "ax.plot(df['eps'], df['asr'], 'o--', label='ASR (on correct)', color='#d9a441')\n"
            "ax.set_xlabel('perturbation budget eps'); ax.set_ylabel('accuracy')\n"
            "ax.legend(); ax.set_title('Credit-card fraud classifier — robustness vs eps')\n"
            "ax.grid(alpha=0.3)"),
    ],

    "25_adversarial_clinical_robustness": [
        (M, "# Adversarial robustness — clinical cohort\n\nRun the same "
            "robustness battery on the small clinical cohort (insurance "
            "prediction). n is small so the numbers are noisy — that is the "
            "point of this test."),
        (C, "import sys\nfrom pathlib import Path\n"
            "sys.path.insert(0, str(Path.cwd()))\n\n"
            "import pandas as pd\n"
            "from examples.adversarial import clinical_binary_dataset, train_test, train, robustness_sweep\n\n"
            "import os\nX, y, feats, target = clinical_binary_dataset(300, seed=int(os.environ.get('FOX_RUN_SEED', '7')))\n"
            "Xtr, Xte, ytr, yte = train_test(X, y)\n"
            "print('features:', feats, '| target:', target)\n"
            "print('train/test:', Xtr.shape[0], '/', Xte.shape[0])\n"
            "print('class balance: %.2f' % y.mean())"),
        (C, "model = train('mlp', Xtr, ytr)\n"
            "print('clean accuracy: %.3f' % (model.predict(Xte) == yte).mean())\n"
            "epsilons = [0.0, 0.1, 0.25, 0.5, 1.0]\n"
            "df = pd.DataFrame(robustness_sweep(model, Xte, yte, epsilons))\n"
            "print(df.to_string(index=False))\n\n"
            "import matplotlib.pyplot as plt\n"
            "fig, ax = plt.subplots(figsize=(7, 4))\n"
            "ax.plot(df['eps'], df['clean_accuracy'], 'o-', label='clean accuracy', color='#4f8cff')\n"
            "ax.plot(df['eps'], df['robust_accuracy'], 'o-', label='robust accuracy', color='#e05b5b')\n"
            "ax.plot(df['eps'], df['asr'], 'o--', label='ASR (on correct)', color='#d9a441')\n"
            "ax.set_xlabel('perturbation budget eps'); ax.set_ylabel('accuracy')\n"
            "ax.legend(); ax.set_title('Clinical cohort — robustness vs eps (small n)')\n"
            "ax.grid(alpha=0.3)"),
    ],

    "26_adversarial_model_comparison": [
        (M, "# Model comparison under attack\n\nTrain logistic regression, "
            "random forest and a small MLP on the same credit-card dataset and "
            "compare clean vs robust accuracy and ASR at a fixed epsilon."),
        (C, "import sys\nfrom pathlib import Path\n"
            "sys.path.insert(0, str(Path.cwd()))\n\n"
            "import pandas as pd\n"
            "from examples.adversarial import credit_card_binary_dataset, train_test, train, evaluate_robustness\n\n"
            "import os\nX, y, feats, target = credit_card_binary_dataset(2000, seed=int(os.environ.get('FOX_RUN_SEED', '42')))\n"
            "Xtr, Xte, ytr, yte = train_test(X, y)\n"
            "EPS = 0.5\n"
            "rows = []\n"
            "models = {}\n"
            "for name in ['lr', 'rf', 'mlp']:\n"
            "    m = train(name, Xtr, ytr)\n"
            "    models[name] = m\n"
            "    r = evaluate_robustness(m, Xte, yte, EPS)\n"
            "    rows.append({'model': name.upper(), 'clean_acc': r['clean_accuracy'],\n"
            "                 'robust_acc': r['robust_accuracy'], 'asr': r['attack_success_rate_on_correct']})\n"
            "df = pd.DataFrame(rows)\n"
            "print(df.to_string(index=False))"),
        (C, "import matplotlib.pyplot as plt\n"
            "x = range(len(df))\n"
            "fig, ax = plt.subplots(figsize=(7.5, 4))\n"
            "ax.bar([i - 0.2 for i in x], df['clean_acc'], width=0.4, label='clean accuracy', color='#4f8cff')\n"
            "ax.bar([i + 0.2 for i in x], df['robust_acc'], width=0.4, label=f'robust accuracy (eps={EPS})', color='#e05b5b')\n"
            "ax.set_xticks(list(x)); ax.set_xticklabels(df['model'])\n"
            "ax.set_ylabel('accuracy'); ax.legend()\n"
            "ax.set_title('Robustness by model family (credit-card fraud task)')\n"
            "for i, r in enumerate(df['asr']):\n"
            "    ax.text(i + 0.2, 0.02, 'ASR %.0f%%' % (r * 100), fontsize=8, ha='center')\n"
            "ax.grid(alpha=0.3, axis='y')"),
    ],

    "27_adversarial_fgsm_art": [
        (M, "# FGSM demo + ART evaluation\n\nApply the lightweight "
            "`robustness__simple_fgsm_perturbation` to a single feature vector, "
            "then attempt the full ART `robustness__evaluate_sklearn_robustness` "
            "(requires adversarial-robustness-toolbox)."),
        (C, "import sys\nfrom pathlib import Path\n"
            "sys.path.insert(0, str(Path.cwd()))\n\n"
            "import joblib, numpy as np\n"
            "from examples.adversarial import credit_card_binary_dataset, train_test, train, fgsm_grad\n"
            "from mcp_servers import robustness_tools as rt\n\n"
            "import os\nX, y, feats, target = credit_card_binary_dataset(1000, seed=int(os.environ.get('FOX_RUN_SEED', '42')))\n"
            "Xtr, Xte, ytr, yte = train_test(X, y)\n"
            "m = train('lr', Xtr, ytr)\n"
            "joblib.dump(m, 'examples/adversarial/creditcard_lr.joblib')\n"
            "np.save('examples/adversarial/X_test.npy', Xte)\n"
            "np.save('examples/adversarial/y_test.npy', yte)\n"
            "print('saved model + test arrays under examples/adversarial/')"),
        (C, "g = fgsm_grad(m, Xte[:1], yte[:1])[0]\n"
            "print(rt.simple_fgsm_perturbation(Xte[0].tolist(), g.tolist(), eps=0.5))"),
        (C, "print(rt.evaluate_sklearn_robustness(\n"
            "    'examples/adversarial/creditcard_lr.joblib',\n"
            "    'examples/adversarial/X_test.npy',\n"
            "    'examples/adversarial/y_test.npy',\n"
            "    attack='ProjectedGradientDescent', eps=0.5, norm='inf'))"),
    ],
}


def main() -> None:
    print(f"Building {len(NOTEBOOKS)} demo notebooks -> {OUT_DIR}")
    for name, cells in NOTEBOOKS.items():
        write(name, cells)


if __name__ == "__main__":
    main()
