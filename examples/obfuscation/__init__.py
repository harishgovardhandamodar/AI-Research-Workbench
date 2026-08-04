"""Data-obfuscation experiments for the Fox workbench.

Importable from the persistent kernel (cwd = repo root, which is on sys.path):

    from examples.obfuscation.credit_card_data import generate_credit_card
    from examples.obfuscation.obfuscate import apply_masking, k_anonymize
    from examples.obfuscation import experiments as exp

    df = generate_credit_card(2000, seed=42)
    report = exp.run_all(df)

The 8 threat scenarios come from the obfuscation study
(`~/WorkBook/obfuscation-study`), adapted to run on synthetic credit-card
transaction data: BEC/fraud, insider threat, supply-chain leakage, sanctions
evasion, corporate espionage, test-environment exposure, account-takeover via
security questions, and re-identification — plus a counterparty-reconstruction
supplement.
"""

from .obfuscate import (  # noqa: F401
    HIGH_SENSITIVITY,
    MEDIUM_SENSITIVITY,
    apply_masking,
    fuzzy_bucket,
    k_anonymize,
    mask_amount,
    mask_bic,
    mask_card,
    mask_city,
    mask_iban,
    mask_name,
    noisy_aggregate,
    obfuscate_dataframe,
    sanitize_metadata,
    token_id,
    tokenize,
)
from .credit_card_data import (  # noqa: F401
    FIELDNAMES,
    QUASI_IDENTIFIER_COLUMNS,
    SENSITIVE_COLUMNS,
    generate_credit_card,
)

__all__ = [
    "generate_credit_card", "FIELDNAMES", "SENSITIVE_COLUMNS",
    "apply_masking", "tokenize", "token_id", "fuzzy_bucket", "k_anonymize",
    "noisy_aggregate", "sanitize_metadata", "obfuscate_dataframe",
    "mask_iban", "mask_bic", "mask_card", "mask_name", "mask_amount",
    "mask_city",
    "HIGH_SENSITIVITY", "MEDIUM_SENSITIVITY", "QUASI_IDENTIFIER_COLUMNS",
]
