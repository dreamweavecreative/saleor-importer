import importlib.util
import unittest
from pathlib import Path

source = Path(__file__).with_name("snapshot_pipeline.py")
spec = importlib.util.spec_from_file_location("snapshot_pipeline_isolated", source)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class SnapshotAvailabilityTests(unittest.TestCase):
    def test_sentinel_is_unknown_not_unlimited(self):
        self.assertEqual(
            module.availability_from_record({"StockAvailable": "999999999"}),
            ("UNKNOWN", None),
        )

    def test_positive_stock_is_finite(self):
        self.assertEqual(
            module.availability_from_record({"StockAvailable": "5"}),
            ("FINITE", 5),
        )

    def test_zero_stock_is_unavailable(self):
        self.assertEqual(
            module.availability_from_record({"StockAvailable": "0"}),
            ("UNAVAILABLE", 0),
        )

    def test_missing_stock_is_unknown(self):
        self.assertEqual(
            module.availability_from_record({"StockAvailable": ""}),
            ("UNKNOWN", None),
        )

    def test_negative_stock_rejected(self):
        with self.assertRaises(module.SnapshotValidationError):
            module.availability_from_record({"StockAvailable": "-1"})

    def test_finite_requires_positive_quantity(self):
        with self.assertRaises(module.SnapshotValidationError):
            module.validate_availability("FINITE", 0)

    def test_unknown_requires_null_quantity(self):
        with self.assertRaises(module.SnapshotValidationError):
            module.validate_availability("UNKNOWN", 0)

    def test_unlimited_requires_null_quantity(self):
        with self.assertRaises(module.SnapshotValidationError):
            module.validate_availability("UNLIMITED", 10)


if __name__ == "__main__":
    unittest.main()
