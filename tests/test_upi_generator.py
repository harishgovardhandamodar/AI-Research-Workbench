"""UPI generator tests: the notebook-adapted synthetic UPI data generator."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend.upi_generator import (generate_upi_set, generate_upi_csv,
                                   hourly_distribution)

REAL_COLS = ["transaction id", "timestamp", "transaction type",
             "merchant_category", "amount (INR)", "transaction_status",
             "sender_age_group", "receiver_age_group", "sender_state",
             "sender_bank", "receiver_bank", "device_type", "network_type",
             "fraud_flag", "hour_of_day", "day_of_week", "is_weekend"]


class TestUpiGenerator(unittest.TestCase):
    def test_schema_matches_real_data(self):
        df = generate_upi_set(n_records=200, seed=42)
        self.assertEqual(list(df.columns), REAL_COLS)
        self.assertEqual(len(df), 200)

    def test_deterministic(self):
        a = generate_upi_set(n_records=100, seed=7)
        b = generate_upi_set(n_records=100, seed=7)
        self.assertTrue(a.equals(b))
        c = generate_upi_set(n_records=100, seed=8)
        self.assertFalse(a.equals(c))

    def test_amounts_plausible(self):
        df = generate_upi_set(n_records=1000, seed=1)
        amt = df["amount (INR)"]
        self.assertGreater(amt.min(), 0)
        self.assertLessEqual(amt.max(), 100000)

    def test_hourly_distribution_sums_to_one(self):
        probs = hourly_distribution()
        self.assertEqual(len(probs), 24)
        self.assertAlmostEqual(probs.sum(), 1.0, places=3)

    def test_generate_upi_csv(self):
        tmp = Path(tempfile.mkdtemp())
        path = generate_upi_csv(tmp / "upi.csv", n_records=50, seed=3)
        self.assertTrue(path.exists())
        self.assertIn("sender_bank", path.read_text()[:200])

    def test_ensure_runnable_dataset_uses_generator(self):
        from backend import experiment_planner as ep
        tmp = Path(tempfile.mkdtemp())
        name, synthetic = ep.ensure_runnable_dataset(tmp)
        self.assertTrue(synthetic)
        import pandas as pd
        df = pd.read_csv(tmp / name)
        self.assertEqual(list(df.columns), REAL_COLS)


if __name__ == "__main__":
    unittest.main()
