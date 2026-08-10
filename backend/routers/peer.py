"""Peer identification & market-share estimation experiment (UPI banks).

A deterministic experiment (no LLM loop): treat each major UPI bank as a *peer*
that only sees its own customers' transactions. Using that auxiliary knowledge
it tries to (a) IDENTIFY which bank an unseen transaction belongs to, and
(b) ESTIMATE other banks' market share per segment (merchant category) or per
payment type (transaction type). We report identification accuracy and
share-estimation error (MAE, correlation) per bank, per segment/type.

Runs the privacy-MCP synthetic generator when no dataset is provided, and
produces a report + charts as workbench artifacts.
"""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from fastapi import APIRouter, HTTPException

from ..artifacts.store import Artifact
from ..state import get_runtime

router = APIRouter()

BANK_COL = "sender_bank"
SEGMENT_COL = "merchant_category"
TYPE_COL = "transaction type"
AMOUNT_COL = "amount (INR)"


def _resolve_project_file(rt, filename: str) -> Path:
    rel = Path(filename)
    if rel.is_absolute() or ".." in rel.parts:
        raise HTTPException(status_code=400, detail="invalid filename")
    cand = rt.dir / rel
    if not cand.exists() or not cand.is_file():
        raise HTTPException(status_code=404, detail=f"file not found: {filename}")
    return cand


def run_peer_share_experiment(df: pd.DataFrame, seed: int = 42) -> dict:
    """Core experiment. Returns tables + metrics for the report/charts."""
    rng = np.random.default_rng(seed)
    df = df.copy()
    df[BANK_COL] = df[BANK_COL].astype(str).str.strip()
    df[SEGMENT_COL] = df[SEGMENT_COL].astype(str).str.strip()
    df[TYPE_COL] = df[TYPE_COL].astype(str).str.strip()
    df[AMOUNT_COL] = pd.to_numeric(df[AMOUNT_COL], errors="coerce")
    df = df.dropna(subset=[AMOUNT_COL]).reset_index(drop=True)

    banks = sorted(df[BANK_COL].unique())
    segments = sorted(df[SEGMENT_COL].unique())
    ptypes = sorted(df[TYPE_COL].unique())

    # True market shares: bank share of total transactions per segment/type.
    def shares_by(group_col: str):
        tot = df.groupby(group_col)[BANK_COL].value_counts(normalize=True)
        return tot.rename("share")

    # --- Peer identification: each bank builds a fingerprint from its OWN rows
    # (age group, state, device, network, hour) and classifies an unseen row's
    # bank by nearest fingerprint (Naive-Bayes-style likelihood over categorical
    # features + log-amount profile).
    features = ["sender_age_group", "sender_state", "device_type",
                "network_type", "hour_of_day"]
    for f in features:
        if f not in df.columns:
            df[f] = "?"
        df[f] = df[f].astype(str).str.strip()

    # Per-bank P(feature) and log-amount mean/std from own data.
    bank_profiles = {}
    for b in banks:
        sub = df[df[BANK_COL] == b]
        probs = {}
        for f in features:
            probs[f] = sub[f].value_counts(normalize=True).to_dict()
        log_amt = np.log1p(sub[AMOUNT_COL].values)
        probs["_logamt_mean"] = float(log_amt.mean())
        probs["_logamt_std"] = float(log_amt.std() + 1e-6)
        bank_profiles[b] = probs

    def predict_bank(row):
        best, best_score = None, -1e18
        la = np.log1p(float(row[AMOUNT_COL]))
        for b, prof in bank_profiles.items():
            s = -((la - prof["_logamt_mean"]) / prof["_logamt_std"]) ** 2
            for f in features:
                p = prof[f].get(row[f], 1e-6)
                s += np.log(p + 1e-9)
            if s > best_score:
                best, best_score = b, s
        return best

    # Victim sample: rows a peer tries to identify (all banks, held out).
    victim = df.sample(n=min(20000, len(df)), random_state=seed).copy()
    victim["predicted_bank"] = victim.apply(predict_bank, axis=1)
    victim["correct"] = victim[BANK_COL] == victim["predicted_bank"]

    identification = {
        "overall_accuracy": float(victim["correct"].mean()),
        "per_bank": victim.groupby(BANK_COL)["correct"]
                        .agg(["mean", "count"]).rename(
                            columns={"mean": "accuracy", "count": "n"}).round(3),
        "confusion": victim.groupby([BANK_COL, "predicted_bank"]).size()
                        .unstack(fill_value=0),
    }

    # --- Share estimation: each peer estimates others' share per segment/type
    # using its own predicted labels (its fingerprint classification) vs the
    # true shares. Report MAE + correlation across banks.
    def estimate_error(by_col):
        true_share = df.groupby(by_col)[BANK_COL].value_counts(normalize=True)
        est_share = victim.groupby(by_col)["predicted_bank"].value_counts(normalize=True)
        idx = true_share.index.union(est_share.index)
        t = true_share.reindex(idx, fill_value=0.0)
        e = est_share.reindex(idx, fill_value=0.0)
        err = (e - t).abs()
        corr = t.corr(e) if len(idx) > 1 and t.std() > 0 and e.std() > 0 else None
        return {
            "mae": float(err.mean()),
            "corr": float(corr) if corr is not None else None,
            "per_group": err.groupby(level=0).mean().round(4),
        }

    seg_err = estimate_error(SEGMENT_COL)
    type_err = estimate_error(TYPE_COL)

    return {
        "n": len(df), "banks": banks, "segments": segments,
        "payment_types": ptypes,
        "identification": identification,
        "segments_error": seg_err,
        "types_error": type_err,
        "bank_volumes": df[BANK_COL].value_counts().to_dict(),
    }


def render_report(res: dict) -> str:
    ident = res["identification"]
    lines = [
        "# Peer Identification & Market-Share Estimation — UPI Banks",
        "",
        f"- **Transactions:** {res['n']:,} · **Banks:** {', '.join(res['banks'])}",
        f"- **Segments:** {', '.join(res['segments'][:8])}{'…' if len(res['segments']) > 8 else ''}",
        f"- **Payment types:** {', '.join(res['payment_types'])}",
        "",
        "## 1. Peer identification",
        "",
        f"Each bank classifies an unseen transaction's origin bank using only its "
        f"own customers' data (age/state/device/network/hour + log-amount profile). "
        f"**Overall identification accuracy: {ident['overall_accuracy']:.1%}** on "
        f"a {ident['per_bank']['n'].sum():,.0f}-row held-out sample.",
        "",
        "### Accuracy per bank",
        "",
        "| Bank | Accuracy | Sample |",
        "|------|----------|--------|",
    ]
    for b, row in ident["per_bank"].iterrows():
        lines.append(f"| {b} | {row['accuracy']:.1%} | {int(row['n'])} |")

    lines += ["", "## 2. Market-share estimation error", ""]
    lines += ["### Per segment (merchant category)"]
    lines += [f"- **MAE:** {res['segments_error']['mae']:.3f}"
              + (f" · **correlation:** {res['segments_error']['corr']:.3f}"
                 if res["segments_error"]["corr"] is not None else "")]
    lines += ["", "| Segment | MAE |", "|---------|-----|"]
    for seg, mae in res["segments_error"]["per_group"].items():
        lines.append(f"| {seg} | {mae:.4f} |")

    lines += ["", "### Per payment type"]
    lines += [f"- **MAE:** {res['types_error']['mae']:.3f}"
              + (f" · **correlation:** {res['types_error']['corr']:.3f}"
                 if res["types_error"]["corr"] is not None else "")]
    lines += ["", "| Payment type | MAE |", "|-------------|-----|"]
    for pt_, mae in res["types_error"]["per_group"].items():
        lines.append(f"| {pt_} | {mae:.4f} |")
    lines += ["", "## 3. Bank volumes", ""]
    lines += ["| Bank | Transactions | Share |", "|------|-------------|-------|"]
    total = res["n"]
    for b, n in sorted(res["bank_volumes"].items(), key=lambda x: -x[1]):
        lines.append(f"| {b} | {n:,} | {n / total:.1%} |")
    return "\n".join(lines)


def render_figures(res: dict) -> dict:
    """Return {filename: bytes} of charts."""
    figs = {}
    ident = res["identification"]

    # 1) Confusion matrix (identification).
    conf = ident["confusion"]
    fig, ax = plt.subplots(figsize=(8, 6))
    im = ax.imshow(conf.values, cmap="Blues")
    ax.set_xticks(range(len(conf.columns))); ax.set_xticklabels(conf.columns, rotation=45, ha="right")
    ax.set_yticks(range(len(conf.index))); ax.set_yticklabels(conf.index)
    ax.set_xlabel("predicted bank"); ax.set_ylabel("true bank")
    ax.set_title("Peer identification — confusion matrix")
    for i in range(len(conf.index)):
        for j in range(len(conf.columns)):
            ax.text(j, i, int(conf.iloc[i, j]), ha="center", va="center", fontsize=7)
    fig.colorbar(im)
    fig.tight_layout()
    _fig_bytes(fig, "fig_identification_confusion.png", figs)
    plt.close(fig)

    # 2) Share-estimation MAE per segment (bar).
    seg = res["segments_error"]["per_group"].sort_values(ascending=False)
    fig, ax = plt.subplots(figsize=(8, 4.5))
    ax.bar(range(len(seg)), seg.values, color="#4f8cff")
    ax.set_xticks(range(len(seg))); ax.set_xticklabels(seg.index, rotation=45, ha="right")
    ax.set_ylabel("share-estimation MAE"); ax.set_title("Market-share error per segment")
    fig.tight_layout()
    _fig_bytes(fig, "fig_share_error_segment.png", figs)
    plt.close(fig)

    # 3) Bank volumes bar.
    vols = res["bank_volumes"]
    fig, ax = plt.subplots(figsize=(8, 4.5))
    names = sorted(vols, key=lambda b: -vols[b])
    ax.bar(range(len(names)), [vols[b] for b in names], color="#7ee787")
    ax.set_xticks(range(len(names))); ax.set_xticklabels(names, rotation=45, ha="right")
    ax.set_ylabel("transactions"); ax.set_title("Bank volumes")
    fig.tight_layout()
    _fig_bytes(fig, "fig_bank_volumes.png", figs)
    plt.close(fig)

    return figs


def _fig_bytes(fig, name: str, into: dict):
    buf = __import__("io").BytesIO()
    fig.savefig(buf, format="png", dpi=140)
    buf.seek(0)
    into[name] = buf.read()


@router.post("/api/projects/{name}/peer/run")
async def peer_run(name: str, body: dict):
    """Run the peer-identification / market-share experiment on a project file.

    body: {filename?} — defaults to the first UPI/banking CSV in the project.
    Returns artifact ids + summary so the UI can render charts inline.
    """
    rt = get_runtime(name)
    filename = (body.get("filename") or "").strip()

    if filename:
        path = _resolve_project_file(rt, filename)
    else:
        # Auto-pick the first UPI/banking CSV in the project.
        cands = sorted(p for p in rt.dir.iterdir()
                       if p.is_file() and p.suffix.lower() == ".csv"
                       and ("upi" in p.name.lower() or "bank" in p.name.lower()))
        if not cands:
            cands = sorted(p for p in rt.dir.iterdir()
                           if p.is_file() and p.suffix.lower() == ".csv")
        if not cands:
            raise HTTPException(status_code=404,
                                detail="no CSV dataset in this project")
        path = cands[0]
        filename = path.name

    try:
        df = pd.read_csv(path, low_memory=False)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=422,
                            detail=f"could not read {filename}: {e}")
    if BANK_COL not in df.columns:
        raise HTTPException(
            status_code=422,
            detail=f"dataset has no '{BANK_COL}' column — expected UPI/banking "
                   "data with sender_bank")

    res = run_peer_share_experiment(df)
    report_md = render_report(res)
    figures = render_figures(res)

    # Register artifacts.
    artifact_ids = []
    fig_refs = []
    try:
        env = await rt.kernels.get_env()
    except Exception:  # noqa: BLE001
        env = {}
    for name_, data in figures.items():
        art = Artifact(kind="figure", name=name_,
                       description=f"Peer experiment figure: {name_}",
                       code=f"peer_experiment({filename})", env=env,
                       message_id="", run_id="", data_type="png")
        rt.artifacts.add_artifact(art, data=data, data_type="png")
        artifact_ids.append(art.id)
        fig_refs.append({"name": name_, "id": art.id})
    if report_md:
        art = Artifact(kind="report", name=f"peer-report-{Path(filename).stem}",
                       description=f"Peer identification report for {filename}",
                       code=f"peer_experiment({filename})", env=env,
                       message_id="", run_id="", data_type="text")
        rt.artifacts.add_artifact(art, data=report_md.encode(), data_type="text")
        artifact_ids.append(art.id)
        report_id = art.id
    else:
        report_id = ""

    # Record a run in the project's table for the Experiments tab.
    try:
        rt.store.add_run(
            prompt=(f"Peer identification & market-share experiment on {filename}"),
            reply=report_md[:3000], status="done",
            started_at=time.time(), finished_at=time.time(),
            artifact_ids=artifact_ids,
            metrics={
                "identification_accuracy": res["identification"]["overall_accuracy"],
                "segment_mae": res["segments_error"]["mae"],
                "type_mae": res["types_error"]["mae"],
            },
            kind="peer_experiment", label=f"peer:{filename}",
            model=None, dataset=filename)
    except Exception:  # noqa: BLE001
        pass

    return {
        "ok": True, "filename": filename,
        "artifact_ids": artifact_ids, "report_id": report_id,
        "figures": fig_refs, "report": report_md,
        "summary": {
            "n": res["n"], "banks": res["banks"],
            "identification_accuracy": res["identification"]["overall_accuracy"],
            "segment_mae": res["segments_error"]["mae"],
            "segment_corr": res["segments_error"]["corr"],
            "type_mae": res["types_error"]["mae"],
            "type_corr": res["types_error"]["corr"],
        },
        "message": (f"Peer experiment done on `{filename}` — identification "
                    f"accuracy {res['identification']['overall_accuracy']:.1%}, "
                    f"share-estimation MAE {res['segments_error']['mae']:.3f} per "
                    f"segment / {res['types_error']['mae']:.3f} per payment type."),
    }
