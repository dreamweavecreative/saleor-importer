import unittest
from decimal import Decimal
from . import storefront_sync as ss


class ClassifyTests(unittest.TestCase):
    def test_clean_row_matches_stored_is_unchanged(self):
        a, p, c = ss.classify("6.75", "5.05", "7.425", "5.050")
        self.assertEqual(a, ss.UNCHANGED)
        self.assertEqual(p, Decimal("7.425"))

    def test_clean_row_differing_price_updates(self):
        a, p, c = ss.classify("6.75", "5.05", "9.999", "5.050")
        self.assertEqual(a, ss.UPDATE)
        self.assertEqual(p, Decimal("7.425"))

    def test_half_up_rounding(self):
        a, p, c = ss.classify("15.35", "9.95", "0", "0")
        self.assertEqual(p, Decimal("16.885"))

    def test_rrp_sentinel_skipped(self):
        for s in ("0", "9999", "9999999"):
            self.assertEqual(ss.classify(s, "5", "1", "1")[0], ss.SKIP_SENTINEL)

    def test_cost_sentinel_skipped(self):
        self.assertEqual(ss.classify("100", "9999", "1", "1")[0], ss.SKIP_SENTINEL)

    def test_below_cost_skipped(self):
        self.assertEqual(ss.classify("263", "1942.97", "1", "1")[0], ss.SKIP_BELOW_COST)

    def test_missing_rrp_skipped(self):
        self.assertEqual(ss.classify("", "5", "1", "1")[0], ss.SKIP_NO_RRP)
        self.assertEqual(ss.classify(None, "5", "1", "1")[0], ss.SKIP_NO_RRP)

    def test_negative_cost_skipped(self):
        self.assertEqual(ss.classify("100", "-1", "1", "1")[0], ss.SKIP_NO_RRP)


class ApplyGuardTests(unittest.TestCase):
    def test_refuses_without_confirm(self):
        r = ss.apply(None, [{"variant_id":"1","product_id":"1","price":"1","cost":"1"}])
        self.assertFalse(r["applied"])

    def test_empty_is_noop(self):
        r = ss.apply(None, [], confirm=True)
        self.assertTrue(r["applied"]); self.assertEqual(r["chunks"], 0)

    def test_circuit_breaker(self):
        u=[{"variant_id":str(i),"product_id":str(i),"price":"1","cost":"1"} for i in range(10)]
        r = ss.apply(None, u, confirm=True, max_changes=5)
        self.assertFalse(r["applied"]); self.assertEqual(r["would_change"], 10)


if __name__ == "__main__":
    unittest.main()
