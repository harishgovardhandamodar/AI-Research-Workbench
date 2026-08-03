"""Synthetic SWIFT transaction data generator (pandas edition).

Ported from the obfuscation study (`~/WorkBook/obfuscation-study/data-generators/
generator.py`) so the data-obfuscation experiments are fully self-contained and
reproducible inside the Fox workbench. Generates realistic SWIFT-like network
transaction records (MT103/MT202/MT940 style) with the same field set: IBANs,
BIC codes, bank names, cities, countries, currencies, amounts and payment
metadata. 100% synthetic, fixed-seed reproducible, no real PII.

    from examples.obfuscation.swift_data import generate_swift
    df = generate_swift(n_rows=2000, seed=42)
"""

from __future__ import annotations

import datetime
import hashlib
import random

# ------------------------------------------------------------------ constants --

FIELDNAMES = [
    "transaction_id", "message_type", "booking_datetime_utc", "value_date",
    "booking_date", "sender_institution_name", "sender_bic_swift_code",
    "sender_iban", "sender_country_code", "sender_city", "sender_address",
    "receiver_bank_name", "receiver_bic_swift_code", "receiver_iban",
    "receiver_country_code", "receiver_city", "receiver_address",
    "currency", "transaction_amount_usd", "fx_rate_to_usd", "amount_in_usd",
    "payment_method", "priority", "charge_bearer", "transaction_fee",
    "segment", "category", "service_type", "reference_number",
    "purpose_code", "remittance_info", "branch_code", "correspondent_bank_bic",
]

# Sensitive / quasi-identifier columns used by the experiments.
SENSITIVE_COLUMNS = [
    "sender_institution_name", "sender_bic_swift_code", "sender_iban",
    "sender_city", "sender_address",
    "receiver_bank_name", "receiver_bic_swift_code", "receiver_iban",
    "receiver_city", "receiver_address",
    "transaction_amount_usd", "amount_in_usd",
]
QUASI_IDENTIFIER_COLUMNS = [
    "booking_date", "value_date", "sender_city", "receiver_city",
    "transaction_amount_usd",
]

_COUNTRIES = [
    ("US", "United States"), ("GB", "United Kingdom"), ("DE", "Germany"),
    ("FR", "France"), ("IN", "India"), ("CN", "China"), ("JP", "Japan"),
    ("AU", "Australia"), ("CA", "Canada"), ("SG", "Singapore"),
    ("CH", "Switzerland"), ("AE", "UAE"), ("BR", "Brazil"), ("MX", "Mexico"),
    ("ZA", "South Africa"), ("NG", "Nigeria"), ("KE", "Kenya"),
    ("EG", "Egypt"), ("SA", "Saudi Arabia"), ("TR", "Turkey"), ("RU", "Russia"),
    ("KR", "South Korea"), ("ID", "Indonesia"), ("TH", "Thailand"),
    ("PH", "Philippines"), ("PK", "Pakistan"), ("BD", "Bangladesh"),
    ("VN", "Vietnam"), ("PL", "Poland"), ("NL", "Netherlands"),
    ("BE", "Belgium"), ("ES", "Spain"), ("IT", "Italy"), ("SE", "Sweden"),
    ("NO", "Norway"),
]
_CTRY_CODES = [c for c, _ in _COUNTRIES]
_WEIGHTS = [9.0, 6.5, 7.0, 6.0, 8.0, 10.0, 5.5, 4.5, 3.5, 2.5,
            3.0, 3.0, 2.5, 1.5, 2.0, 1.5, 1.0, 1.0, 1.5, 1.0,
            1.5, 2.5, 1.5, 1.0, 1.0, 1.0, 0.8, 0.8, 2.0, 3.0,
            2.5, 2.5, 2.0, 1.0, 0.8]

_CURRENCIES = {
    "US": ["USD"], "GB": ["GBP", "EUR", "USD"], "DE": ["EUR"], "FR": ["EUR"],
    "IN": ["INR", "USD", "EUR"], "CN": ["CNY", "USD"], "JP": ["JPY", "USD"],
    "AU": ["AUD", "USD"], "CA": ["CAD", "USD"], "SG": ["SGD", "USD", "EUR"],
    "CH": ["CHF", "USD", "EUR"], "AE": ["AED", "USD"], "BR": ["BRL", "USD"],
    "MX": ["MXN", "USD"], "ZA": ["ZAR", "USD"], "NG": ["NGN", "USD"],
    "KE": ["KES", "USD"], "EG": ["EGP", "USD"], "SA": ["SAR", "USD"],
    "TR": ["TRY", "USD"], "RU": ["RUB", "USD"], "KR": ["KRW", "USD"],
    "ID": ["IDR", "USD"], "TH": ["THB", "USD"], "PH": ["PHP", "USD"],
    "PK": ["PKR", "USD"], "BD": ["BDT", "USD"], "VN": ["VND", "USD"],
    "PL": ["PLN", "EUR"], "NL": ["EUR"], "BE": ["EUR"], "ES": ["EUR"],
    "IT": ["EUR"], "SE": ["SEK", "EUR"], "NO": ["NOK", "EUR"],
}

_CITIES = {
    "US": ["New York", "Los Angeles", "Chicago", "Houston", "Miami",
           "San Francisco", "Dallas", "Boston", "Seattle", "Denver"],
    "GB": ["London", "Manchester", "Birmingham", "Leeds", "Liverpool",
           "Edinburgh", "Glasgow", "Cardiff"],
    "DE": ["Berlin", "Munich", "Hamburg", "Frankfurt", "Cologne",
           "Stuttgart", "Dusseldorf", "Leipzig"],
    "FR": ["Paris", "Lyon", "Marseille", "Toulouse", "Nice",
           "Strasbourg", "Bordeaux", "Lille"],
    "IN": ["Mumbai", "Delhi", "Bangalore", "Chennai", "Hyderabad",
           "Kolkata", "Pune", "Ahmedabad"],
    "CN": ["Beijing", "Shanghai", "Guangzhou", "Shenzhen", "Hangzhou",
           "Chengdu", "Wuhan", "Tianjin"],
    "JP": ["Tokyo", "Osaka", "Yokohama", "Nagoya", "Sapporo",
           "Kyoto", "Kobe", "Hiroshima"],
    "AU": ["Sydney", "Melbourne", "Brisbane", "Perth",
           "Adelaide", "Canberra", "Darwin", "Newcastle"],
}
_ALL_CITIES = sorted({c for lst in _CITIES.values() for c in lst})

_BANK_NAMES = {
    "US": ["First National Bank", "Global Trust Bank", "Liberty Savings and Loan",
           "Meridian Financial Group", "Summit National Bank", "Apex Commercial Bank",
           "Evergreen Capital Bank", "NorthStar Federal Bank", "Horizon Trust Bank"],
    "GB": ["Royal Mercantile Bank", "Thames Valley Bank", "Crown Imperial Bank",
           "Union National Bank", "Highland Financial Bank",
           "Westminster Banking Corp", "Sovereign Capital Bank",
           "Sterling Federal Bank"],
    "DE": ["Deutsche Bundesbank AG", "Munich Commercial Bank", "Hamburg Trust Bank",
           "Frankfurt Finance Corp", "Berlin National Bank", "Rhine Valley Bank AG",
           "Saxony Federal Bank", "Nuremberg Capital Bank"],
    "FR": ["Banque de Lyon", "Paris Credit Bank", "Marseille Trust Bank",
           "Versailles National Bank", "Bordeaux Capital Bank",
           "Strasbourg Euro Bank", "Lille Regional Bank"],
}
_FALLBACK_BANKS = ("First National Bank,Pacific Commerce Bank,Atlantic Credit Bank,"
                   "Redwood Savings Bank,Cascade Financial Corp,"
                   "Frontier Banking Group,Evergreen Capital,NorthStar Federal Bank"
                   ).split(",")

_CATEGORIES = ["Customer Transfer", "Financial Institution Transfer",
               "Trade Payment", "Investment Order", "Loan Funding",
               "Remittance", "Settlement Message"]
_SEGMENTS = ["Corporate Treasury", "Retail Banking", "Trade Finance",
             "Foreign Exchange", "Capital Markets", "Payments",
             "Treasury Operations", "Cash Management"]
_PURPOSES = ["PAY FOR SERVICES", "SUPPLIER INVOICE", "SALARY PAYMENT",
             "TRADE SETTLEMENT", "LOAN REPAYMENT", "DIVIDEND",
             "INVESTMENT FUNDING", "GOODS PURCHASE", "ROYALTY",
             "INSURANCE PREMIUM"]
_PAYMENT_METHODS = ["SWIFT WIRE", "REAL TIME", "ACH NETWORK", "CHEQUE CLEARED",
                    "FAST PAYMENT", "RTGS"]
_SERVICE_TYPES = ["Regular Credit", "Debit Payment", "Cheque Issue & Reply",
                  "Online Transfer", "Real-time Gross Settlement",
                  "Standing Order", "Foreign Exchange Remittance"]
_MESSAGE_TYPES = ["MT103", "MT202", "MT940", "MT942", "MT760", "MT202COVO", "MT950"]
_DISTRICTS = ["Downtown", "Midtown", "Uptown", "Westside", "East End",
              "Northgate", "Riverside", "Harbor"]
_STREET_TYPES = ["Street", "Avenue", "Boulevard", "Road", "Drive", "Lane", "Place"]
_ALPH = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


# ---------------------------------------------------------------- low-level ----

def _pick_country(rng: random.Random) -> str:
    return rng.choices(_CTRY_CODES, weights=_WEIGHTS, k=1)[0]


def _sample_amount(rng: random.Random) -> float:
    if rng.random() < 0.01:
        lo = rng.uniform(1e7, 3e9)
    else:
        lo = rng.lognormvariate(9.5, 1.8)
    return min(rng.uniform(100, max(lo, 100)), 5e9)


def _generate_bic(rng: random.Random) -> str:
    pfx = "".join(rng.choices("ABCDFGHJKLMNPRSTUVWXY", k=4))
    bc = "%03d" % rng.randint(100, 999)
    cc2 = "".join(rng.choices(_ALPH, k=2))
    post = "".join(rng.choices("ABCDEF", k=2))
    return pfx[:2] + bc[:2] + post + cc2.upper()[-2:]


def _generate_iban(country_code: str, idx: int, rng: random.Random) -> str:
    """Country-flavoured IBAN/account number (structurally plausible, fake)."""
    if country_code == "US":
        routing = "%02d%04d" % (rng.randint(10, 99), rng.randint(1, 9999))
        return routing + "/" + "%07d" % ((idx + 10000) % 10000000)
    if country_code == "GB":
        bban = "".join(rng.choices(_ALPH, k=4)).upper()
        sort_n = "%02d%02d" % (rng.randint(10, 99), rng.randint(10, 99))
        return "GB" + bban + sort_n + "%06d" % ((idx + 999) % 10 ** 6)
    if country_code == "DE":
        bank_id = "%04d%08d" % (rng.randint(10, 99), rng.randint(0, 99999999))
        return "DE" + bank_id + "%018d" % ((idx + 1000) * 7 % 10 ** 18)
    if country_code == "FR":
        bank = "%05d%02d" % (rng.randint(10000, 99999), rng.randint(10, 99))
        branch = "%05d" % rng.randint(10000, 99999)
        return "FR" + bank + branch + "%011d%02d" % ((idx + 100) * 37 % 10 ** 11,
                                                     rng.randint(0, 99))
    if country_code == "NL":
        return "NL" + "%04d%08d" % (rng.randint(1000, 9999),
                                    rng.randint(10 ** 7, 10 ** 8 - 1))
    if country_code == "BE":
        return "BE" + "%03d%02d" % (rng.randint(100, 999), rng.randint(80, 99)) \
               + "%03d" % ((idx + 100) % 10 ** 6)
    if country_code == "IN":
        pan = rng.choice(_ALPH) + str(rng.randint(100, 999)) + rng.choice(_ALPH) \
              + str(rng.randint(100, 999))
        return "INDPANC" + pan + "/" + str(idx % 1000 + 1)
    if country_code == "AU":
        bsb = "%d%02d%04d" % (rng.randint(1, 6), rng.randint(10, 99),
                              rng.randint(1000, 9999))
        return bsb + "A" + "%05d" % ((idx + 100) % 10 ** 5)
    if country_code == "CN":
        return "CC%d%s" % (rng.randint(10, 99), str(idx + 10000)[-8:])
    if country_code == "JP":
        return "JP" + "%03d" % rng.randint(100, 999) + "B" + "%07d" % ((idx + 4267) % 10 ** 7)
    # Generic IBAN-style fallback (with checksum digits).
    bbn = "%06d%012d" % (rng.randint(1, 99), idx + 123456)
    return country_code.upper() + "00" + bbn[-16:]


def _random_address(rng: random.Random) -> str:
    num = rng.randint(10, 4999)
    unit = rng.choice(["Apt ", "Suite ", "", "Floor "])
    addr = "%d %s %s%s" % (num, rng.choice(_DISTRICTS), unit,
                           rng.choice(_STREET_TYPES))
    return addr


# --------------------------------------------------------------- public API ----

def generate_swift(n_rows: int = 2000, seed: int = 42, year: int = 2025) -> "pd.DataFrame":
    """Generate `n_rows` synthetic SWIFT transaction records.

    Deterministic for a given (n_rows, seed, year). Returns a pandas DataFrame
    with the full SWIFT field set (see FIELDNAMES).
    """
    import pandas as pd

    rng = random.Random(seed)
    base_date = datetime.date(year=year, month=1, day=1)
    rows = []
    for idx in range(n_rows):
        cc_s = _pick_country(rng)
        for _ in range(50):
            cc_d = _pick_country(rng)
            if cc_d != cc_s:
                break

        banks_s = _BANK_NAMES.get(cc_s, _FALLBACK_BANKS)
        banks_d = _BANK_NAMES.get(cc_d, _FALLBACK_BANKS)

        tx_date = base_date + datetime.timedelta(days=rng.randint(0, 364))
        hh = rng.choices(range(8, 18), weights=[2, 3, 5, 7, 8, 10, 9, 7, 4, 2], k=1)[0] \
            if rng.random() < 0.85 else rng.randint(0, 23)
        tx_dt = datetime.datetime.combine(tx_date, datetime.time(hh, rng.randint(0, 59),
                                                                 rng.randint(0, 59)))
        vd = tx_date + datetime.timedelta(days=rng.randint(1, 3)) \
            if rng.random() < 0.3 else tx_date

        amount = _sample_amount(rng)
        fx = round(rng.uniform(0.5, 135.0), 6)
        currency = rng.choice(_CURRENCIES.get(cc_s, ["USD"]))

        rows.append({
            "transaction_id": hashlib.sha256(f"{idx}:{hh}".encode()).hexdigest()[:24].upper(),
            "message_type": rng.choice(_MESSAGE_TYPES),
            "booking_datetime_utc": tx_dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "value_date": vd.strftime("%Y-%m-%d"),
            "booking_date": tx_date.strftime("%Y-%m-%d"),
            "sender_institution_name": rng.choice(banks_s),
            "sender_bic_swift_code": _generate_bic(rng),
            "sender_iban": _generate_iban(cc_s, idx, rng),
            "sender_country_code": cc_s,
            "sender_city": rng.choice(_CITIES.get(cc_s, _ALL_CITIES)),
            "sender_address": _random_address(rng),
            "receiver_bank_name": rng.choice(banks_d),
            "receiver_bic_swift_code": _generate_bic(rng),
            "receiver_iban": _generate_iban(cc_d, idx + 10000, rng),
            "receiver_country_code": cc_d,
            "receiver_city": rng.choice(_CITIES.get(cc_d, _ALL_CITIES)),
            "receiver_address": _random_address(rng),
            "currency": currency,
            "transaction_amount_usd": round(amount, 2),
            "fx_rate_to_usd": fx,
            "amount_in_usd": round(amount * (fx if currency != "USD" else 1.0), 2),
            "payment_method": rng.choice(_PAYMENT_METHODS),
            "priority": rng.choice(["NORMAL", "URGENCY", "PRIORITY"]),
            "charge_bearer": rng.choice(["SHA", "OUR", "BEN"]),
            "transaction_fee": round(rng.uniform(2.5, 85.0), 2),
            "segment": rng.choice(_SEGMENTS),
            "category": rng.choice(_CATEGORIES),
            "service_type": rng.choice(_SERVICE_TYPES)[:45],
            "reference_number": "%d-%s%d8-%d" % (
                rng.randint(10, 99), tx_date.strftime("%y%m"), rng.randint(5, 8),
                rng.randint(10000, 99999)),
            "purpose_code": rng.choice(_PURPOSES),
            "remittance_info": "INV-%d / %s" % (rng.randint(10000, 99999),
                                                rng.choice(_PURPOSES)),
            "branch_code": "%d%s%d" % (idx % 1000, rng.choice(_ALPH),
                                       rng.randint(100, 999))[:7],
            "correspondent_bank_bic": _generate_bic(rng) if rng.random() < 0.4 else "",
        })
    return pd.DataFrame(rows, columns=FIELDNAMES)


if __name__ == "__main__":
    df = generate_swift(n_rows=500, seed=42)
    print(f"Generated {len(df):,} synthetic SWIFT records "
          f"({df.shape[1]} columns)")
    print(df.head(3).to_string())
