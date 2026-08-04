"""Synthetic credit-card transaction data generator (pandas edition).

Replaces the original bank-transaction generator with a structurally
analogous credit-card dataset so the data-obfuscation and adversarial
experiments stay fully self-contained and reproducible inside the Fox
workbench. Generates realistic card-transaction records with the same field
shape: PANs (Luhn-valid), issuer BINs, cardholder names, merchant names,
cities, countries, currencies, amounts and payment metadata. 100% synthetic,
fixed-seed reproducible, no real PII.

    from examples.obfuscation.credit_card_data import generate_credit_card
    df = generate_credit_card(n_rows=2000, seed=42)
"""

from __future__ import annotations

import datetime
import hashlib
import random

# ------------------------------------------------------------------ constants --

FIELDNAMES = [
    "transaction_id", "card_type", "transaction_datetime_utc", "settlement_date",
    "transaction_date", "cardholder_name", "card_bin", "card_number",
    "cardholder_country_code", "cardholder_city", "cardholder_address",
    "merchant_name", "acquirer_code", "merchant_account",
    "merchant_country_code", "merchant_city", "merchant_address",
    "currency", "transaction_amount_usd", "fx_rate_to_usd", "amount_in_usd",
    "payment_method", "transaction_status", "charge_type", "transaction_fee",
    "segment", "merchant_category", "transaction_type", "reference_number",
    "purpose_code", "remittance_info", "terminal_code", "acquirer_bic",
]

# Sensitive / quasi-identifier columns used by the experiments.
SENSITIVE_COLUMNS = [
    "cardholder_name", "card_bin", "card_number",
    "cardholder_city", "cardholder_address",
    "merchant_name", "merchant_account", "acquirer_code",
    "merchant_city", "merchant_address",
    "transaction_amount_usd", "amount_in_usd",
]
QUASI_IDENTIFIER_COLUMNS = [
    "transaction_date", "settlement_date", "cardholder_city",
    "merchant_city", "transaction_amount_usd",
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

_FIRST_NAMES = ["Ava", "Noah", "Olivia", "Liam", "Emma", "Ethan", "Mia",
                "Lucas", "Isabella", "Mason", "Sophia", "James", "Amelia",
                "Benjamin", "Charlotte", "Henry", "Harper", "Daniel",
                "Evelyn", "Samuel", "Grace", "Alexander", "Chloe"]
_LAST_NAMES = ["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia",
               "Miller", "Davis", "Rodriguez", "Martinez", "Hernandez",
               "Lopez", "Gonzalez", "Wilson", "Anderson", "Thomas",
               "Taylor", "Moore", "Jackson", "Martin", "Lee", "Perez"]

_MERCHANTS = {
    "US": ["Corner Grill", "Metro Mart", "Grand Retail", "Sunrise Cafe",
           "Harbor Gas & Go", "Cityline Hotel", "Main Street Books",
           "Quick Eats", "Summit Outfitters", "Valley Pharmacy"],
    "GB": ["Rose & Crown Cafe", "Thames Fresh", "West End Boutique",
           "Kings Road Grill", "Oxford Street Market", "Harbour Hotel",
           "Lakeside Pharmacy", "Curry House London"],
    "DE": ["Rathaus Bistro", "Berlin Warenhaus", "Alpenmarkt",
           "Mainwelle Hotel", "Stadtcafe", "Hafen Grill",
           "Schloss Konditorei", "Marktplatz Apotheke"],
    "FR": ["Le Petit Bistro", "Champs Boutique", "Marche de la Gare",
           "Cafe du Port", "Grand Hotel Paris", "Boulangerie Centrale",
           "Lyon Epicerie", "La Fontaine Restaurant"],
}
_FALLBACK_MERCHANTS = ("Global Mart,Corner Cafe,Harbor Grill,City Market,"
                       "Sunrise Coffee,Grand Hotel,Metro Pharmacy,"
                       "Downtown Bistro,Travel Hub,Quick Stop").split(",")

_CATEGORIES = ["Customer Transfer", "Card Payment", "Online Purchase",
               "Recurring Billing", "POS Purchase", "Refund",
               "Cash Advance", "Chargeback"]
_SEGMENTS = ["Consumer Classic", "Consumer Gold", "Consumer Platinum",
             "Business", "Corporate", "Premium", "Student", "Youth"]
_PURPOSES = ["PAY FOR SERVICES", "SUPPLIER INVOICE", "SALARY PAYMENT",
             "TRADE SETTLEMENT", "LOAN REPAYMENT", "DIVIDEND",
             "INVESTMENT FUNDING", "GOODS PURCHASE", "ROYALTY",
             "INSURANCE PREMIUM"]
_PAYMENT_METHODS = ["CHIP", "CONTACTLESS", "SWIPE", "ONLINE",
                    "CARD_NOT_PRESENT", "MANUAL_ENTRY", "RECURRING"]
_STATUSES = ["APPROVED", "DECLINED", "FRAUD_FLAGGED", "REVERSED"]
_CHARGE_TYPES = ["CARDHOLDER", "MERCHANT", "SHARED"]
_SERVICE_TYPES = ["Regular Credit", "Debit Payment", "Cheque Issue & Reply",
                  "Online Transfer", "Real-time Gross Settlement",
                  "Standing Order", "Foreign Exchange Remittance"]
_MCC_CATEGORIES = ["Restaurants", "Groceries", "Retail", "Travel",
                   "Fuel", "E-commerce", "Utilities", "Healthcare",
                   "Entertainment", "Telecom"]
_CARD_TYPES = ["VISA", "MASTERCARD", "AMEX", "DISCOVER"]
_DISTRICTS = ["Downtown", "Midtown", "Uptown", "Westside", "East End",
              "Northgate", "Riverside", "Harbor"]
_STREET_TYPES = ["Street", "Avenue", "Boulevard", "Road", "Drive", "Lane", "Place"]
_ALPH = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


# ---------------------------------------------------------------- low-level ----

def _pick_country(rng: random.Random) -> str:
    return rng.choices(_CTRY_CODES, weights=_WEIGHTS, k=1)[0]


def _sample_amount(rng: random.Random) -> float:
    if rng.random() < 0.01:
        lo = rng.uniform(1e4, 2e5)
    else:
        lo = rng.lognormvariate(6.0, 1.6)
    return min(rng.uniform(5, max(lo, 5)), 5e6)


def _luhn_digit(body: str) -> str:
    """Return the Luhn check digit that makes `body + d` a valid card number."""
    total = 0
    for i, ch in enumerate(reversed(body)):
        d = int(ch)
        if i % 2 == 0:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return str((10 - total % 10) % 10)


def _card_number(card_type: str, rng: random.Random) -> str:
    """Generate a structurally valid (Luhn) PAN for the given network."""
    if card_type == "AMEX":
        length, prefix = 15, rng.choice(["34", "37"])
    elif card_type == "MASTERCARD":
        length = 16
        prefix = rng.choice(["51", "52", "53", "54", "55", "2221", "2226",
                             "2400", "2720"])
    elif card_type == "DISCOVER":
        length = 16
        prefix = rng.choice(["6011", "644", "645", "646", "647", "648", "649",
                             "65"])
    else:  # VISA
        length, prefix = 16, "4"
    body = prefix + "".join(str(rng.randint(0, 9))
                            for _ in range(length - len(prefix) - 1))
    return body + _luhn_digit(body)


def _generate_bic(rng: random.Random) -> str:
    pfx = "".join(rng.choices("ABCDFGHJKLMNPRSTUVWXY", k=4))
    bc = "%03d" % rng.randint(100, 999)
    cc2 = "".join(rng.choices(_ALPH, k=2))
    post = "".join(rng.choices("ABCDEF", k=2))
    return pfx[:2] + bc[:2] + post + cc2.upper()[-2:]


def _random_address(rng: random.Random) -> str:
    num = rng.randint(10, 4999)
    unit = rng.choice(["Apt ", "Suite ", "", "Floor "])
    addr = "%d %s %s%s" % (num, rng.choice(_DISTRICTS), unit,
                           rng.choice(_STREET_TYPES))
    return addr


# --------------------------------------------------------------- public API ----

def generate_credit_card(n_rows: int = 2000, seed: int = 42,
                         year: int = 2025) -> "pd.DataFrame":
    """Generate `n_rows` synthetic credit-card transaction records.

    Deterministic for a given (n_rows, seed, year). Returns a pandas DataFrame
    with the full card-transaction field set (see FIELDNAMES).
    """
    import pandas as pd

    rng = random.Random(seed)
    base_date = datetime.date(year=year, month=1, day=1)
    rows = []
    for idx in range(n_rows):
        cc_c = _pick_country(rng)
        for _ in range(50):
            cc_m = _pick_country(rng)
            if cc_m != cc_c:
                break

        merchants_c = _MERCHANTS.get(cc_c, _FALLBACK_MERCHANTS)
        merchants_m = _MERCHANTS.get(cc_m, _FALLBACK_MERCHANTS)

        tx_date = base_date + datetime.timedelta(days=rng.randint(0, 364))
        hh = rng.choices(range(8, 22), weights=[2, 3, 5, 7, 8, 10, 9, 7, 4, 2,
                                                1, 1, 1, 1], k=1)[0] \
            if rng.random() < 0.85 else rng.randint(0, 23)
        tx_dt = datetime.datetime.combine(tx_date, datetime.time(hh, rng.randint(0, 59),
                                                                 rng.randint(0, 59)))
        settle = tx_date + datetime.timedelta(days=1) \
            if rng.random() < 0.35 else tx_date

        amount = _sample_amount(rng)
        fx = round(rng.uniform(0.5, 135.0), 6)
        currency = rng.choice(_CURRENCIES.get(cc_c, ["USD"]))
        card_type = rng.choice(_CARD_TYPES)
        pan = _card_number(card_type, rng)
        status = rng.choices(_STATUSES,
                             weights=[0.76, 0.07, 0.15, 0.02], k=1)[0]
        if status == "FRAUD_FLAGGED" and amount < 200:
            status = rng.choices(["APPROVED", "DECLINED"], weights=[0.9, 0.1],
                                 k=1)[0]

        rows.append({
            "transaction_id": hashlib.sha256(f"{idx}:{hh}".encode()).hexdigest()[:24].upper(),
            "card_type": card_type,
            "transaction_datetime_utc": tx_dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "settlement_date": settle.strftime("%Y-%m-%d"),
            "transaction_date": tx_date.strftime("%Y-%m-%d"),
            "cardholder_name": "{} {}".format(rng.choice(_FIRST_NAMES),
                                              rng.choice(_LAST_NAMES)),
            "card_bin": pan[:6],
            "card_number": pan,
            "cardholder_country_code": cc_c,
            "cardholder_city": rng.choice(_CITIES.get(cc_c, _ALL_CITIES)),
            "cardholder_address": _random_address(rng),
            "merchant_name": rng.choice(merchants_m),
            "acquirer_code": "ACQ{}{}{}".format(rng.randint(100, 999),
                                                rng.choice(_ALPH),
                                                rng.randint(10, 99))[:8],
            "merchant_account": "%d%s%d" % (idx % 1000, rng.choice(_ALPH),
                                            rng.randint(100, 999))[:7],
            "merchant_country_code": cc_m,
            "merchant_city": rng.choice(_CITIES.get(cc_m, _ALL_CITIES)),
            "merchant_address": _random_address(rng),
            "currency": currency,
            "transaction_amount_usd": round(amount, 2),
            "fx_rate_to_usd": fx,
            "amount_in_usd": round(amount * (fx if currency != "USD" else 1.0), 2),
            "payment_method": rng.choice(_PAYMENT_METHODS),
            "transaction_status": status,
            "charge_type": rng.choice(_CHARGE_TYPES),
            "transaction_fee": round(rng.uniform(0.1, 12.0), 2),
            "segment": rng.choice(_SEGMENTS),
            "merchant_category": rng.choice(_MCC_CATEGORIES),
            "transaction_type": rng.choice(["PURCHASE", "WITHDRAWAL", "REFUND",
                                            "PAYMENT", "REVERSAL"]),
            "reference_number": "%d-%s%d8-%d" % (
                rng.randint(10, 99), tx_date.strftime("%y%m"), rng.randint(5, 8),
                rng.randint(10000, 99999)),
            "purpose_code": rng.choice(_PURPOSES),
            "remittance_info": "INV-%d / %s" % (rng.randint(10000, 99999),
                                                rng.choice(_PURPOSES)),
            "terminal_code": "%d%s%d" % (idx % 1000, rng.choice(_ALPH),
                                         rng.randint(100, 999))[:7],
            "acquirer_bic": _generate_bic(rng) if rng.random() < 0.4 else "",
        })
    return pd.DataFrame(rows, columns=FIELDNAMES)


if __name__ == "__main__":
    df = generate_credit_card(n_rows=500, seed=42)
    print(f"Generated {len(df):,} synthetic credit-card records "
          f"({df.shape[1]} columns)")
    print(df.head(3).to_string())
