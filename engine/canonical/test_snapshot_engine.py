import unittest

from .availability import (classify, validate, purchase_eligible,
                           effective_quantity, AvailabilityError)
from . import snapshot_engine as se


class AvailabilityTests(unittest.TestCase):
    def test_sentinel_is_unknown_not_unlimited(self):
        self.assertEqual(classify("999999999"), ("UNKNOWN", None))

    def test_sentinel_unlimited_requires_explicit_policy(self):
        self.assertEqual(classify("999999999", sentinel_state="UNLIMITED"),
                         ("UNLIMITED", None))

    def test_positive_is_finite(self):
        self.assertEqual(classify("165"), ("FINITE", 165))

    def test_zero_is_unavailable(self):
        self.assertEqual(classify("0"), ("UNAVAILABLE", 0))

    def test_blank_and_none_are_unknown(self):
        self.assertEqual(classify(""), ("UNKNOWN", None))
        self.assertEqual(classify(None), ("UNKNOWN", None))

    def test_negative_rejected(self):
        with self.assertRaises(AvailabilityError):
            classify("-1")

    def test_non_integer_rejected(self):
        with self.assertRaises(AvailabilityError):
            classify("lots")

    def test_finite_requires_positive(self):
        with self.assertRaises(AvailabilityError):
            validate("FINITE", 0)

    def test_unavailable_requires_zero(self):
        with self.assertRaises(AvailabilityError):
            validate("UNAVAILABLE", 5)

    def test_unknown_and_unlimited_reject_quantity(self):
        for state in ("UNKNOWN", "UNLIMITED"):
            with self.assertRaises(AvailabilityError):
                validate(state, 10)

    def test_unknown_not_eligible_by_default(self):
        ok, reason = purchase_eligible("UNKNOWN", None)
        self.assertFalse(ok)
        self.assertEqual(reason, "UNKNOWN_STOCK")

    def test_unknown_opt_in_is_explicit(self):
        ok, reason = purchase_eligible("UNKNOWN", None, allow_unknown=True)
        self.assertTrue(ok)
        self.assertEqual(reason, "UNKNOWN_STOCK_ALLOWED")

    def test_unavailable_never_eligible(self):
        ok, reason = purchase_eligible("UNAVAILABLE", 0, allow_unknown=True)
        self.assertFalse(ok)
        self.assertEqual(reason, "UNAVAILABLE")

    def test_insufficient_finite_stock(self):
        ok, reason = purchase_eligible("FINITE", 5, requested=10)
        self.assertFalse(ok)
        self.assertEqual(reason, "INSUFFICIENT_STOCK")

    def test_moq_and_pack_rounding(self):
        self.assertEqual(effective_quantity(1, moq=100, pack=1), 100)
        self.assertEqual(effective_quantity(120, moq=1, pack=50), 150)
        ok, _ = purchase_eligible("FINITE", 100, requested=1, moq=100)
        self.assertTrue(ok)


class ThresholdTests(unittest.TestCase):
    CLEAN = {"source_rows": 100, "eligible_rows": 100, "excluded_rows": 0,
             "matched_rows": 100, "inserted_offers": 100,
             "unmatched_rows": 0, "invalid_rows": 0, "duplicate_skus": 0}

    def test_clean_feed_passes(self):
        ok, reasons, _ = se.evaluate_thresholds(self.CLEAN)
        self.assertTrue(ok, reasons)

    def test_missing_offer_rejected(self):
        c = dict(self.CLEAN, inserted_offers=99)
        ok, reasons, _ = se.evaluate_thresholds(c)
        self.assertFalse(ok)
        self.assertIn("NOT_ALL_ELIGIBLE_ROWS_HAVE_OFFERS", reasons)

    def test_unmatched_rejected(self):
        c = dict(self.CLEAN, matched_rows=99, unmatched_rows=1)
        ok, reasons, _ = se.evaluate_thresholds(c)
        self.assertFalse(ok)

    def test_duplicate_skus_rejected(self):
        ok, reasons, _ = se.evaluate_thresholds(dict(self.CLEAN, duplicate_skus=1))
        self.assertIn("DUPLICATE_SUPPLIER_SKUS", reasons)

    def test_incomplete_feed_cannot_retire_catalogue(self):
        c = dict(self.CLEAN, source_rows=50, eligible_rows=50,
                 matched_rows=50, inserted_offers=50)
        ok, reasons, _ = se.evaluate_thresholds(c, previous_rows=100)
        self.assertFalse(ok)
        self.assertIn("ROW_COUNT_DECREASE_EXCEEDED", reasons)

    def test_row_totals_must_reconcile(self):
        ok, reasons, _ = se.evaluate_thresholds(dict(self.CLEAN, excluded_rows=5))
        self.assertIn("SOURCE_ROW_TOTALS_DO_NOT_RECONCILE", reasons)


class OrderingTests(unittest.TestCase):
    def test_newer_sequence_accepted(self):
        self.assertTrue(se.assert_newer(2, None, 1, None))

    def test_older_sequence_rejected(self):
        with self.assertRaises(se.SnapshotOrderingError):
            se.assert_newer(1, None, 2, None)

    def test_equal_sequence_rejected(self):
        with self.assertRaises(se.SnapshotOrderingError):
            se.assert_newer(2, None, 2, None)

    def test_mixed_ordering_metadata_is_ambiguous(self):
        with self.assertRaises(se.SnapshotOrderingError):
            se.assert_newer(2, None, None, "2026-01-01")

    def test_no_metadata_requires_override(self):
        with self.assertRaises(se.SnapshotOrderingError):
            se.assert_newer(None, None, None, None)
        self.assertTrue(se.assert_newer(None, None, None, None,
                                        allow_unordered=True))


class TransitionTests(unittest.TestCase):
    def test_staging_to_ready_allowed(self):
        self.assertTrue(se.check_transition("STAGING", "READY"))

    def test_promoted_cannot_return_to_ready(self):
        with self.assertRaises(se.SnapshotTransitionError):
            se.check_transition("PROMOTED", "READY")

    def test_rejected_is_terminal(self):
        for target in ("READY", "PROMOTED", "STAGING"):
            with self.assertRaises(se.SnapshotTransitionError):
                se.check_transition("REJECTED", target)

    def test_superseded_can_be_restored(self):
        self.assertTrue(se.check_transition("SUPERSEDED", "PROMOTED"))


if __name__ == "__main__":
    unittest.main()
