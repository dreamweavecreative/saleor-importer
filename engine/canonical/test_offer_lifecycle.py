import unittest
from decimal import Decimal

from . import availability as av
from . import offer_writer as ow
from . import offer_selection as osel
from . import visibility_policy as vp
from . import enrichment_policy as ep
from . import lifecycle_authority as la


class OfferValidationTests(unittest.TestCase):
    def test_finite_offer_accepted(self):
        v = ow.validate_offer("10.00", "aud", "FINITE", 5)
        self.assertEqual(v["currency"], "AUD")
        self.assertEqual(v["unit_cost"], Decimal("10.00"))

    def test_unknown_must_not_carry_quantity(self):
        with self.assertRaises(ValueError):
            ow.validate_offer("10", "AUD", "UNKNOWN", 5)

    def test_unlimited_must_not_carry_quantity(self):
        with self.assertRaises(ValueError):
            ow.validate_offer("10", "AUD", "UNLIMITED", 5)

    def test_unavailable_requires_zero(self):
        v = ow.validate_offer("10", "AUD", "UNAVAILABLE", 0)
        self.assertEqual(v["available_quantity"], Decimal("0"))

    def test_zero_cost_allowed_but_negative_rejected(self):
        ow.validate_offer("0", "AUD", "UNKNOWN", None)
        with self.assertRaises(ow.OfferWriteError):
            ow.validate_offer("-1", "AUD", "UNKNOWN", None)

    def test_bad_currency_rejected(self):
        with self.assertRaises(ow.OfferWriteError):
            ow.validate_offer("10", "AU", "UNKNOWN", None)

    def test_zero_moq_rejected(self):
        with self.assertRaises(ow.OfferWriteError):
            ow.validate_offer("10", "AUD", "UNKNOWN", None, minimum_order_quantity=0)

    def test_negative_lead_time_rejected(self):
        with self.assertRaises(ow.OfferWriteError):
            ow.validate_offer("10", "AUD", "UNKNOWN", None, lead_time_days=-1)

    def test_boolean_lead_time_rejected(self):
        with self.assertRaises(ow.OfferWriteError):
            ow.validate_offer("10", "AUD", "UNKNOWN", None, lead_time_days=True)


class SelectionTests(unittest.TestCase):
    def _c(self, sup, unit, total, rel=50, pri=100):
        return {"supplier_id": sup, "landed_unit_cost": unit,
                "landed_total": total, "reliability_score": rel, "priority": pri}

    def test_lowest_landed_unit_cost_wins(self):
        r = osel.rank_candidates([self._c("b", "12", "120"),
                                  self._c("a", "10", "100")])
        self.assertEqual(r[0]["supplier_id"], "a")

    def test_total_breaks_unit_tie(self):
        r = osel.rank_candidates([self._c("b", "10", "200"),
                                  self._c("a", "10", "100")])
        self.assertEqual(r[0]["supplier_id"], "a")

    def test_reliability_breaks_cost_tie(self):
        r = osel.rank_candidates([self._c("b", "10", "100", rel=40),
                                  self._c("a", "10", "100", rel=90)])
        self.assertEqual(r[0]["supplier_id"], "a")

    def test_unknown_not_eligible_by_default(self):
        ok, why = osel.screen({"availability_state": "UNKNOWN",
                               "available_quantity": None, "rule_active": True})
        self.assertFalse(ok)
        self.assertEqual(why, "UNKNOWN_STOCK")

    def test_unavailable_never_eligible(self):
        ok, why = osel.screen({"availability_state": "UNAVAILABLE",
                               "available_quantity": 0, "rule_active": True},
                              allow_unknown=True)
        self.assertFalse(ok)

    def test_missing_landed_cost_rule_is_eligible(self):
        ok, why = osel.screen({"availability_state": "FINITE",
                               "available_quantity": 100, "rule_active": None})
        self.assertTrue(ok)
        self.assertEqual(why, "ELIGIBLE")

    def test_explicitly_inactive_rule_rejected(self):
        ok, why = osel.screen({"availability_state": "FINITE",
                               "available_quantity": 100, "rule_active": False})
        self.assertFalse(ok)
        self.assertEqual(why, "SUPPLIER_RULE_INACTIVE")

    def test_finite_with_stock_eligible(self):
        ok, _ = osel.screen({"availability_state": "FINITE",
                             "available_quantity": 100, "rule_active": True})
        self.assertTrue(ok)


class VisibilityTests(unittest.TestCase):
    def test_no_supply_withdraws_published(self):
        d = vp.decide(False, False, True, True, True)
        self.assertEqual(d["action"], vp.WITHDRAW)
        self.assertFalse(d["is_published"])
        self.assertFalse(d["is_available_for_purchase"])

    def test_other_supplier_keeps_product_live(self):
        d = vp.decide(True, False, True, True, True)
        self.assertEqual(d["action"], vp.UNCHANGED)

    def test_owned_stock_keeps_product_live(self):
        d = vp.decide(False, True, True, True, True)
        self.assertEqual(d["action"], vp.UNCHANGED)

    def test_no_force_publication_without_intent(self):
        d = vp.decide(True, False, False, False, False)
        self.assertEqual(d["action"], vp.UNCHANGED)

    def test_explicit_reactivation_publishes(self):
        d = vp.decide(True, False, False, False, False, reactivate=True)
        self.assertEqual(d["action"], vp.REACTIVATE)
        self.assertTrue(d["is_published"])

    def test_already_withdrawn_is_unchanged(self):
        d = vp.decide(False, False, False, False, False)
        self.assertEqual(d["action"], vp.UNCHANGED)

    def test_summary_counts(self):
        s = vp.summarise([vp.decide(False, False, True, True, True),
                          vp.decide(True, False, True, True, True)])
        self.assertEqual(s["counts"]["WITHDRAW"], 1)
        self.assertEqual(s["evaluated"], 2)


class EnrichmentTests(unittest.TestCase):
    def test_no_supply_skips_provider_spend(self):
        d = ep.decide(False, False, "DELL LAPTOP")
        self.assertEqual(d["action"], "SKIP")
        self.assertEqual(d["provider_calls"], 0)

    def test_verified_media_skipped(self):
        d = ep.decide(True, True, "DELL LAPTOP")
        self.assertEqual(d["action"], "SKIP")

    def test_force_overrides_verified_media(self):
        d = ep.decide(True, True, "DELL LAPTOP", force=True)
        self.assertEqual(d["action"], "SOURCE")

    def test_digital_type_gets_card_no_api(self):
        d = ep.decide(True, False, "MS NCE O365", product_type_slug="digital-services")
        self.assertEqual(d["action"], "CARD")
        self.assertEqual(d["provider_calls"], 0)

    def test_warranty_name_gets_card(self):
        d = ep.decide(True, False, "HP 4YR PARTS & LABOUR NBD ONSITE")
        self.assertEqual(d["action"], "CARD")

    def test_physical_with_supply_sources(self):
        d = ep.decide(True, False, "LENOVO THINKPAD T1G G8", "NOTEBOOKS")
        self.assertEqual(d["action"], "SOURCE")
        self.assertEqual(d["provider_calls"], 1)

    def test_summary_estimates_provider_calls(self):
        s = ep.summarise([ep.decide(True, False, "LENOVO LAPTOP", "NOTEBOOKS"),
                          ep.decide(True, False, "HP WARRANTY NBD")])
        self.assertEqual(s["estimated_provider_calls"], 1)


class AuthorityTests(unittest.TestCase):
    def test_deprecated_lifecycle_guarded(self):
        for name in ("snapshot_lifecycle", "snapshot_pipeline",
                     "supplier_offers", "landed_cost"):
            with self.assertRaises(la.DeprecatedLifecycleUse):
                la.guard(name)

    def test_authoritative_allowed(self):
        for name in la.AUTHORITATIVE:
            self.assertTrue(la.guard(name))

    def test_dotted_name_resolves(self):
        with self.assertRaises(la.DeprecatedLifecycleUse):
            la.guard("importer.engine.canonical.snapshot_lifecycle")


if __name__ == "__main__":
    unittest.main()
