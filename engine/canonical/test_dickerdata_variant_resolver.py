import unittest
from unittest import mock

from importer.engine.canonical import supplier_import as si
from importer.engine.canonical.reconciliation import IdentityConflict


class ResolverCursor:
    def __init__(self, responses):
        self.responses = list(responses)
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchall(self):
        if not self.responses:
            raise AssertionError("Unexpected fetchall()")
        return self.responses.pop(0)


class DickerDataVariantResolverTests(unittest.TestCase):

    def test_exact_stock_code_resolves_variant_level_canonical_record(self):
        cursor = ResolverCursor([
            [(501, 77)],
            [("canonical-501", "77", "501")],
        ])

        result = si._resolve_dickerdata_variant(cursor, " dd-501 ")

        self.assertEqual(
            result,
            ("canonical-501", "DICKERDATA_STOCKCODE"),
        )
        self.assertEqual(
            cursor.executed[0][1],
            ["DD-501"],
        )
        self.assertEqual(
            cursor.executed[1][1],
            ["501"],
        )

    def test_missing_saleor_variant_is_unmatched(self):
        cursor = ResolverCursor([[]])

        self.assertEqual(
            si._resolve_dickerdata_variant(cursor, "MISSING-SKU"),
            (None, "UNMATCHED"),
        )
        self.assertEqual(len(cursor.executed), 1)

    def test_saleor_variant_without_canonical_bridge_is_unmatched(self):
        cursor = ResolverCursor([
            [(501, 77)],
            [],
        ])

        self.assertEqual(
            si._resolve_dickerdata_variant(cursor, "DD-501"),
            (None, "UNMATCHED"),
        )

    def test_duplicate_saleor_variant_sku_is_conflict(self):
        cursor = ResolverCursor([
            [(501, 77), (502, 77)],
        ])

        with self.assertRaises(IdentityConflict):
            si._resolve_dickerdata_variant(cursor, "DD-501")

    def test_duplicate_canonical_variant_bridge_is_conflict(self):
        cursor = ResolverCursor([
            [(501, 77)],
            [
                ("canonical-a", "77", "501"),
                ("canonical-b", "77", "501"),
            ],
        ])

        with self.assertRaises(IdentityConflict):
            si._resolve_dickerdata_variant(cursor, "DD-501")

    def test_canonical_parent_product_mismatch_is_conflict(self):
        cursor = ResolverCursor([
            [(501, 77)],
            [("canonical-501", "999", "501")],
        ])

        with self.assertRaises(IdentityConflict):
            si._resolve_dickerdata_variant(cursor, "DD-501")

    def test_dickerdata_path_never_calls_mpn_identity_resolver(self):
        canonical_id = "canonical-501"
        cursor = mock.MagicMock()
        cursor.fetchone.side_effect = [
            ("dickerdata",),
            ("supplier-link-501", canonical_id),
        ]

        row = (
            "row-501",
            "DD-501",
            "Example product",
            "Example manufacturer",
            "Example brand",
            None,
            "MPN-COLLISION",
            None,
            "AUD",
            5,
            None,
            {"StockCode": "DD-501", "VendorStockCode": "MPN-COLLISION"},
        )

        with (
            mock.patch.object(
                si,
                "_resolve_dickerdata_variant",
                return_value=(canonical_id, "DICKERDATA_STOCKCODE"),
            ) as variant_resolver,
            mock.patch.object(si, "resolve_identity") as mpn_resolver,
            mock.patch.object(si, "register_identity") as register_identity,
            mock.patch.object(
                si.CanonicalRepository,
                "link_supplier",
            ),
            mock.patch.object(
                si.CanonicalRepository,
                "record_reconciliation",
            ),
        ):
            result = si._process_row(cursor, "batch-501", row)

        self.assertEqual(result, "MATCHED")
        variant_resolver.assert_called_once_with(cursor, "DD-501")
        mpn_resolver.assert_not_called()
        register_identity.assert_not_called()

    def test_unmatched_rows_make_batch_completion_unsuccessful(self):
        self.assertEqual(
            si._batch_completion_status({
                "MATCHED": 10,
                "UNMATCHED": 1,
                "CONFLICT": 0,
                "INVALID": 0,
            }),
            "COMPLETED_WITH_ERRORS",
        )

    def test_conflicts_and_invalid_rows_make_batch_unsuccessful(self):
        self.assertEqual(
            si._batch_completion_status({
                "MATCHED": 10,
                "UNMATCHED": 0,
                "CONFLICT": 1,
                "INVALID": 1,
            }),
            "COMPLETED_WITH_ERRORS",
        )

    def test_all_matched_rows_complete_cleanly(self):
        self.assertEqual(
            si._batch_completion_status({
                "MATCHED": 10,
                "UNMATCHED": 0,
                "CONFLICT": 0,
                "INVALID": 0,
            }),
            "COMPLETED",
        )


if __name__ == "__main__":
    unittest.main()
