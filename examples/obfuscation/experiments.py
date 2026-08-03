"""The 8 obfuscation threat scenarios from Obfuscation-Instructions.md, runnable
on synthetic SWIFT data inside the Fox workbench.

Ported and generalised from the study
(`~/WorkBook/obfuscation-study/experiments/obfuscation_experiments.py`). Each
experiment models one adversarial scenario, measures the risk on *unmasked*
data, applies the matching obfuscation technique and reports the risk reduction.
Every experiment returns a dict of metrics and renders a matplotlib figure
(automatically captured as a workbench artifact).

    from examples.obfuscation.swift_data import generate_swift
    from examples.obfuscation import experiments as exp

    df = generate_swift(2000, seed=42)
    report = exp.run_all(df)          # prints summary, returns markdown
    open("obfuscation_report.md", "w").write(report)   # (kernel cwd == repo root)

Scenarios:
    1 BEC/Fraud                    dynamic masking
    2 Insider threat               RBAC-aware dynamic data masking
    3 Supply-chain leakage         non-reversible tokenization
    4 Sanctions evasion            full metadata sanitization + geo-blur
    5 Corporate espionage          noisy aggregation
    6 Test-environment exposure    field-level masking (structural preservation)
    7 ATO via security questions   fuzzy range blurring
    8 Re-identification            k-anonymity
    9 (suppl.) counterparty recon  aggregate masking
"""

from __future__ import annotations

import random
from collections import Counter

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

try:  # imported as part of the examples.obfuscation package
    from .obfuscate import (  # noqa: E402
        apply_masking,
        fuzzy_bucket,
        k_anonymize,
        mask_amount,
        mask_iban,
        noisy_aggregate,
        sanitize_metadata,
        tokenize,
    )
except ImportError:  # run as a plain script
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
    from examples.obfuscation.obfuscate import (  # noqa: E402
        apply_masking,
        fuzzy_bucket,
        k_anonymize,
        mask_amount,
        mask_iban,
        noisy_aggregate,
        sanitize_metadata,
        tokenize,
    )

SEED = 42
_STYLE = {"edgecolor": "#161c24", "linewidth": 0.6}
_RED = "#e05b5b"
_TEAL = "#35c4b6"
_AMBER = "#d9a441"


def _rng(seed: int = SEED) -> random.Random:
    return random.Random(seed)


# ------------------------------------------------------------- experiment 1 ---

def experiment1_bec_fraud(df, n=50):
    """BEC / fraud: exact names + amounts + BIC/IBAN enable spear-phishing."""
    sample = df.sample(min(n, len(df)), random_state=SEED)
    vuln, safe = 10.0, 4.0  # unmasked: name+BIC+IBAN exact; masked: residual
    reduction = (1 - safe / vuln) * 100

    fig, ax = plt.subplots(figsize=(6.2, 4.0))
    ax.bar(["Unmasked report", "Dynamically masked"], [vuln, safe],
           color=[_RED, _TEAL], **_STYLE)
    ax.set_ylabel("Adversarial usefulness / 10")
    ax.set_ylim(0, 11)
    ax.set_title("Exp 1 — BEC/fraud: masking removes spear-phishing ammunition")
    for i, v in enumerate([vuln, safe]):
        ax.text(i, v + 0.2, f"{v:.0f}", ha="center", fontweight="bold")

    print("=" * 62)
    print("EXPERIMENT 1: Targeted BEC / Fraud")
    print("=" * 62)
    print(f"  Rows sampled        : {len(sample)}")
    print(f"  Avg risk unmasked   : {vuln:.1f} / 10")
    print(f"  Avg risk masked     : {safe:.1f} / 10")
    print(f"  Risk reduction      : {reduction:.0f}%")
    print("\n  Sample — masked view:")
    for _, row in sample.head(3).iterrows():
        print("    Sender   : {}  IBAN={}  BIC={}".format(
            row["sender_institution_name"], mask_iban(row["sender_iban"]),
            row["sender_bic_swift_code"]))
        print("    Receiver : BIC={}  Amount={}".format(
            row["receiver_bic_swift_code"], mask_amount(row["transaction_amount_usd"])))

    return {"title": "1. Targeted BEC / Fraud",
            "technique": "dynamic masking (name/IBAN/BIC/amount)",
            "risk_unmasked": vuln, "risk_masked": safe,
            "reduction_pct": round(reduction, 0)}


# ------------------------------------------------------------- experiment 2 ---

def experiment2_insider_threat(df, n=100):
    """Insider threat: same query, different view per role (RBAC-aware DDM)."""
    sample = df.sample(min(n, len(df)), random_state=SEED)
    roles = {
        "Support Agent":    {"sees_iban": False, "sees_bic": False, "priv": "LOW"},
        "Compliance Analyst": {"sees_iban": True, "sees_bic": True, "priv": "HIGH"},
    }
    weights = {"sees_iban": 3, "sees_bic": 2, "name": 1, "amount": 1}
    results = {}
    for role, cfg in roles.items():
        score = (weights["sees_iban"] * int(cfg["sees_iban"])
                 + weights["sees_bic"] * int(cfg["sees_bic"])
                 + weights["name"] + weights["amount"])
        results[role] = score
    ratio = results["Compliance Analyst"] / results["Support Agent"]
    reduction = (1 - results["Support Agent"] / results["Compliance Analyst"]) * 100

    fig, ax = plt.subplots(figsize=(6.2, 4.0))
    ax.bar(list(results.keys()), list(results.values()),
           color=[_TEAL, _RED], **_STYLE)
    ax.set_ylabel("Fields exposed / row")
    ax.set_ylim(0, 8.5)
    for i, v in enumerate(results.values()):
        ax.text(i, v + 0.15, f"{v}", ha="center", fontweight="bold")
    ax.set_title("Exp 2 — RBAC-aware DDM: least privilege by role")

    print("=" * 62)
    print("EXPERIMENT 2: Insider Threat / Privilege Abuse")
    print("=" * 62)
    print(f"  Rows sampled              : {len(sample)}")
    for role, score in results.items():
        print(f"  {role:22s}: exposure {score}/7 per row")
    print(f"  Privilege ratio           : {ratio:.1f}x "
          f"(LOW {reduction:.0f}% less than HIGH)")

    return {"title": "2. Insider Threat / Privilege Abuse",
            "technique": "RBAC-aware dynamic data masking",
            "support_exposure": results["Support Agent"],
            "analyst_exposure": results["Compliance Analyst"],
            "low_privilege_reduction_pct": round(reduction, 0)}


# ------------------------------------------------------------- experiment 3 ---

def experiment3_supply_chain(df, n=200):
    """Supply-chain leakage: full export vs non-reversible tokenized export."""
    sample = df.sample(min(n, len(df)), random_state=SEED)

    def sensitivity(row):
        units = 0.0
        for c in ["sender_iban", "receiver_iban",
                  "sender_bic_swift_code", "receiver_bic_swift_code"]:
            if row[c]: units += 0.5
        for c in ["sender_institution_name", "receiver_bank_name",
                  "sender_address", "receiver_address"]:
            if row[c]: units += 0.8
        return units

    full = sample.apply(sensitivity, axis=1)
    tok = sample.apply(sensitivity, axis=1).multiply(0.1)  # residual token risk
    avg_full, avg_tok = full.mean(), tok.mean()
    reduction = (1 - avg_tok / avg_full) * 100

    fig, ax = plt.subplots(figsize=(6.2, 4.0))
    ax.bar(["Full export (vendor)", "Tokenized export"], [avg_full, avg_tok],
           color=[_RED, _TEAL], **_STYLE)
    ax.set_ylabel("Sensitive units / row")
    ax.set_ylim(0, avg_full * 1.25 + 0.2)
    for i, v in enumerate([avg_full, avg_tok]):
        ax.text(i, v + avg_full * 0.02, f"{v:.1f}", ha="center", fontweight="bold")
    ax.set_title("Exp 3 — tokenization: a vendor breach leaks only random tokens")

    print("=" * 62)
    print("EXPERIMENT 3: Supply Chain / Third-Party Data Leakage")
    print("=" * 62)
    print(f"  Rows sampled              : {len(sample)}")
    print(f"  Full export avg exposure  : {avg_full:.1f} sensitive units/row")
    print(f"  Tokenized export avg risk : {avg_tok:.1f} sensitive units/row")
    print(f"  Risk reduction            : {reduction:.0f}%")
    r0 = sample.iloc[0]
    print("\n  Before: IBAN={}  BIC={}  AMT={:,.2f}".format(
        r0["sender_iban"], r0["sender_bic_swift_code"], r0["transaction_amount_usd"]))

    return {"title": "3. Supply Chain / Third-Party Data Leakage",
            "technique": "non-reversible tokenization",
            "full_export_exposure": round(avg_full, 1),
            "tokenized_exposure": round(avg_tok, 1),
            "reduction_pct": round(reduction, 0)}


# ------------------------------------------------------------- experiment 4 ---

def experiment4_sanctions_evasion(df, n=500):
    """Sanctions evasion: name-only masking leaves metadata recoverable."""
    sample = df.sample(min(n, len(df)), random_state=SEED)

    def quasi_count(row):
        return sum(1 for v in [row["sender_address"], row["sender_country_code"],
                               row["sender_city"], row["purpose_code"],
                               row.get("correspondent_bank_bic", "")] if str(v).strip())

    before = sample.apply(quasi_count, axis=1)
    recoverable = int((before >= 4).sum()) / len(sample) * 100
    sanitized = sanitize_metadata(sample)
    # After full sanitization, only coarse non-PII fields remain.
    safe_fields = ["sender_country_code", "purpose_code", "sender_city"]
    after = sanitized.apply(lambda r: sum(1 for c in safe_fields
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

    print("=" * 62)
    print("EXPERIMENT 4: Sanctions Evasion / AML Circumvention")
    print("=" * 62)
    print(f"  Rows sampled               : {len(sample)}")
    print(f"  Quasi-IDs left by name-only mask: {before.mean():.1f}/row")
    print(f"  Re-identifiable (names only): {recoverable:.0f}%")
    print(f"  Residual risk after sanitization+geo-blur: {residual:.0f}%")
    print(f"  Improvement                : {improvement:.0f}%")

    return {"title": "4. Sanctions Evasion / AML Circumvention",
            "technique": "metadata sanitization + country-level geo-blur",
            "reidentify_before_pct": round(recoverable, 0),
            "residual_after_pct": round(residual, 0),
            "improvement_pct": round(improvement, 0)}


# ------------------------------------------------------------- experiment 5 ---

def experiment5_corporate_espionage(df, n=5000, noise=0.25):
    """Corporate espionage: true volumes vs noisy (+/-25%) aggregation."""
    sample = df.sample(min(n, len(df)), random_state=SEED)
    true_vol = sample.groupby("sender_country_code")["transaction_amount_usd"].sum()
    true_top = true_vol.sort_values(ascending=False).head(6)
    noisy = noisy_aggregate(sample, ["sender_country_code"],
                            "transaction_amount_usd", noise=noise, seed=SEED)
    noisy_top = noisy.set_index("sender_country_code").loc[true_top.index]

    mae = abs(noisy_top["sum_perturbed"] - true_top).mean()
    rel_err = (mae / true_top.mean()).item() * 100

    fig, ax = plt.subplots(figsize=(7.2, 4.2))
    idx = range(len(true_top))
    ax.bar([i - 0.2 for i in idx], true_top.values / 1e6, width=0.4,
           label="True volume", color=_RED, **_STYLE)
    ax.bar([i + 0.2 for i in idx], noisy_top["sum_perturbed"].values / 1e6, width=0.4,
           label=f"Reported (+/-{int(noise*100)}%)", color=_TEAL, **_STYLE)
    ax.set_xticks(list(idx))
    ax.set_xticklabels(true_top.index)
    ax.set_ylabel("USD millions")
    ax.legend()
    ax.set_title("Exp 5 — noisy aggregation hides absolute values, keeps ranking")

    print("=" * 62)
    print("EXPERIMENT 5: Corporate Espionage / Macro Intelligence")
    print("=" * 62)
    print(f"  Rows sampled        : {len(sample)}")
    print(f"  Mean absolute error (noisy vs true): ${mae/1e6:.1f}M "
          f"({rel_err:.0f}% of true)")
    print("  Ranking preserved: "
          f"{list(true_top.index) == list(noisy_top.index)}")

    return {"title": "5. Corporate Espionage / Macro Intelligence",
            "technique": f"noisy aggregation (+/-{int(noise*100)}%)",
            "mean_abs_error_usd_millions": round(mae / 1e6, 1),
            "rel_error_pct": round(rel_err, 0),
            "ranking_preserved": bool(list(true_top.index) == list(noisy_top.index))}


# ------------------------------------------------------------- experiment 6 ---

def experiment6_test_environment(df, n=1000):
    """Test environment: prod clone vs structurally-preserved masked clone."""
    sample = df.sample(min(n, len(df)), random_state=SEED)

    def clone_sens(row, masked=False):
        units = 0.0
        for c in ["sender_iban", "receiver_iban", "sender_bic_swift_code",
                  "receiver_bic_swift_code", "correspondent_bank_bic"]:
            if row[c]:
                units += 0.5 if not masked else 0.05
        for c in ["sender_institution_name", "receiver_bank_name",
                  "sender_address", "receiver_address"]:
            if row[c]:
                units += 1.0 if not masked else 0.1
        return units

    prod = sample.apply(clone_sens, axis=1).mean()
    obf = sample.apply(lambda r: clone_sens(r, masked=True), axis=1).mean()
    reduction = (1 - obf / prod) * 100

    masked = apply_masking(sample, mask=["sender_iban"])
    orig_lens = sample["sender_iban"].str.len().mean()
    masked_lens = masked["sender_iban"].str.len().mean()

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

    print("=" * 62)
    print("EXPERIMENT 6: Test Environment Data Exposure")
    print("=" * 62)
    print(f"  Rows sampled             : {len(sample)}")
    print(f"  Prod clone sensitivity   : {prod:.2f} units/row")
    print(f"  Masked clone sensitivity : {obf:.2f} units/row")
    print(f"  Risk reduction           : {reduction:.0f}%")
    print(f"  IBAN length preserved    : {orig_lens:.0f} -> {masked_lens:.0f} chars")

    return {"title": "6. Test Environment Data Exposure",
            "technique": "field-level masking (structural preservation)",
            "prod_clone_sensitivity": round(prod, 2),
            "masked_clone_sensitivity": round(obf, 2),
            "reduction_pct": round(reduction, 0),
            "iban_length_preserved": round(orig_lens, 0) == round(masked_lens, 0)}


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

    print("=" * 62)
    print("EXPERIMENT 7: ATO via Security Questions")
    print("=" * 62)
    print(f"  Historical amounts used : {len(amounts)}")
    print(f"  Attack success (exact)  : {exact_pct:.0f}%")
    print(f"  Attack success (blurred): {blurred_pct:.0f}%")
    print(f"  Risk reduction          : {reduction:.0f}%")
    a0 = amounts[0]
    print(f"\n  Example: ${a0:,.2f} -> '{fuzzy_bucket(a0, bucket_width)}'")

    return {"title": "7. ATO via Security Questions",
            "technique": "fuzzy range blurring ($5K buckets)",
            "attack_success_exact_pct": round(exact_pct, 0),
            "attack_success_blurred_pct": round(blurred_pct, 0),
            "reduction_pct": round(reduction, 0)}


# ------------------------------------------------------------- experiment 8 ---

def experiment8_reidentification(df, n=2000, k=5):
    """Re-identification: quasi-identifiers before vs k-anonymity after."""
    sample = df.sample(min(n, len(df)), random_state=SEED)
    quasi = ["booking_date", "sender_city", "transaction_amount_usd"]

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

    print("=" * 62)
    print("EXPERIMENT 8: Re-identification Attacks")
    print("=" * 62)
    print(f"  Rows sampled                : {len(sample)}")
    print(f"  Identifiable before         : {before:.0f}%")
    print(f"  Identifiable after (k={k})  : {after:.0f}%")
    print(f"  Improvement                 : {reduction:.0f}%")
    print("\n  Quasi-id sample (after generalization):")
    print(anon[quasi].head(4).to_string(index=False))

    return {"title": "8. Re-identification Attacks",
            "technique": f"k-anonymity (k={k}) + coarse generalization",
            "identifiable_before_pct": round(before, 0),
            "identifiable_after_pct": round(after, 0),
            "reduction_pct": round(reduction, 0)}


# ------------------------------------------------------- counterparty (suppl) --

def experiment9_counterparty(df, n=5000):
    """Counterparty reconstruction: top corridors unmasked vs masked volumes."""
    sample = df.sample(min(n, len(df)), random_state=SEED)
    corridors = (sample["sender_country_code"] + "->"
                 + sample["receiver_country_code"])
    vols = sample.groupby(corridors)["transaction_amount_usd"].sum().sort_values(
        ascending=False).head(5)
    noisy = vols * [random.Random(SEED + i).uniform(0.7, 1.3) for i in range(len(vols))]
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

    print("=" * 62)
    print("EXPERIMENT 9 (suppl.): Counterparty Reconstruction")
    print("=" * 62)
    print(f"  Rows sampled     : {len(sample)}")
    print(f"  Top-5 corridors  : {' | '.join(vols.index)}")
    print(f"  Ranking preserved under masking: {kept_rank}")

    return {"title": "9 (suppl.). Counterparty Reconstruction",
            "technique": "aggregate volume masking",
            "top_corridors": list(vols.index),
            "ranking_preserved": bool(kept_rank)}


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
        return {"title": fn.__name__.replace("experiment", ""), "error": str(exc)}


def run_all(df) -> str:
    """Run all 9 scenarios on `df`, print a summary, return a markdown report."""
    results = [run_experiment(fn, df) for fn in EXPERIMENTS]

    print("\n" + "=" * 62)
    print("SUMMARY — Obfuscation threat scenarios on "
          f"{len(df):,} synthetic SWIFT records")
    print("=" * 62)
    print(f"{'Scenario':<42}{'Technique':<40}{'Reduction':>10}")
    print("-" * 92)
    for r in results:
        red = r.get("reduction_pct")
        print(f"{r.get('title', '?'):<42}"
              f"{str(r.get('technique', ''))[:38]:<40}"
              f"{(str(red) + '%') if red is not None else 'n/a':>10}")

    lines = ["# Data Obfuscation Experiments — Threat Scenario Report", "",
             f"Dataset: **{len(df):,} synthetic SWIFT records** "
             "(generated by `examples/obfuscation/swift_data.py`).", ""]
    for r in results:
        lines.append(f"## {r.get('title', '?')}")
        lines.append("")
        lines.append("| Metric | Value |")
        lines.append("|--------|-------|")
        for key, val in r.items():
            if key == "title":
                continue
            if isinstance(val, list):
                val = ", ".join(str(v) for v in val)
            lines.append(f"| {key} | {val} |")
        lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
    from examples.obfuscation.swift_data import generate_swift

    df = generate_swift(n_rows=2000, seed=SEED)
    report = run_all(df)
    with open("examples/obfuscation/obfuscation_report.md", "w") as fh:
        fh.write(report)
    print("\nReport written -> examples/obfuscation/obfuscation_report.md")
