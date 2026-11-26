import unittest

from . import offer_scoped as osc


class StateResolutionTests(unittest.TestCase):
    def test_explicit_unlimited_drops_quantity(self):
        self.assertEqual(osc.resolve_state("UNLIMITED", 999, {}), ("UNLIMITED", None))

    def test_explicit_unknown_drops_quantity(self):
        self.assertEqual(osc.resolve_state("UNKNOWN", 5, {}), ("UNKNOWN", None))

    def test_unavailable_forces_zero(self):
        self.assertEqual(osc.resolve_state("UNAVAILABLE", 7, {}), ("UNAVAILABLE", 0))

    def test_finite_keeps_quantity(self):
        self.assertEqual(osc.resolve_state("FINITE", 7, {}), ("FINITE", 7))

    def test_finite_zero_rejected(self):
        with self.assertRaises(ValueError):
            osc.resolve_state("FINITE", 0, {})

    def test_payload_state_used_when_absent(self):
        self.assertEqual(
            osc.resolve_state(None, None, {"availability_state": "UNLIMITED"}),
            ("UNLIMITED", None))

    def test_null_quantity_never_infers_unlimited(self):
        self.assertEqual(osc.resolve_state(None, None, {}), ("UNKNOWN", None))

    def test_zero_quantity_infers_unavailable(self):
        self.assertEqual(osc.resolve_state(None, 0, {}), ("UNAVAILABLE", 0))

    def test_positive_quantity_infers_finite(self):
        self.assertEqual(osc.resolve_state(None, 4, {}), ("FINITE", 4))

    def test_invalid_state_rejected(self):
        with self.assertRaises(osc.OfferScopeError):
            osc.resolve_state("MAYBE", None, {})


class CommercialValidationTests(unittest.TestCase):
    def test_zero_cost_allowed(self):
        _, code, _, _ = osc.validate_commercial("0", "aud", 1, 1, None)
        self.assertEqual(code, "AUD")

    def test_negative_cost_rejected(self):
        with self.assertRaises(osc.OfferScopeError):
            osc.validate_commercial("-1", "AUD", 1, 1, None)

    def test_bad_currency_rejected(self):
        with self.assertRaises(osc.OfferScopeError):
            osc.validate_commercial("1", "AU", 1, 1, None)

    def test_zero_moq_rejected(self):
        with self.assertRaises(osc.OfferScopeError):
            osc.validate_commercial("1", "AUD", 0, 1, None)

    def test_none_moq_defaults_to_one(self):
        _, _, moq, _ = osc.validate_commercial("1", "AUD", None, None, None)
        self.assertEqual(int(moq), 1)

    def test_negative_lead_time_rejected(self):
        with self.assertRaises(osc.OfferScopeError):
            osc.validate_commercial("1", "AUD", 1, 1, -1)

    def test_boolean_lead_time_rejected(self):
        with self.assertRaises(osc.OfferScopeError):
            osc.validate_commercial("1", "AUD", 1, 1, True)


class SqlShapeTests(unittest.TestCase):
    def test_conflict_target_is_scoped(self):
        self.assertIn("ON CONFLICT (supplier_link_id, snapshot_batch_id)",
                      osc.UPSERT_SQL)

    def test_reappearance_reactivates(self):
        self.assertIn("is_active = TRUE", osc.UPSERT_SQL)
        self.assertIn("retired_at = NULL", osc.UPSERT_SQL)
        self.assertIn("retirement_reason = NULL", osc.UPSERT_SQL)

    def test_batch_id_required(self):
        with self.assertRaises(osc.OfferScopeError):
            osc.upsert_scoped(None, None, 1, "1", "AUD")


if __name__ == "__main__":
    unittest.main()
