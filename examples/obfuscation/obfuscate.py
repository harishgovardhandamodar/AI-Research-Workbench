"""Reusable data-obfuscation library for experimenting with sensitive data.

Provides every technique from the SWIFT obfuscation study
(`~/WorkBook/obfuscation-study`) as functions that operate on pandas DataFrames,
so you can apply them interactively to generated or uploaded data inside the Fox
kernel. All functions are deterministic (fixed `seed` where randomness is used).

Techniques (from Summary-Obfuscation-Study.md):
  - Field-level masking   -> apply_masking
  - Tokenization          -> tokenize
  - Fuzzy range blurring  -> fuzzy_bucket / apply_fuzzy_buckets
  - Noisy aggregation     -> noisy_aggregate
  - K-anonymity           -> k_anonymize
  - Metadata sanitization -> sanitize_metadata
  - High-level entry      -> obfuscate_dataframe

Example:
    from examples.obfuscation.swift_data import generate_swift
    from examples.obfuscation import obfuscate as obf

    df = generate_swift(1000)
    masked = obf.apply_masking(df, mask=["sender_iban", "sender_bic_swift_code"])
    tokenized = ob.tokenize(df, columns=["sender_iban", "receiver_iban"])
    anon = ob.k_anonymize(df, quasi_columns=["booking_date", "sender_city",
                                             "transaction_amount_usd"], k=5)
"""

from __future__ import annotations

import hashlib
import random
from typing import Iterable

import pandas as pd

# Fields whose values are highly sensitive and must be masked/tokenized.
HIGH_SENSITIVITY = ["sender_iban", "receiver_iban",
                    "sender_bic_swift_code", "receiver_bic_swift_code",
                    "correspondent_bank_bic"]
MEDIUM_SENSITIVITY = ["sender_institution_name", "receiver_bank_name",
                      "sender_address", "receiver_address",
                      "sender_city", "receiver_city"]

# Country -> continent/region (used for geo-blurring).
_REGIONS = {
    "US": "Americas", "CA": "Americas", "MX": "Americas", "BR": "Americas",
    "GB": "Europe", "DE": "Europe", "FR": "Europe", "CH": "Europe",
    "NL": "Europe", "BE": "Europe", "ES": "Europe", "IT": "Europe",
    "SE": "Europe", "NO": "Europe", "PL": "Europe", "RU": "Europe",
    "TR": "Europe", "IN": "Asia", "CN": "Asia", "JP": "Asia", "KR": "Asia",
    "SG": "Asia", "TH": "Asia", "ID": "Asia", "PH": "Asia", "PK": "Asia",
    "BD": "Asia", "VN": "Asia", "AE": "Middle East", "SA": "Middle East",
    "EG": "Africa", "NG": "Africa", "KE": "Africa", "ZA": "Africa",
    "AU": "Oceania",
}
_DEFAULT_REGION = "Unknown"


# ------------------------------------------------------------- scalar masks ----

def mask_iban(value) -> str:
    """Keep the last 4 characters, hash-stamp the rest. Preserves length."""
    if not value:
        return ""
    s = str(value).strip()
    if len(s) < 6:
        return "X" * len(s)
    visible = s[-4:]
    prefix = "#" * max(4, len(s) - 4)
    return prefix[: len(s) - len(visible)] + visible


def mask_bic(value) -> str:
    """Keep the last 2 characters (country/purpose hint), replace the rest."""
    if not value:
        return ""
    s = str(value).strip()
    if len(s) < 4:
        return "?" * len(s)
    return "?" * (len(s) - 2) + s[-2:]


def mask_name(value) -> str:
    """Blur a multi-word name: first letter of each word + asterisks."""
    if not value:
        return ""
    words = str(value).split()
    return " ".join(w[0] + "*" * max(0, len(w) - 1) for w in words)


def mask_address(value, rng: random.Random | None = None) -> str:
    """Replace a street address with a generic district placeholder."""
    if not value:
        return ""
    rng = rng or random.Random(0)
    districts = ["Downtown", "Midtown", "Uptown", "Westside", "East End",
                 "Northgate", "Riverside", "Harbor"]
    return "{} ** ***".format(rng.choice(districts))


def mask_city(value, rng: random.Random | None = None) -> str:
    """Replace a city with a random large city of the same country (if known)."""
    if not value:
        return ""
    rng = rng or random.Random(0)
    big = {"US": ("New York", "Los Angeles"), "DE": ("Berlin", "Munich"),
           "CN": ("Shanghai", "Beijing"), "IN": ("Mumbai", "Delhi"),
           "GB": ("London", "Manchester"), "FR": ("Paris", "Lyon"),
           "JP": ("Tokyo", "Osaka"), "AU": ("Sydney", "Melbourne")}
    pool = list(big.keys())
    cc = rng.choice(pool)
    return rng.choice(big[cc])


def mask_amount(value, symbol: str = "$") -> str:
    """Round to the nearest 100 and add a trailing *** (destroys precision)."""
    try:
        amt = float(value)
    except (ValueError, TypeError):
        return f"{symbol}0.00***"
    return "{}{:,.0f}***".format(symbol, round(amt / 100) * 100)


def token_id(value, salt: str = "fox-obfuscation", prefix: str = "TKN") -> str:
    """Non-reversible deterministic token (SHA-256 truncated)."""
    h = hashlib.sha256(f"{salt}:{value}".encode()).hexdigest()[:12].upper()
    return f"{prefix}_{h}"


def fuzzy_bucket(value, width: float = 5000.0, symbol: str = "$") -> str:
    """Map an exact amount to a fuzzy range of `width` (e.g. $5K buckets).

    "$847.32" -> "Between $0 and $5,000"
    """
    try:
        amt = float(value)
    except (ValueError, TypeError):
        return "unknown"
    lo = int(amt // width) * width
    lo_s = f"{symbol}{int(lo):,}"
    hi_s = f"{symbol}{int(lo + width):,}"
    return "Between {} and {}".format(lo_s, hi_s)


# ------------------------------------------------------------- dataframe ops ---

_MASKERS = {
    "iban": mask_iban,
    "bic": mask_bic,
    "name": mask_name,
    "address": mask_address,
    "city": mask_city,
    "amount": mask_amount,
}


def apply_masking(df: pd.DataFrame, mask: Iterable[str] | None = None) -> pd.DataFrame:
    """Field-level masking.

    `mask` is an iterable of column names. The masking function is chosen from
    the column name (iban/bic/name/address/city/amount substrings). If `mask` is
    None, all known sensitive columns are masked. Preserves formats/lengths so
    the output still passes structural validation (ideal for test environments).
    """
    out = df.copy()
    cols = list(mask) if mask is not None else list(
        df.columns.intersection([*HIGH_SENSITIVITY, *MEDIUM_SENSITIVITY]))
    for col in cols:
        if col not in df.columns:
            continue
        kind = "amount" if "amount" in col.lower() else \
            ("iban" if "iban" in col.lower() else
             ("bic" if "bic" in col.lower() else
              ("name" if "name" in col.lower() else
               ("address" if "address" in col.lower() else
                ("city" if "city" in col.lower() else "name")))))
        fn = _MASKERS[kind]
        if kind in ("address", "city"):
            rng = random.Random(0)
            out[col] = df[col].apply(lambda v: fn(v, rng))
        else:
            out[col] = df[col].apply(fn)
    return out


def tokenize(df: pd.DataFrame, columns: Iterable[str] | None = None,
             salt: str = "fox-obfuscation", prefix: str = "TKN") -> pd.DataFrame:
    """Non-reversible tokenization of sensitive columns (supply-chain safety).

    Tokens are deterministic for a given (value, salt) so joins stay consistent,
    but no key exists to recover the original value — even for insiders.
    """
    out = df.copy()
    cols = list(columns) if columns is not None else list(
        df.columns.intersection([*HIGH_SENSITIVITY, *MEDIUM_SENSITIVITY]))
    for col in cols:
        if col in df.columns:
            out[col] = df[col].apply(
                lambda v: token_id(v, salt=salt, prefix=prefix) if v else "")
    return out


def _generalize_amount(value, width: float = 1e6) -> str:
    try:
        amt = float(value)
    except (ValueError, TypeError):
        return "unknown"
    lo = int(amt // width) * width
    return f"{int(lo):,}-{int(lo + width):,}"


def _generalize_date(value, level: str = "month") -> str:
    s = str(value).strip()
    if len(s) < 7:
        return "unknown"
    if level == "month":
        return s[:7]                    # YYYY-MM
    if level == "year":
        return s[:4]                    # YYYY
    return s                            # day-level (as-is)


def _generalize_city(value, country_value=None) -> str:
    if country_value and str(country_value).strip():
        return str(country_value).strip().upper()
    return "XX"


def k_anonymize(df: pd.DataFrame, quasi_columns: Iterable[str],
                k: int = 5, amount_width: float = 1e6,
                date_level: str = "month", seed: int | None = 7) -> pd.DataFrame:
    """K-anonymity via coarse-grained generalization.

    Generalizes each quasi-identifier so that every combination appears in at
    least `k` records:
      - date columns  -> month/year buckets
      - amount columns -> `amount_width` buckets
      - city columns  -> country/region level
    Returns the generalized frame plus the fraction of rows still in
    equivalence classes smaller than k (the re-identification risk).
    """
    out = df.copy()
    for col in quasi_columns:
        if col not in df.columns:
            continue
        low = col.lower()
        if "date" in low:
            out[col] = df[col].apply(lambda v: _generalize_date(v, date_level))
        elif "amount" in low:
            out[col] = df[col].apply(lambda v: _generalize_amount(v, amount_width))
        elif "city" in low:
            cc_col = col.replace("city", "country_code")
            if cc_col in df.columns:
                out[col] = [str(c or "").strip().upper()
                            for c in df[cc_col].fillna("")]
            else:
                out[col] = "XX"
        else:
            continue
    # Evaluate re-identification risk on the generalized quasi-id columns.
    risk = _low_k_fraction(out, list(quasi_columns), k)
    return out, risk


def _low_k_fraction(df: pd.DataFrame, cols: list[str], k: int) -> float:
    if not cols or len(df) == 0:
        return 0.0
    counts = df.groupby(cols, dropna=False).size()
    in_low_k = int(counts[counts < k].sum())
    return in_low_k / len(df)


def noisy_aggregate(df: pd.DataFrame, group_cols: Iterable[str],
                    value_col: str, noise: float = 0.25,
                    seed: int | None = 42) -> pd.DataFrame:
    """Aggregate with +/-`noise` multiplicative perturbation per group.

    Preserves relative rankings but destroys exact absolute values (defence
    against corporate espionage). Each call with a different `seed` produces a
    different perturbed snapshot.
    """
    agg = df.groupby(list(group_cols))[value_col].agg(["sum", "count"]).reset_index()
    rng = random.Random(seed)
    factor = lambda: rng.uniform(1 - noise, 1 + noise)  # noqa: E731
    agg["sum_perturbed"] = agg["sum"] * [factor() for _ in range(len(agg))]
    return agg


def sanitize_metadata(df: pd.DataFrame) -> pd.DataFrame:
    """Full metadata sanitization + country-level geo-blur (AML/sanctions shield).

    Removes names/addresses entirely, replaces BICs with masked placeholders,
    blurs cities to their region, keeps only coarse non-PII fields.
    """
    out = df.copy()
    drop_cols = [c for c in ["sender_address", "receiver_address"] if c in out.columns]
    out = out.drop(columns=drop_cols)
    for col in ["sender_institution_name", "receiver_bank_name"]:
        if col in out.columns:
            out[col] = "REDACTED"
    for col in ["sender_bic_swift_code", "receiver_bic_swift_code",
                "correspondent_bank_bic"]:
        if col in out.columns:
            out[col] = out[col].apply(mask_bic)
    for city, cc in [("sender_city", "sender_country_code"),
                     ("receiver_city", "receiver_country_code")]:
        if city in out.columns:
            out[city] = [_REGIONS.get(str(c or "").strip().upper(), _DEFAULT_REGION)
                         for c in out[cc].fillna("")]
    return out


def obfuscate_dataframe(df: pd.DataFrame, techniques: dict) -> pd.DataFrame:
    """High-level entry point to apply a stack of techniques.

    `techniques` keys:
      "mask"   : list of columns (or True for all sensitive) -> apply_masking
      "tokenize": list of columns (or True for all sensitive) -> tokenize
      "fuzzy"  : list of amount columns -> exact amounts become $5K buckets
      "sanitize": True -> full metadata sanitization + geo-blur
      "k_anonymize": {"quasi_columns": [...], "k": 5, ...} -> k-anonymity
      "noisy"  : {"group_cols": [...], "value_col": "..."} -> returns df + agg
    Returns the (possibly generalized) DataFrame.
    """
    out = df.copy()
    sens = [*HIGH_SENSITIVITY, *MEDIUM_SENSITIVITY]

    if techniques.get("sanitize"):
        out = sanitize_metadata(out)

    if techniques.get("mask"):
        cols = sens if techniques["mask"] is True else techniques["mask"]
        out = apply_masking(out, cols)

    if techniques.get("tokenize"):
        cols = sens if techniques["tokenize"] is True else techniques["tokenize"]
        out = tokenize(out, cols)

    if techniques.get("fuzzy"):
        for col in techniques["fuzzy"]:
            if col in out.columns:
                out[col] = out[col].apply(fuzzy_bucket)

    if techniques.get("k_anonymize"):
        spec = dict(techniques["k_anonymize"])
        quasi = spec.pop("quasi_columns", None)
        if quasi is None:
            from .swift_data import QUASI_IDENTIFIER_COLUMNS
            quasi = QUASI_IDENTIFIER_COLUMNS
        out, _ = k_anonymize(out, quasi, **spec)

    return out


if __name__ == "__main__":
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
    from examples.obfuscation.swift_data import generate_swift

    df = generate_swift(200)
    print("=== original ===")
    print(df[["sender_iban", "sender_bic_swift_code", "sender_institution_name",
              "transaction_amount_usd"]].head(3).to_string())
    print("\n=== masked ===")
    m = apply_masking(df, mask=["sender_iban", "sender_bic_swift_code"])
    print(m[["sender_iban", "sender_bic_swift_code"]].head(3).to_string())
    print("\n=== k-anonymized (k=5) ===")
    anon, risk = k_anonymize(df, ["booking_date", "sender_city",
                                  "transaction_amount_usd"], k=5)
    print("rows in k<5 classes: {:.1%}".format(risk))
    print(anon[["booking_date", "sender_city", "transaction_amount_usd"]].head(3).to_string())
