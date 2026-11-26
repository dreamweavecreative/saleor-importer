import unittest
from . import visibility_apply as va


class DecisionTests(unittest.TestCase):
    def test_no_supply_and_sellable_withdraws(self):
        self.assertEqual(va.decide(True, True, True, False), va.WITHDRAW)

    def test_no_supply_partially_sellable_withdraws(self):
        self.assertEqual(va.decide(False, False, True, False), va.WITHDRAW)

    def test_no_supply_already_down_unchanged(self):
        self.assertEqual(va.decide(False, False, False, False), va.UNCHANGED)

    def test_supply_present_never_withdraws(self):
        self.assertEqual(va.decide(True, True, True, True), va.UNCHANGED)

    def test_reactivation_requires_intent(self):
        self.assertEqual(va.decide(False, False, False, True), va.UNCHANGED)

    def test_reactivation_with_intent(self):
        self.assertEqual(va.decide(False, False, False, True, True), va.REACTIVATE)

    def test_fully_live_not_reactivated(self):
        self.assertEqual(va.decide(True, True, True, True, True), va.UNCHANGED)


class PartitionTests(unittest.TestCase):
    ROWS = [
        ("c1", "1", "11", True, True, True, False, False),
        ("c2", "2", "12", False, False, False, False, False),
        ("c3", "3", "13", True, True, True, True, False),
        ("c4", "4", "14", False, False, False, True, False),
        ("c5", "5", "15", True, True, True, False, True),
    ]

    def test_partition_counts(self):
        p = va.partition(self.ROWS)
        self.assertEqual(len(p[va.WITHDRAW]), 1)
        self.assertEqual(len(p[va.UNCHANGED]), 3)
        self.assertEqual(len(p[va.PROTECTED]), 1)

    def test_checkout_variant_is_protected_not_withdrawn(self):
        p = va.partition(self.ROWS)
        self.assertEqual(p[va.PROTECTED][0]["canonical_product_id"], "c5")
        self.assertNotIn("c5", [x["canonical_product_id"] for x in p[va.WITHDRAW]])

    def test_partition_with_reactivate(self):
        p = va.partition(self.ROWS, reactivate=True)
        self.assertEqual(p[va.REACTIVATE][0]["canonical_product_id"], "c4")

    def test_payload_shape(self):
        item = va.partition(self.ROWS)[va.WITHDRAW][0]
        self.assertEqual(set(item),
                         {"canonical_product_id", "saleor_product_id",
                          "saleor_variant_id"})


class GuardTests(unittest.TestCase):
    def test_refuses_without_confirm(self):
        r = va.apply_withdrawal(None, [{"saleor_product_id": "1",
                                        "saleor_variant_id": "11"}])
        self.assertFalse(r["applied"])

    def test_circuit_breaker(self):
        targets = [{"saleor_product_id": str(i), "saleor_variant_id": str(i)}
                   for i in range(10)]
        r = va.apply_withdrawal(None, targets, confirm=True, max_changes=5)
        self.assertFalse(r["applied"])
        self.assertEqual(r["would_change"], 10)

    def test_empty_targets_needs_no_database(self):
        r = va.apply_withdrawal(None, [], confirm=True)
        self.assertTrue(r["applied"])
        self.assertEqual(r["chunks"], 0)
        self.assertEqual(r["product_listings_changed"], 0)


if __name__ == "__main__":
    unittest.main()
