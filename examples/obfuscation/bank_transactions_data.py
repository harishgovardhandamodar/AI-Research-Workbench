"""Synthetic bank-transaction data generator (pandas edition).

Generates realistic-looking bank-account transfer records with the same field
shape as the original obfuscation study dataset: IBANs (mod-97 valid), sort
codes + account numbers, SWIFT/BIC codes, account holders, counterparties,
cities, countries, currencies, amounts, running balances and transfer
metadata. 100% synthetic, fixed-seed reproducible, no real PII.

    from examples.obfuscation.bank_transactions_data import generate_bank_transactions
    df = generate_bank_transactions(n_rows=2000, seed=42)
"""

from __future__ import annotations

import datetime
import hashlib
import random

# ------------------------------------------------------------------ constants --

FIELDNAMES = [
    "transaction_id", "transaction_datetime_utc", "transaction_date",
    "account_holder_name", "account_number", "iban", "swift_bic",
    "bank_name", "branch_code", "account_holder_country_code",
    "account_holder_city", "account_holder_address",
    "counterparty_name", "counterparty_iban", "counterparty_bic",
    "counterparty_country_code", "counterparty_city",
    "currency", "fx_rate_to_usd", "transaction_amount",
    "transaction_amount_usd", "balance_after", "transaction_type",
    "transaction_status", "purpose_code", "remittance_info",
    "reference_number", "channel",
]

# Sensitive / quasi-identifier columns used by the experiments.
SENSITIVE_COLUMNS = [
    "account_holder_name", "account_number", "iban", "swift_bic",
    "bank_name", "branch_code",
    "account_holder_city", "account_holder_address",
    "counterparty_name", "counterparty_iban", "counterparty_bic",
    "counterparty_city",
    "transaction_amount", "transaction_amount_usd", "balance_after",
]
QUASI_IDENTIFIER_COLUMNS = [
    "transaction_date", "account_holder_city", "counterparty_city",
    "transaction_amount_usd",
]

_COUNTRIES = [
    ("GB", "United Kingdom"), ("DE", "Germany"), ("FR", "France"),
    ("NL", "Netherlands"), ("BE", "Belgium"), ("CH", "Switzerland"),
    ("ES", "Spain"), ("IT", "Italy"), ("SE", "Sweden"), ("PL", "Poland"),
    ("US", "United States"), ("CA", "Canada"), ("SG", "Singapore"),
    ("AU", "Australia"), ("JP", "Japan"), ("AE", "UAE"), ("SA", "Saudi Arabia"),
    ("IN", "India"), ("BR", "Brazil"), ("ZA", "South Africa"),
]
_CTRY_CODES = [c for c, _ in _COUNTRIES]
_WEIGHTS = [9.0, 8.0, 6.5, 4.0, 3.0, 3.0, 4.0, 3.5, 2.0, 2.5,
            10.0, 3.0, 3.0, 2.5, 2.5, 2.5, 1.5, 4.0, 2.0, 1.5]

_CURRENCIES = {
    "GB": ["GBP", "EUR", "USD"], "DE": ["EUR", "USD"], "FR": ["EUR", "USD"],
    "NL": ["EUR"], "BE": ["EUR"], "CH": ["CHF", "EUR", "USD"],
    "ES": ["EUR"], "IT": ["EUR"], "SE": ["SEK", "EUR"], "PL": ["PLN", "EUR"],
    "US": ["USD", "EUR", "GBP"], "CA": ["CAD", "USD"], "SG": ["SGD", "USD"],
    "AU": ["AUD", "USD"], "JP": ["JPY", "USD"], "AE": ["AED", "USD"],
    "SA": ["SAR", "USD"], "IN": ["INR", "USD"], "BR": ["BRL", "USD"],
    "ZA": ["ZAR", "USD"],
}

_CITIES = {
    "GB": ["London", "Manchester", "Birmingham", "Leeds", "Liverpool",
           "Edinburgh", "Glasgow", "Cardiff"],
    "DE": ["Berlin", "Munich", "Hamburg", "Frankfurt", "Cologne",
           "Stuttgart", "Dusseldorf", "Leipzig"],
    "FR": ["Paris", "Lyon", "Marseille", "Toulouse", "Nice",
           "Strasbourg", "Bordeaux", "Lille"],
    "US": ["New York", "Los Angeles", "Chicago", "Houston", "Miami",
           "San Francisco", "Dallas", "Boston"],
    "IN": ["Mumbai", "Delhi", "Bangalore", "Chennai", "Hyderabad",
           "Kolkata", "Pune", "Ahmedabad"],
    "NL": ["Amsterdam", "Rotterdam", "The Hague", "Utrecht"],
    "CH": ["Zurich", "Geneva", "Basel", "Lausanne", "Bern"],
}
_ALL_CITIES = sorted({c for lst in _CITIES.values() for c in lst})

_FIRST_NAMES = ["Ava", "Noah", "Olivia", "Liam", "Emma", "Ethan", "Mia",
                "Lucas", "Isabella", "Mason", "Sophia", "James", "Amelia",
                "Benjamin", "Charlotte", "Henry", "Harper", "Daniel",
                "Evelyn", "Samuel", "Grace", "Alexander", "Chloe"]
_LAST_NAMES = ["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia",
               "Miller", "Davis", "Rodriguez", "Martinez", "Hernandez",
               "Lopez", "Gonzalez", "Wilson", "Anderson", "Thomas",
               "Taylor", "Moore", "Jackson", "Martin", "Lee", "Perez"]

_BANKS = {
    "GB": ["Barclays Bank PLC", "HSBC Bank PLC", "NatWest", "Lloyds Bank",
           "Santander UK"],
    "DE": ["Deutsche Bank", "Commerzbank", "KfW", "DZ Bank", "UniCredit Bank"],
    "FR": ["BNP Paribas", "Credit Agricole", "Societe Generale", "BPCE"],
    "US": ["Chase", "Bank of America", "Wells Fargo", "Citibank"],
    "IN": ["State Bank of India", "HDFC Bank", "ICICI Bank", "Axis Bank"],
    "NL": ["ING Bank", "Rabobank", "ABN AMRO", "Triodos Bank"],
    "CH": ["UBS", "Credit Suisse", "ZKB", "Julius Baer"],
}
_FALLBACK_BANKS = ("Global Bank,Metro Bank,Union Bank,Continental Bank,"
                   "Atlantic Bank,Meridian Bank").split(",")

_TRANSACTION_TYPES = ["WIRE", "SEPA", "ACH", "INTERNAL_TRANSFER",
                      "DEPOSIT", "WITHDRAWAL", "FX_CONVERSION", "FEE",
                      "DIRECT_DEBIT", "STANDING_ORDER"]
_STATUSES = ["SETTLED", "PENDING", "REJECTED", "FLAGGED"]
_CHANNELS = ["ONLINE", "BRANCH", "MOBILE", "ATM", "CORPORATE"]
_PURPOSES = ["PAY FOR SERVICES", "SUPPLIER INVOICE", "SALARY PAYMENT",
             "TRADE SETTLEMENT", "LOAN REPAYMENT", "DIVIDEND",
             "INVESTMENT FUNDING", "GOODS PURCHASE", "ROYALTY",
             "INSURANCE PREMIUM"]
_REMITTANCE = ["INV-", "SAL-", "ORD-", "SET-", "TAX-"]

_ALPH = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_DISTRICTS = ["Downtown", "Midtown", "Uptown", "Westside", "East End",
              "Northgate", "Riverside", "Harbor"]
_STREET_TYPES = ["Street", "Avenue", "Boulevard", "Road", "Drive", "Lane",
                 "Place"]

# Per-country IBAN layouts: (bban_len, banks_len, branch_len, account_len).
# Total IBAN length == 4 (country + check) + bban_len.
_IBAN_LAYOUTS = {
    "GB": (18, 4, 6, 8),
    "DE": (18, 8, 0, 10),
    "FR": (23, 5, 5, 11),
    "NL": (14, 4, 0, 10),
    "BE": (12, 3, 0, 7),
    "CH": (17, 5, 0, 12),
    "ES": (20, 4, 4, 10),
    "IT": (23, 5, 5, 11),
    "SE": (20, 3, 0, 17),
    "PL": (24, 8, 0, 16),
}


# ---------------------------------------------------------------- low-level ----

def _pick_country(rng: random.Random) -> str:
    return rng.choices(_CTRY_CODES, weights=_WEIGHTS, k=1)[0]


def _sample_amount(rng: random.Random) -> float:
    """Transfer sizes: mostly everyday, occasionally large corporate wires."""
    if rng.random() < 0.02:
        lo = rng.uniform(1e5, 5e6)
    elif rng.random() < 0.10:
        lo = rng.uniform(1e4, 1e5)
    else:
        lo = rng.lognormvariate(6.2, 1.7)
    return min(rng.uniform(5, max(lo, 5)), 1e8)


def _iban_mod97(iban: str) -> int:
    """mod-97 of an IBAN string (letters -> 10..35)."""
    rearranged = iban[4:] + iban[:4]
    digits = "".join(str(ord(ch) - 55) if ch.isalpha() else ch
                     for ch in rearranged)
    return int(digits) % 97


def _iban_checksum(bban: str, country: str) -> str:
    """Two-digit IBAN check digits for `bban` under `country`."""
    check = 98 - _iban_mod97(country + "00" + bban)
    return "%02d" % (check % 97)


def _generate_iban(country: str, rng: random.Random) -> str:
    """Generate a mod-97-valid IBAN for `country` (or empty if unsupported)."""
    layout = _IBAN_LAYOUTS.get(country)
    if layout is None:
        return ""
    bban_len, bank_len, branch_len, acct_len = layout
    if country == "GB":
        bban = ("%04d" % rng.randint(1000, 9999)
                + "%06d" % rng.randint(100000, 999999)
                + "%08d" % rng.randint(10000000, 99999999))
    else:
        bban = ("" .join(str(rng.randint(0, 9)) for _ in range(bank_len))
                + ("" .join(str(rng.randint(0, 9))
                            for _ in range(branch_len)) if branch_len else "")
                + ("" .join(str(rng.randint(0, 9))
                            for _ in range(acct_len)) if acct_len else ""))
        if len(bban) < bban_len:
            bban += "".join(rng.choices(_ALPH, k=bban_len - len(bban)))
    check = _iban_checksum(bban, country)
    return country + check + bban


def _generate_bic(rng: random.Random) -> str:
    pfx = "".join(rng.choices("ABCDFGHJKLMNPRSTUVWXY", k=4))
    cc2 = "".join(rng.choices(_ALPH, k=2))
    loc = "".join(rng.choices("ABCDEFGHJKLMNPRSTUVWXY", k=2))
    return (pfx + cc2 + loc)[:8]


def _account_number(country: str, rng: random.Random) -> str:
    if country == "GB":
        return "%02d-%02d-%02d %08d" % (rng.randint(10, 99),
                                        rng.randint(10, 99),
                                        rng.randint(10, 99),
                                        rng.randint(10000000, 99999999))
    if country == "US":
        return "US%02d%06d %09d" % (rng.randint(10, 99),
                                    rng.randint(100000, 999999),
                                    rng.randint(10000000, 999999999))
    return "".join(str(rng.randint(0, 9)) for _ in range(12))


def _random_address(rng: random.Random) -> str:
    num = rng.randint(10, 4999)
    unit = rng.choice(["Apt ", "Suite ", "", "Floor "])
    return "%d %s %s%s" % (num, rng.choice(_DISTRICTS), unit,
                           rng.choice(_STREET_TYPES))


# --------------------------------------------------------------- public API ----

def generate_bank_transactions(n_rows: int = 2000, seed: int = 42,
                               year: int = 2025) -> "pd.DataFrame":
    """Generate `n_rows` synthetic bank-transaction records.

    Deterministic for a given (n_rows, seed, year). Returns a pandas DataFrame
    with the full bank-transfer field set (see FIELDNAMES).
    """
    import pandas as pd

    rng = random.Random(seed)
    base_date = datetime.date(year=year, month=1, day=1)
    rows = []
    for idx in range(n_rows):
        acct_c = _pick_country(rng)
        for _ in range(50):
            cp_c = _pick_country(rng)
            if cp_c != acct_c:
                break

        banks_c = _BANKS.get(acct_c, _FALLBACK_BANKS)

        tx_date = base_date + datetime.timedelta(days=rng.randint(0, 364))
        hh = rng.choices(range(8, 22), weights=[2, 3, 5, 7, 8, 10, 9, 7, 4, 2,
                                                1, 1, 1, 1], k=1)[0] \
            if rng.random() < 0.85 else rng.randint(0, 23)
        tx_dt = datetime.datetime.combine(tx_date, datetime.time(hh, rng.randint(0, 59),
                                                                 rng.randint(0, 59)))

        amount = _sample_amount(rng)
        fx = round(rng.uniform(0.01, 135.0), 6)
        currency = rng.choice(_CURRENCIES.get(acct_c, ["USD"]))
        status = rng.choices(_STATUSES,
                             weights=[0.78, 0.12, 0.07, 0.03], k=1)[0]
        if status == "FLAGGED" and amount < 5000:
            status = rng.choices(["SETTLED", "PENDING"], weights=[0.9, 0.1],
                                 k=1)[0]

        rows.append({
            "transaction_id": hashlib.sha256(f"{idx}:{hh}".encode()).hexdigest()[:24].upper(),
            "transaction_datetime_utc": tx_dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "transaction_date": tx_date.strftime("%Y-%m-%d"),
            "account_holder_name": "{} {}".format(rng.choice(_FIRST_NAMES),
                                                  rng.choice(_LAST_NAMES)),
            "account_number": _account_number(acct_c, rng),
            "iban": _generate_iban(acct_c, rng),
            "swift_bic": _generate_bic(rng),
            "bank_name": rng.choice(banks_c),
            "branch_code": "%03d%s" % (rng.randint(100, 999),
                                       rng.choice(_ALPH)),
            "account_holder_country_code": acct_c,
            "account_holder_city": rng.choice(_CITIES.get(acct_c, _ALL_CITIES)),
            "account_holder_address": _random_address(rng),
            "counterparty_name": "{} {}".format(rng.choice(_FIRST_NAMES),
                                                rng.choice(_LAST_NAMES)),
            "counterparty_iban": _generate_iban(cp_c, rng),
            "counterparty_bic": _generate_bic(rng),
            "counterparty_country_code": cp_c,
            "counterparty_city": rng.choice(_CITIES.get(cp_c, _ALL_CITIES)),
            "currency": currency,
            "fx_rate_to_usd": fx,
            "transaction_amount": round(amount, 2),
            "transaction_amount_usd": round(amount * (fx if currency != "USD" else 1.0), 2),
            "balance_after": round(rng.uniform(100, 5e5), 2),
            "transaction_type": rng.choice(_TRANSACTION_TYPES),
            "transaction_status": status,
            "purpose_code": rng.choice(_PURPOSES),
            "remittance_info": "{}{} / {}".format(
                rng.choice(_REMITTANCE), rng.randint(10000, 99999),
                rng.choice(_PURPOSES)),
            "reference_number": "%d-%s%d8-%d" % (
                rng.randint(10, 99), tx_date.strftime("%y%m"), rng.randint(5, 8),
                rng.randint(10000, 99999)),
            "channel": rng.choice(_CHANNELS),
        })
    return pd.DataFrame(rows, columns=FIELDNAMES)


if __name__ == "__main__":
    df = generate_bank_transactions(n_rows=500, seed=42)
    print(f"Generated {len(df):,} synthetic bank-transaction records "
          f"({df.shape[1]} columns)")
    print(df[["account_holder_name", "iban", "swift_bic",
              "transaction_amount_usd"]].head(3).to_string())
