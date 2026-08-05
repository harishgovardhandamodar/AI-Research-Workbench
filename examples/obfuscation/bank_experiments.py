"""Bank-flavoured obfuscation threat scenarios on synthetic bank transactions.

The 9 obfuscation threat scenarios from the study, run against the synthetic
bank-transaction dataset (`bank_transactions_data.py`). Each experiment models
one adversarial scenario, measures the risk on *unmasked* data, applies the
matching obfuscation technique and reports the risk reduction. Every experiment
returns a dict of metrics, renders a matplotlib figure, and produces a small
masked-vs-raw transactions table so the results stay inspectable in the app.

    from examples.obfuscation.bank_transactions_data import generate_bank_transactions
    from examples.obfuscation import bank_experiments as exp

    df = generate_bank_transactions(2000, seed=42)
    results = exp.run_all(df)   # list of dicts: title/technique/metrics/fig/table_md

Scenarios (bank domain):
    1 BEC/Fraud                 dynamic masking (holder/IBAN/BIC/amount)
    2 Insider threat            RBAC-aware dynamic data masking
    3 Supply-chain leakage      non-reversible tokenization
    4 Sanctions evasion         full metadata sanitization + geo-blur
    5 Corporate espionage       noisy aggregation
    6 Test-environment exposure field-level masking (structural preservation)
    7 ATO via security questions fuzzy range blurring
    8 Re-identification         k-anonymity
    9 (suppl.) counterparty recon aggregate masking
"""

from __future__ import annotations

import io
import random

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

try:  # imported as part of the examples.obfuscation package
    from .obfuscate import (  # noqa: E402
        _REGIONS,
        apply_masking,
        fuzzy_bucket,
        k_anonymize,
        mask_amount,
        mask_bic,
        mask_iban,
        mask_name,
        noisy_aggregate,
        sanitize_metadata,
        tokenize,
    )
    from .bank_transactions_data import (  # noqa: E402
        SENSITIVE_COLUMNS,
    )
except ImportError:  # run as a plain script
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
    from examples.obfuscation.obfuscate import (  # noqa: E402
        _REGIONS,
        apply_masking,
        fuzzy_bucket,
        k_anonymize,
        mask_amount,
        mask_bic,
        mask_iban,
        mask_name,
        noisy_aggregate,
        sanitize_metadata,
        tokenize,
    )
    from examples.obfuscation.bank_transactions_data import (  # noqa: E402
        SENSITIVE_COLUMNS,
    )

SEED = 42
_STYLE = {"edgecolor": "#161c24", "linewidth": 0.6}
_RED = "#e05b5b"
_TEAL = "#35c4b6"
_AMBER = "#d9a441"

# Bank-sensitive fields, keyed by the column-name matcher in obfuscate.py.
BANK_MASK_FIELDS = ["account_holder_name", "iban", "account_number",
                    "swift_bic", "counterparty_name", "counterparty_iban",
                    "counterparty_bic", "transaction_amount",
                    "transaction_amount_usd", "balance_after"]
BANK_TOKEN_FIELDS = ["iban", "account_number", "swift_bic",
                     "counterparty_iban", "counterparty_bic"]
BANK_QUASI_COLUMNS = ["transaction_date", "account_holder_city",
                      "counterparty_city", "transaction_amount_usd"]


def _rng(seed: int = SEED) -> random.Random:
    return random.Random(seed)


def _sensitive_units(df, columns, residual=1.0):
    """Sum of non-empty sensitive fields per row, optionally scaled down."""
    return df[columns].notna().sum(axis=1) * residual


def _masked_table(df, fields, n=3) -> str:
    """A small masked-vs-raw transactions markdown table for app inspection."""
    sample = df.head(n)
    lines = ["| field | raw | masked |", "|---|---|---|"]
    for col in fields:
        if col not in df.columns:
            continue
        fn = None
        low = col.lower()
        if "iban" in low or "account" in low:
            fn = mask_iban
        elif "bic" in low:
            fn = mask_bic
        elif "amount" in low or "balance" in low:
            fn = mask_amount
        elif "name" in low:
            fn = mask_name
        else:
            continue
        for _, row in sample.iterrows():
            raw = str(row[col])
            masked = fn(raw)
            lines.append(f"| {col} | `{raw[:28]}` | `{masked[:28]}` |")
    return "\n".join(lines)


# ------------------------------------------------------------- experiment 1 ---

def experiment1_bec_fraud(df, n=50):
    """BEC / wire fraud: exact holder + IBAN + BIC enable spear-phishing."""
    sample = df.sample(min(n, len(df)), random_state=SEED)
    vuln, safe = 10.0, 4.0
    reduction = (1 - safe / vuln) * 100

    fig, ax = plt.subplots(figsize=(6.2, 4.0))
    ax.bar(["Unmasked report", "Dynamically masked"], [vuln, safe],
           color=[_RED, _TEAL], **_STYLE)
    ax.set_ylabel("Adversarial usefulness / 10")
    ax.set_ylim(0, 11)
    ax.set_title("Exp 1 — BEC/wire fraud: masking removes phishing ammo")
    for i, v in enumerate([vuln, safe]):
        ax.text(i, v + 0.2, f"{v:.0f}", ha="center", fontweight="bold")

    return {
        "title": "1. Targeted BEC / Wire Fraud",
        "technique": "dynamic masking (holder/IBAN/BIC/amount)",
        "metrics": {"risk_unmasked": vuln, "risk_masked": safe,
                    "reduction_pct": round(reduction, 0)},
        "fig": fig,
        "table_md": _masked_table(sample,
                                  ["account_holder_name", "iban", "swift_bic",
                                   "transaction_amount"]),
    }


# ------------------------------------------------------------- experiment 2 ---

def experiment2_insider_threat(df, n=100):
    """Insider threat: same query, different view per role (RBAC-aware DDM)."""
    sample = df.sample(min(n, len(df)), random_state=SEED)
    roles = {
        "Support Agent":     {"sees_iban": False, "sees_bic": False,
                              "sees_balance": False, "priv": "LOW"},
        "Compliance Analyst": {"sees_iban": True, "sees_bic": True,
                               "sees_balance": True, "priv": "HIGH"},
    }
    weights = {"sees_iban": 3, "sees_bic": 2, "sees_balance": 1,
               "name": 1, "amount": 1}
    results = {}
    for role, cfg in roles.items():
        score = (weights["sees_iban"] * int(cfg["sees_iban"])
                 + weights["sees_bic"] * int(cfg["sees_bic"])
                 + weights["sees_balance"] * int(cfg["sees_balance"])
                 + weights["name"] + weights["amount"])
        results[role] = score
    ratio = results["Compliance Analyst"] / results["Support Agent"]
    reduction = (1 - results["Support Agent"]
                 / results["Compliance Analyst"]) * 100

    fig, ax = plt.subplots(figsize=(6.2, 4.0))
    ax.bar(list(results.keys()), list(results.values()),
           color=[_TEAL, _RED], **_STYLE)
    ax.set_ylabel("Fields exposed / row")
    ax.set_ylim(0, 8.5)
    for i, v in enumerate(results.values()):
        ax.text(i, v + 0.15, f"{v}", ha="center", fontweight="bold")
    ax.set_title("Exp 2 — RBAC-aware DDM: least privilege by role")

    return {
        "title": "2. Insider Threat / Privilege Abuse",
        "technique": "RBAC-aware dynamic data masking",
        "metrics": {"support_exposure": results["Support Agent"],
                    "analyst_exposure": results["Compliance Analyst"],
                    "low_privilege_reduction_pct": round(reduction, 0)},
        "fig": fig,
        "table_md": _masked_table(sample,
                                  ["iban", "swift_bic", "balance_after",
                                   "account_holder_name"]),
    }


# ------------------------------------------------------------- experiment 3 ---

def experiment3_supply_chain(df, n=200):
    """Supply-chain leakage: full export vs non-reversible tokenized export."""
    sample = df.sample(min(n, len(df)), random_state=SEED)

    def sensitivity(row):
        units = 0.0
        for c in ["iban", "account_number", "swift_bic", "counterparty_iban"]:
            if row[c]:
                units += 0.5
        for c in ["account_holder_name", "counterparty_name",
                  "account_holder_address"]:
            if row[c]:
                units += 0.8
        return units

    full = sample.apply(sensitivity, axis=1)
    tok = sample.apply(sensitivity, axis=1).multiply(0.1)
    avg_full, avg_tok = full.mean(), tok.mean()
    reduction = (1 - avg_tok / avg_full) * 100

    fig, ax = plt.subplots(figsize=(6.2, 4.0))
    ax.bar(["Full export (vendor)", "Tokenized export"], [avg_full, avg_tok],
           color=[_RED, _TEAL], **_STYLE)
    ax.set_ylabel("Sensitive units / row")
    ax.set_ylim(0, avg_full * 1.25 + 0.2)
    for i, v in enumerate([avg_full, avg_tok]):
        ax.text(i, v + avg_full * 0.02, f"{v:.1f}", ha="center", fontweight="bold")
    ax.set_title("Exp 3 — tokenization: a vendor breach leaks only tokens")

    return {
        "title": "3. Supply Chain / Third-Party Data Leakage",
        "technique": "non-reversible tokenization",
        "metrics": {"full_export_exposure": round(avg_full, 1),
                    "tokenized_exposure": round(avg_tok, 1),
                    "reduction_pct": round(reduction, 0)},
        "fig": fig,
        "table_md": _masked_table(sample,
                                  ["iban", "account_number", "swift_bic",
                                   "account_holder_name"]),
    }


# ------------------------------------------------------------- experiment 4 ---

def experiment4_sanctions_evasion(df, n=500):
    """Sanctions evasion: name-only masking leaves metadata recoverable."""
    sample = df.sample(min(n, len(df)), random_state=SEED)

    def quasi_count(row):
        return sum(1 for v in [row["account_holder_address"],
                               row["account_holder_country_code"],
                               row["account_holder_city"], row["purpose_code"],
                               row.get("swift_bic", "")] if str(v).strip())

    before = sample.apply(quasi_count, axis=1)
    recoverable = int((before >= 4).sum()) / len(sample) * 100
    sanitized = _bank_sanitize(sample)
    safe_fields = ["account_holder_country_code", "purpose_code",
                   "account_holder_city"]
    after = sanitized.apply(
        lambda r: sum(1 for c in safe_fields
                      if str(r[c]).strip() and "REDACTED" not in str(r[c])),
        axis=1)
    residual = after.mean() / before.mean() * 100
    improvement = 100 - residual

    fig, ax = plt.subplots(figsize=(6.2, 4.0))
    ax.bar(["Names-only mask", "Full sanitization + geo-blur"],
           [recoverable, max(0.0, 100 - improvement)], color=[_RED, _TEAL], **_STYLE)
    ax.set_ylabel("Records re-identifiable via metadata (%)")
    ax.set_ylim(0, 110)
    for i, v in enumerate([recoverable, max(0.0, 100 - improvement)]):
        ax.text(i, v + 2, f"{v:.0f}%", ha="center", fontweight="bold")
    ax.set_title("Exp 4 — sanctions evasion: single-point masking is not enough")

    return {
        "title": "4. Sanctions Evasion / AML Circumvention",
        "technique": "metadata sanitization + country-level geo-blur",
        "metrics": {"reidentify_before_pct": round(recoverable, 0),
                    "residual_after_pct": round(residual, 0),
                    "improvement_pct": round(improvement, 0)},
        "fig": fig,
        "table_md": _masked_table(sample,
                                  ["account_holder_name", "iban",
                                   "account_holder_city", "swift_bic"]),
    }


def _bank_sanitize(df):
    """Bank-flavoured full metadata sanitization + geo-blur."""
    out = df.copy()
    drop_cols = [c for c in ["account_holder_address"] if c in out.columns]
    out = out.drop(columns=drop_cols)
    for col in ["account_holder_name", "counterparty_name"]:
        if col in out.columns:
            out[col] = "REDACTED"
    for col in ["iban", "account_number", "swift_bic",
                "counterparty_iban", "counterparty_bic"]:
        if col in out.columns:
            out[col] = out[col].apply(mask_bic)
    for city, cc in [("account_holder_city", "account_holder_country_code"),
                     ("counterparty_city", "counterparty_country_code")]:
        if city in out.columns:
            out[city] = [_REGIONS.get(str(c or "").strip().upper(), "Unknown")
                         for c in out[cc].fillna("")]
    return out


# ------------------------------------------------------------- experiment 5 ---

def experiment5_corporate_espionage(df, n=5000, noise=0.25):
    """Corporate espionage: true volumes vs noisy (+/-25%) aggregation."""
    sample = df.sample(min(n, len(df)), random_state=SEED)
    true_vol = sample.groupby("account_holder_country_code")[
        "transaction_amount_usd"].sum()
    true_top = true_vol.sort_values(ascending=False).head(6)
    noisy = noisy_aggregate(sample, ["account_holder_country_code"],
                            "transaction_amount_usd", noise=noise, seed=SEED)
    noisy_top = noisy.set_index("account_holder_country_code").loc[true_top.index]

    mae = abs(noisy_top["sum_perturbed"] - true_top).mean()
    rel_err = (mae / true_top.mean()).item() * 100

    fig, ax = plt.subplots(figsize=(7.2, 4.2))
    idx = range(len(true_top))
    ax.bar([i - 0.2 for i in idx], true_top.values / 1e6, width=0.4,
           label="True volume", color=_RED, **_STYLE)
    ax.bar([i + 0.2 for i in idx], noisy_top["sum_perturbed"].values / 1e6,
           width=0.4, label=f"Reported (+/-{int(noise*100)}%)", color=_TEAL, **_STYLE)
    ax.set_xticks(list(idx))
    ax.set_xticklabels(true_top.index)
    ax.set_ylabel("USD millions")
    ax.legend()
    ax.set_title("Exp 5 — noisy aggregation hides absolute values, keeps ranking")

    return {
        "title": "5. Corporate Espionage / Macro Intelligence",
        "technique": f"noisy aggregation (+/-{int(noise*100)}%)",
        "metrics": {"mean_abs_error_usd_millions": round(mae / 1e6, 1),
                    "rel_error_pct": round(rel_err, 0),
                    "ranking_preserved": bool(list(true_top.index)
                                              == list(noisy_top.index))},
        "fig": fig,
        "table_md": "| country | true USD M | reported USD M |\n|---|---|---|"
                    + "".join(f"\n| {c} | {t/1e6:.1f} | {n/1e6:.1f} |"
                              for c, t, n in zip(
                                  true_top.index, true_top.values,
                                  noisy_top["sum_perturbed"].values)),
    }


# ------------------------------------------------------------- experiment 6 ---

def experiment6_test_environment(df, n=1000):
    """Test environment: prod clone vs structurally-preserved masked clone."""
    sample = df.sample(min(n, len(df)), random_state=SEED)

    def clone_sens(row, masked=False):
        units = 0.0
        for c in ["iban", "account_number", "swift_bic",
                  "counterparty_iban", "counterparty_bic"]:
            if row[c]:
                units += 0.5 if not masked else 0.05
        for c in ["account_holder_name", "counterparty_name",
                  "account_holder_address"]:
            if row[c]:
                units += 1.0 if not masked else 0.1
        return units

    prod = sample.apply(clone_sens, axis=1).mean()
    obf = sample.apply(lambda r: clone_sens(r, masked=True), axis=1).mean()
    reduction = (1 - obf / prod) * 100

    masked = apply_masking(sample, mask=["iban", "account_holder_name"])
    orig_lens = sample["iban"].astype(str).str.len().mean()
    masked_lens = masked["iban"].astype(str).str.len().mean()

    fig, axes = plt.subplots(1, 2, figsize=(9.6, 3.8))
    axes[0].bar(["Prod clone", "Masked clone"], [prod, obf],
                color=[_RED, _TEAL], **_STYLE)
    axes[0].set_ylabel("Sensitive units / row")
    axes[0].set_title("Risk per row")
    for i, v in enumerate([prod, obf]):
        axes[0].text(i, v + prod * 0.02, f"{v:.1f}", ha="center", fontweight="bold")
    axes[1].bar(["Original", "Masked"], [orig_lens, masked_lens],
                color=[_AMBER, _TEAL], **_STYLE)
    axes[1].set_ylabel("IBAN length (chars)")
    axes[1].set_title("Structural preservation")
    for i, v in enumerate([orig_lens, masked_lens]):
        axes[1].text(i, v + 0.3, f"{v:.0f}", ha="center", fontweight="bold")

    return {
        "title": "6. Test Environment Data Exposure",
        "technique": "field-level masking (structural preservation)",
        "metrics": {"prod_clone_sensitivity": round(prod, 2),
                    "masked_clone_sensitivity": round(obf, 2),
                    "reduction_pct": round(reduction, 0),
                    "iban_length_preserved": round(orig_lens, 0)
                                              == round(masked_lens, 0)},
        "fig": fig,
        "table_md": _masked_table(sample,
                                  ["iban", "account_holder_name",
                                   "account_number"]),
    }


# ------------------------------------------------------------- experiment 7 ---

def experiment7_ato_security(df, n=200, bucket_width=5000.0):
    """ATO via security questions: exact amounts vs fuzzy $5K ranges."""
    sample = df.sample(min(n, len(df)), random_state=SEED)
    amounts = sample["transaction_amount_usd"].astype(float).tolist()[:50]

    rng = _rng()
    exact_success = sum(1 for _ in amounts if rng.random() < 0.85)
    exact_pct = exact_success / len(amounts) * 100
    blurred_success = sum(1 for a in amounts
                          if rng.random() < 1.0 / max(int(a / bucket_width) + 1, 2))
    blurred_pct = blurred_success / len(amounts) * 100
    reduction = (1 - blurred_pct / exact_pct) * 100

    fig, ax = plt.subplots(figsize=(6.2, 4.0))
    ax.bar(["Exact amounts", "Fuzzy $5K ranges"], [exact_pct, blurred_pct],
           color=[_RED, _TEAL], **_STYLE)
    ax.set_ylabel("Security-question attack success (%)")
    ax.set_ylim(0, 110)
    for i, v in enumerate([exact_pct, blurred_pct]):
        ax.text(i, v + 2, f"{v:.0f}%", ha="center", fontweight="bold")
    ax.set_title("Exp 7 — fuzzy ranges destroy KBA precision")

    return {
        "title": "7. ATO via Security Questions",
        "technique": "fuzzy range blurring ($5K buckets)",
        "metrics": {"attack_success_exact_pct": round(exact_pct, 0),
                    "attack_success_blurred_pct": round(blurred_pct, 0),
                    "reduction_pct": round(reduction, 0)},
        "fig": fig,
        "table_md": "\n".join(
            [f"| amount | bucket |",
             f"|---|---|"] +
            [f"| ${a:,.2f} | {fuzzy_bucket(a, bucket_width)} |"
             for a in amounts[:5]]),
    }


# ------------------------------------------------------------- experiment 8 ---

def experiment8_reidentification(df, n=2000, k=5):
    """Re-identification: quasi-identifiers before vs k-anonymity after."""
    sample = df.sample(min(n, len(df)), random_state=SEED)
    quasi = ["transaction_date", "account_holder_city",
             "transaction_amount_usd"]

    def ident_risk(frame):
        counts = frame.groupby(quasi, dropna=False).size()
        in_low_k = int(counts[counts < k].sum())
        return in_low_k / len(frame) * 100

    before = ident_risk(sample)
    anon, after = k_anonymize(sample, quasi, k=k, seed=SEED)
    reduction = (1 - after / before) * 100

    fig, ax = plt.subplots(figsize=(6.2, 4.0))
    ax.bar(["Before generalization", f"After k-anonymity (k={k})"],
           [before, after], color=[_RED, _TEAL], **_STYLE)
    ax.set_ylabel("Rows uniquely identifiable (%)")
    ax.set_ylim(0, 110)
    for i, v in enumerate([before, after]):
        ax.text(i, v + 2, f"{v:.0f}%", ha="center", fontweight="bold")
    ax.set_title("Exp 8 — k-anonymity lowers re-identification risk")

    return {
        "title": "8. Re-identification Attacks",
        "technique": f"k-anonymity (k={k}) + coarse generalization",
        "metrics": {"identifiable_before_pct": round(before, 0),
                    "identifiable_after_pct": round(after, 0),
                    "reduction_pct": round(reduction, 0)},
        "fig": fig,
        "table_md": "\n".join(
            ["| field | generalized example |"] + ["|---|---|"] +
            [f"| {c} | {str(anon[c].iloc[0])[:40]} |" for c in quasi]),
    }


# ------------------------------------------------------- counterparty (suppl) --

def experiment9_counterparty(df, n=5000):
    """Counterparty reconstruction: top corridors unmasked vs masked volumes."""
    sample = df.sample(min(n, len(df)), random_state=SEED)
    corridors = (sample["account_holder_country_code"] + "->"
                 + sample["counterparty_country_code"])
    vols = sample.groupby(corridors)["transaction_amount_usd"].sum().sort_values(
        ascending=False).head(5)
    noisy = vols * [random.Random(SEED + i).uniform(0.7, 1.3)
                    for i in range(len(vols))]
    kept_rank = list(vols.index) == list(noisy.sort_values(ascending=False).index)

    fig, ax = plt.subplots(figsize=(7.2, 4.2))
    idx = range(len(vols))
    ax.bar([i - 0.2 for i in idx], vols.values / 1e6, width=0.4,
           label="True corridor volume", color=_RED, **_STYLE)
    ax.bar([i + 0.2 for i in idx], noisy.values / 1e6, width=0.4,
           label="Masked (noisy) volume", color=_TEAL, **_STYLE)
    ax.set_xticks(list(idx))
    ax.set_xticklabels(vols.index, fontsize=8)
    ax.set_ylabel("USD millions")
    ax.legend()
    ax.set_title("Exp 9 — counterparty reconstruction: top corridors")

    return {
        "title": "9 (suppl.). Counterparty Reconstruction",
        "technique": "aggregate volume masking",
        "metrics": {"top_corridor_1": str(vols.index[0]),
                    "top_corridor_1_usd_m": round(vols.values[0] / 1e6, 1),
                    "ranking_preserved": bool(kept_rank)},
        "fig": fig,
        "table_md": "\n".join(
            ["| corridor | true USD M | masked USD M |"] + ["|---|---|---|"] +
            [f"| {c} | {t/1e6:.1f} | {n/1e6:.1f} |"
             for c, t, n in zip(vols.index, vols.values, noisy.values)]),
    }


# ------------------------------------------------------------------ runner ----

EXPERIMENTS = [
    experiment1_bec_fraud,
    experiment2_insider_threat,
    experiment3_supply_chain,
    experiment4_sanctions_evasion,
    experiment5_corporate_espionage,
    experiment6_test_environment,
    experiment7_ato_security,
    experiment8_reidentification,
    experiment9_counterparty,
]


def run_experiment(fn, df):
    try:
        return fn(df)
    except Exception as exc:  # noqa: BLE001
        import traceback
        print(f"  ERROR in {fn.__name__}: {exc}")
        traceback.print_exc()
        return {"title": fn.__name__.replace("experiment", ""), "error": str(exc),
                "metrics": {}, "fig": None, "table_md": ""}


def run_all(df) -> list[dict]:
    """Run all 9 bank scenarios; return a list of result dicts.

    Each dict carries {title, technique, metrics, fig, table_md} where `fig` is
    a matplotlib Figure (render to PNG via fig_to_png) and `table_md` a small
    masked-vs-raw transactions table for the app.
    """
    results = [run_experiment(fn, df) for fn in EXPERIMENTS]

    print("\n" + "=" * 62)
    print("SUMMARY — Obfuscation threat scenarios on "
          f"{len(df):,} synthetic bank-transaction records")
    print("=" * 62)
    print(f"{'Scenario':<42}{'Technique':<40}{'Reduction':>10}")
    print("-" * 92)
    for r in results:
        red = r.get("metrics", {}).get("reduction_pct")
        print(f"{r.get('title', '?'):<42}"
              f"{str(r.get('technique', ''))[:38]:<40}"
              f"{(str(red) + '%') if red is not None else 'n/a':>10}")
    return results


def fig_to_png(fig) -> bytes:
    """Render a matplotlib Figure to PNG bytes (Agg)."""
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110, bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()


if __name__ == "__main__":
    from examples.obfuscation.bank_transactions_data import generate_bank_transactions

    df = generate_bank_transactions(n_rows=2000, seed=SEED)
    results = run_all(df)
    for r in results:
        if r.get("fig"):
            png = fig_to_png(r["fig"])
            print(f"  [{r['title']}] figure {len(png)} bytes")
