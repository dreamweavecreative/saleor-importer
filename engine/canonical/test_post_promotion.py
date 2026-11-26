import unittest
from . import post_promotion as pp


class GuardTests(unittest.TestCase):
    """These must return BEFORE any Django or importer import."""

    def test_run_refuses_without_confirm(self):
        r = pp.run(None)
        self.assertFalse(r["applied"])
        self.assertIn("confirm", r["error"])

    def test_reactivate_refuses_without_confirm(self):
        r = pp.apply_reactivation(None, [{"canonical_product_id":"c",
                                          "saleor_product_id":"1",
                                          "saleor_variant_id":"1"}])
        self.assertFalse(r["applied"])

    def test_reactivate_empty_is_noop(self):
        r = pp.apply_reactivation(None, [], confirm=True)
        self.assertTrue(r["applied"]); self.assertEqual(r["chunks"], 0)

    def test_reactivate_circuit_breaker(self):
        t=[{"canonical_product_id":str(i),"saleor_product_id":str(i),
            "saleor_variant_id":str(i)} for i in range(10)]
        r = pp.apply_reactivation(None, t, confirm=True, max_changes=5)
        self.assertFalse(r["applied"]); self.assertEqual(r["would_change"], 10)

    def test_actor_is_automation(self):
        self.assertEqual(pp.ACTOR, "automation")


class PriceGateTests(unittest.TestCase):
    def test_reactivation_sql_contains_price_gate(self):
        import inspect
        src = inspect.getsource(pp.reactivation_targets)
        self.assertIn("NOT e.price_sentinel", src)
        self.assertIn("NOT e.below_cost", src)

    def test_reactivation_sql_contains_ledger_gate(self):
        import inspect
        src = inspect.getsource(pp.reactivation_targets)
        self.assertIn("la.action='WITHDRAW'", src)


if __name__ == "__main__":
    unittest.main()
