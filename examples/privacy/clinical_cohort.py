"""Small clinical cohort for privacy red-teaming examples.

Generates a small (n=200) synthetic clinical cohort with classic
quasi-identifiers (age, sex, ZIP prefix, city, condition, admission date,
visit amount). A cohort this small is a textbook HIGH re-identification risk —
exactly the red-team case in the study guidance.

    from examples.privacy.clinical_cohort import build_cohort
    df = build_cohort(n=200, seed=7)

The CSV is written (optionally) to examples/privacy/clinical_cohort.csv so the
privacy MCP tools (assess_dataframe_privacy / generate_synthetic_tabular) can
read it via a relative path.
"""

from __future__ import annotations

import random
from pathlib import Path

FIELDNAMES = ["patient_id", "age", "sex", "zip_prefix", "city", "condition",
              "admission_date", "visit_amount_usd", "insurance"]

_CONDITIONS = ["Hypertension", "Type 2 Diabetes", "Asthma", "Arthritis",
               "CHD", "Migraine", "Hypothyroidism", "COPD"]
_CITIES = ["Springfield", "Riverside", "Fairview", "Georgetown", "Ashland",
           "Madison", "Clinton", "Franklin"]
_SEX = ["M", "F", "F", "M", "M", "F"]  # mildly imbalanced


def build_cohort(n: int = 200, seed: int = 7) -> "pd.DataFrame":
    """Deterministic small clinical cohort with quasi-identifiers."""
    import pandas as pd
    rng = random.Random(seed)
    rows = []
    for i in range(n):
        admission = "2024-%02d-%02d" % (rng.randint(1, 12), rng.randint(1, 28))
        rows.append({
            "patient_id": f"P{i:04d}",
            "age": rng.randint(18, 92),
            "sex": rng.choice(_SEX),
            "zip_prefix": "%03d" % rng.randint(100, 999),
            "city": rng.choice(_CITIES),
            "condition": rng.choice(_CONDITIONS),
            "admission_date": admission,
            "visit_amount_usd": round(rng.lognormvariate(7.2, 0.9), 2),
            "insurance": rng.choice([True, True, False]),
        })
    return pd.DataFrame(rows, columns=FIELDNAMES)


if __name__ == "__main__":
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

    df = build_cohort(200, seed=7)
    out = Path(__file__).resolve().parent / "clinical_cohort.csv"
    df.to_csv(out, index=False)
    print(f"Wrote {len(df)} clinical records -> {out}")
