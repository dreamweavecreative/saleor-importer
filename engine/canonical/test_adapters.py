import unittest

from importer.engine.adapters import get, available
from importer.engine.adapters.base import FeedValidationError
from importer.engine.adapters.dickerdata import (
    EXPECTED_COLUMNS, UNLIMITED_STOCK_SENTINEL, DickerDataAdapter,
)
from importer.engine.canonical import availability_policy as pol


def row(**over):
    base = {c: "" for c in EXPECTED_COLUMNS}
    base.update({"StockCode": "SKU-1", "Vendor": "ACME",
                 "VendorStockCode": "MPN-1", "StockDescription": "Thing",
                 "RRPEx": "20.00", "DealerEx": "10.00",
                 "StockAvailable": "7", "Status": "A", "Type": "StockedItem"})
    base.update(over)
    return base


class RegistryTests(unittest.TestCase):
    def test_dickerdata_registered(self):
        self.assertIn("dickerdata", available())
        self.assertEqual(get("dickerdata").supplier_id, "dickerdata")

    def test_unknown_supplier_rejected(self):
        with self.assertRaises(Exception):
            get("no-such-supplier")

    def test_contract_methods_present(self):
        a = DickerDataAdapter()
        for name in ("validate_headers", "stream_records", "is_eligible",
                     "normalise", "validate_record", "availability",
                     "resolve_identity", "ordering", "validate_feed"):
            self.assertTrue(callable(getattr(a, name)), name)


class HeaderTests(unittest.TestCase):
    def setUp(self):
        self.a = DickerDataAdapter()

    def test_exact_headers_accepted(self):
        self.assertEqual(self.a.validate_headers(list(EXPECTED_COLUMNS)),
                         list(EXPECTED_COLUMNS))

    def test_missing_column_rejected(self):
        cols = [c for c in EXPECTED_COLUMNS if c != "DealerEx"]
        with self.assertRaises(FeedValidationError):
            self.a.validate_headers(cols)

    def test_unexpected_column_rejected(self):
        with self.assertRaises(FeedValidationError):
            self.a.validate_headers(list(EXPECTED_COLUMNS) + ["Extra"])

    def test_duplicate_headers_rejected(self):
        with self.assertRaises(FeedValidationError):
            self.a.validate_headers(list(EXPECTED_COLUMNS) + ["StockCode"])


class EligibilityTests(unittest.TestCase):
    def setUp(self):
        self.a = DickerDataAdapter()

    def test_active_stocked_item_eligible(self):
        self.assertEqual(self.a.is_eligible(row()), (True, ""))

    def test_bundled_item_excluded(self):
        ok, why = self.a.is_eligible(row(Type="BundledItem"))
        self.assertFalse(ok)
        self.assertEqual(why, "non_stocked_type")

    def test_terminated_excluded(self):
        ok, why = self.a.is_eligible(row(Status="T"))
        self.assertFalse(ok)
        self.assertEqual(why, "non_active_status")

    def test_case_and_whitespace_tolerated(self):
        self.assertTrue(self.a.is_eligible(row(Status=" a ",
                                              Type=" stockeditem "))[0])


class AvailabilityTests(unittest.TestCase):
    def setUp(self):
        self.a = DickerDataAdapter()

    def test_sentinel_is_unlimited_per_specification(self):
        self.assertEqual(self.a.availability(row(StockAvailable="999999999")),
                         ("UNLIMITED", None))
        self.assertEqual(self.a.sentinel_policy["meaning"], "UNLIMITED")

    def test_positive_is_finite(self):
        self.assertEqual(self.a.availability(row(StockAvailable="12")),
                         ("FINITE", 12))

    def test_zero_is_unavailable(self):
        self.assertEqual(self.a.availability(row(StockAvailable="0")),
                         ("UNAVAILABLE", 0))

    def test_blank_is_unknown(self):
        self.assertEqual(self.a.availability(row(StockAvailable="")),
                         ("UNKNOWN", None))

    def test_negative_rejected(self):
        with self.assertRaises(FeedValidationError):
            self.a.availability(row(StockAvailable="-1"))

    def test_non_integer_rejected(self):
        with self.assertRaises(FeedValidationError):
            self.a.availability(row(StockAvailable="lots"))


class NormalisationTests(unittest.TestCase):
    def setUp(self):
        self.a = DickerDataAdapter()

    def test_native_columns_retained(self):
        out = self.a.normalise(row(PrimaryCategory="NOTEBOOKS", ETA="TBA"))
        self.assertEqual(out["PrimaryCategory"], "NOTEBOOKS")
        self.assertEqual(out["ETA"], "TBA")
        self.assertEqual(out["DealerEx"], "10.00")

    def test_cost_is_aud_ex_gst(self):
        out = self.a.normalise(row())
        self.assertEqual(out["currency"], "AUD")
        self.assertFalse(self.a.tax_inclusive)

    def test_missing_cost_rejected(self):
        with self.assertRaises(FeedValidationError):
            self.a.normalise(row(DealerEx=""))

    def test_lead_time_never_invented(self):
        self.assertIsNone(self.a.lead_time_days(row(ETA="TBA")))

    def test_availability_state_carried(self):
        out = self.a.normalise(row(StockAvailable="999999999"))
        self.assertEqual(out["availability_state"], "UNLIMITED")
        self.assertIsNone(out["stock"])
        self.assertTrue(self.a.validate_record(out))


class SharedPolicyTests(unittest.TestCase):
    def test_no_supplier_names_in_shared_policy(self):
        import inspect
        src = inspect.getsource(pol)
        for token in ("dickerdata", "StockAvailable", "999999999", "DealerEx"):
            self.assertNotIn(token, src)

    def test_unlimited_is_eligible(self):
        ok, why = pol.purchase_eligible("UNLIMITED", None)
        self.assertTrue(ok)
        self.assertEqual(why, "UNLIMITED")

    def test_unknown_not_eligible_by_default(self):
        ok, why = pol.purchase_eligible("UNKNOWN", None)
        self.assertFalse(ok)
        self.assertEqual(why, "UNKNOWN_STOCK")

    def test_unavailable_never_eligible(self):
        ok, _ = pol.purchase_eligible("UNAVAILABLE", 0, allow_unknown=True)
        self.assertFalse(ok)

    def test_moq_and_pack_rounding(self):
        self.assertEqual(pol.effective_quantity(1, moq=100), 100)
        self.assertEqual(pol.effective_quantity(120, pack=50), 150)

    def test_incomplete_feed_cannot_retire_catalogue(self):
        counts = {"source_rows": 5000, "eligible_rows": 5000,
                  "excluded_rows": 0, "matched_rows": 5000,
                  "inserted_offers": 5000, "unmatched_rows": 0,
                  "invalid_rows": 0, "duplicate_skus": 0}
        ok, reasons = pol.evaluate_feed(counts, previous_eligible_rows=72513)
        self.assertFalse(ok)
        self.assertIn("ROW_COUNT_DECREASE_EXCEEDED", reasons)

    def test_clean_full_feed_passes(self):
        counts = {"source_rows": 75315, "eligible_rows": 72513,
                  "excluded_rows": 2802, "matched_rows": 72513,
                  "inserted_offers": 72513, "unmatched_rows": 0,
                  "invalid_rows": 0, "duplicate_skus": 0}
        ok, reasons = pol.evaluate_feed(counts, previous_eligible_rows=72513,
                                        thresholds={"min_eligible_rows": 1000})
        self.assertTrue(ok, reasons)

    def test_duplicate_skus_rejected(self):
        counts = {"source_rows": 10, "eligible_rows": 10, "excluded_rows": 0,
                  "matched_rows": 10, "inserted_offers": 10,
                  "unmatched_rows": 0, "invalid_rows": 0, "duplicate_skus": 1}
        ok, reasons = pol.evaluate_feed(counts)
        self.assertIn("DUPLICATE_SUPPLIER_SKUS", reasons)


class FeedKindTests(unittest.TestCase):
    def test_dickerdata_declares_full(self):
        self.assertEqual(DickerDataAdapter().feed_kind, "FULL")

    def test_ordering_is_sequence_not_mtime(self):
        a = DickerDataAdapter()
        self.assertEqual(a.ordering_mode, "sequence")
        self.assertEqual(a.ordering("/nonexistent", 2, None), (2, None))


if __name__ == "__main__":
    unittest.main()
