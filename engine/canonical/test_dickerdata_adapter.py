import unittest

from importer.engine.dickerdata_adapter import (
    EXPECTED_COLUMNS,
    UNLIMITED_STOCK_SENTINEL,
    adapt_records,
)


def make_row(**overrides):
    row = {column: "" for column in EXPECTED_COLUMNS}
    row.update({
        "StockCode": "SKU-001",
        "Vendor": "Example Vendor",
        "VendorStockCode": "MPN-001",
        "StockDescription": "Example Product",
        "RRPEx": "29.95",
        "DealerEx": "19.95",
        "StockAvailable": "12",
        "Status": "A",
        "Type": "StockedItem",
    })
    row.update(overrides)
    return row


class DickerDataAdapterTests(unittest.TestCase):

    def test_maps_native_fields_to_canonical_fields(self):
        records, summary = adapt_records([make_row()])

        self.assertEqual(len(records), 1)
        record = records[0]

        self.assertEqual(record["sku"], "SKU-001")
        self.assertEqual(record["name"], "Example Product")
        self.assertEqual(record["manufacturer"], "Example Vendor")
        self.assertEqual(record["mpn"], "MPN-001")
        self.assertEqual(record["price"], "19.95")
        self.assertEqual(record["currency"], "AUD")
        self.assertEqual(record["stock"], 12)
        self.assertIsNone(record["gtin"])
        self.assertIsNone(record["moq"])

        self.assertEqual(summary["eligible_rows"], 1)
        self.assertEqual(summary["profile"], "dickerdata")

    def test_preserves_original_source_columns(self):
        source = make_row(
            PrimaryCategory="Networking",
            RRPEx="39.95",
            ETA="2026-12-01",
        )

        records, _ = adapt_records([source])
        record = records[0]

        self.assertEqual(record["PrimaryCategory"], "Networking")
        self.assertEqual(record["RRPEx"], "39.95")
        self.assertEqual(record["ETA"], "2026-12-01")
        self.assertEqual(record["DealerEx"], "19.95")

    def test_unlimited_stock_sentinel_becomes_none(self):
        records, summary = adapt_records([
            make_row(StockAvailable=str(UNLIMITED_STOCK_SENTINEL))
        ])

        self.assertIsNone(records[0]["stock"])
        self.assertEqual(summary["unlimited_stock_sentinel_rows"], 1)

    def test_sentinel_availability_is_unlimited(self):
        records, summary = adapt_records([
            make_row(StockAvailable=str(UNLIMITED_STOCK_SENTINEL))
        ])
        self.assertEqual(records[0]["availability_state"], "UNLIMITED")
        self.assertIsNone(records[0]["stock"])
        self.assertEqual(summary["sentinel_availability_state"], "UNLIMITED")

    def test_positive_stock_availability_is_finite(self):
        records, _ = adapt_records([make_row(StockAvailable="7")])
        self.assertEqual(records[0]["availability_state"], "FINITE")

    def test_zero_stock_availability_is_unavailable(self):
        records, _ = adapt_records([make_row(StockAvailable="0")])
        self.assertEqual(records[0]["availability_state"], "UNAVAILABLE")

    def test_negative_stock_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "cannot be negative"):
            adapt_records([make_row(StockAvailable="-1")])

    def test_zero_stock_remains_zero(self):
        records, summary = adapt_records([
            make_row(StockAvailable="0")
        ])

        self.assertEqual(records[0]["stock"], 0)
        self.assertEqual(summary["unlimited_stock_sentinel_rows"], 0)

    def test_excludes_bundle_special_and_non_active_rows(self):
        rows = [
            make_row(StockCode="STOCK-1"),
            make_row(
                StockCode="BUNDLE-1",
                Type="BundledItem",
            ),
            make_row(
                StockCode="SPECIAL-1",
                Type="SpecialItem",
            ),
            make_row(
                StockCode="TERM-1",
                Status="T",
            ),
            make_row(
                StockCode="UNKNOWN-1",
                Status="N",
            ),
        ]

        records, summary = adapt_records(rows)

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["sku"], "STOCK-1")
        self.assertEqual(summary["source_rows"], 5)
        self.assertEqual(summary["eligible_rows"], 1)
        self.assertEqual(summary["excluded_rows"], 4)
        self.assertEqual(summary["excluded_by_type"]["BundledItem"], 1)
        self.assertEqual(summary["excluded_by_type"]["SpecialItem"], 1)
        self.assertEqual(summary["excluded_by_status"]["T"], 1)
        self.assertEqual(summary["excluded_by_status"]["N"], 1)

    def test_case_and_whitespace_are_normalized_for_eligibility(self):
        records, _ = adapt_records([
            make_row(Status=" a ", Type=" stockeditem ")
        ])

        self.assertEqual(len(records), 1)

    def test_missing_required_commercial_value_rejected(self):
        with self.assertRaisesRegex(ValueError, "DealerEx"):
            adapt_records([make_row(DealerEx="")])

    def test_missing_stock_rejected(self):
        with self.assertRaisesRegex(ValueError, "StockAvailable"):
            adapt_records([make_row(StockAvailable="")])

    def test_non_integer_stock_rejected(self):
        with self.assertRaisesRegex(ValueError, "not an integer"):
            adapt_records([make_row(StockAvailable="unknown")])

    def test_schema_drift_rejected(self):
        row = make_row()
        row.pop("BundledItem5")

        with self.assertRaisesRegex(ValueError, "schema mismatch"):
            adapt_records([row])

    def test_no_eligible_rows_rejected(self):
        with self.assertRaisesRegex(ValueError, "no eligible"):
            adapt_records([
                make_row(Status="T"),
                make_row(Type="BundledItem"),
            ])

    def test_empty_input_rejected(self):
        with self.assertRaisesRegex(ValueError, "no source records"):
            adapt_records([])


if __name__ == "__main__":
    unittest.main()
