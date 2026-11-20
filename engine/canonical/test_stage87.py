
import unittest
import json

from importer.engine.canonical import supplier_import as si


VALID = {
    "sku": "TEST-001",
    "name": "Test Product",
    "gtin": "4006381333931",
    "manufacturer": "Example Manufacturer",
    "price": "19.95",
    "currency": "AUD",
    "stock": "10",
}


class FeedParserTests(unittest.TestCase):

    def test_csv_parsing(self):
        data = (
            "sku,name,gtin\n"
            "TEST-001,Product One,4006381333931\n"
            "TEST-002,Product Two,4006381333931\n"
        )

        records = si.parse_feed(data, "CSV")
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["sku"], "TEST-001")

    def test_csv_utf8_bom(self):
        data = (
            "\ufeffsku,name,gtin\n"
            "TEST-001,Product One,4006381333931\n"
        )

        records = si.parse_feed(data, "CSV")
        self.assertIn("sku", records[0])


    def test_csv_utf8_bytes_bom(self):
        data = bytes.fromhex(
            "efbbbf" + "736b752c6e616d650a" +
            "544553542d3030322c427974657320424f4d2050726f647563740a"
        )
        records = si.parse_feed(data, "CSV")
        self.assertEqual(records[0]["sku"], "TEST-002")
        self.assertEqual(records[0]["name"], "Bytes BOM Product")

    def test_csv_duplicate_headers_rejected(self):
        data = "sku,SKU,name\n1,2,Product\n"

        with self.assertRaises(ValueError):
            si.parse_feed(data, "CSV")

    def test_csv_empty_feed_rejected(self):
        with self.assertRaises(ValueError):
            si.parse_feed("sku,name\n", "CSV")

    def test_csv_extra_columns_rejected(self):
        with self.assertRaises(ValueError):
            si.parse_feed(
                "sku,name\nTEST-001,Product,EXTRA\n",
                "CSV",
            )

    def test_json_products(self):
        data = json.dumps({
            "products": [VALID]
        })

        records = si.parse_feed(data, "JSON")
        self.assertEqual(len(records), 1)

    def test_json_items(self):
        data = json.dumps({
            "items": [VALID]
        })

        records = si.parse_feed(data, "JSON")
        self.assertEqual(len(records), 1)

    def test_json_invalid_record_rejected(self):
        with self.assertRaises(ValueError):
            si.parse_feed(
                '{"products": [null]}',
                "JSON",
            )

    def test_json_malformed_rejected(self):
        with self.assertRaises(ValueError):
            si.parse_feed('{"products": [}', "JSON")

    def test_xml_products(self):
        data = """
        <products>
            <product>
                <sku>TEST-001</sku>
                <name>Test Product</name>
            </product>
            <product>
                <sku>TEST-002</sku>
                <name>Second Product</name>
            </product>
        </products>
        """

        records = si.parse_feed(data, "XML")
        self.assertEqual(len(records), 2)

    def test_xml_namespace(self):
        data = """
        <products xmlns="urn:test">
            <product>
                <sku>TEST-001</sku>
                <name>Test Product</name>
            </product>
        </products>
        """

        records = si.parse_feed(data, "XML")
        self.assertEqual(records[0]["sku"], "TEST-001")

    def test_xml_malformed_rejected(self):
        with self.assertRaises(ValueError):
            si.parse_feed("<products>", "XML")

    def test_empty_feed_rejected(self):
        with self.assertRaises(ValueError):
            si.parse_feed("", "JSON")

    def test_unsupported_format_rejected(self):
        with self.assertRaises(ValueError):
            si.parse_feed("data", "XLSX")


class NormalizationTests(unittest.TestCase):

    def test_valid_record(self):
        record = si.normalize_record(VALID)

        self.assertEqual(record["supplier_sku"], "TEST-001")
        self.assertEqual(str(record["supplier_price"]), "19.95")
        self.assertEqual(record["currency"], "AUD")
        self.assertEqual(record["stock_quantity"], 10)

    def test_sku_normalization(self):
        raw = dict(VALID, sku=" test-001 ")
        record = si.normalize_record(raw)

        self.assertEqual(record["supplier_sku"], "TEST-001")

    def test_empty_price_becomes_none(self):
        raw = dict(VALID, price="")
        raw["currency"] = ""

        record = si.normalize_record(raw)
        self.assertIsNone(record["supplier_price"])
        self.assertIsNone(record["currency"])

    def test_currency_without_price_rejected(self):
        raw = dict(VALID, price="")
        raw["currency"] = "AUD"

        with self.assertRaises(ValueError):
            si.normalize_record(raw)

    def test_price_without_currency_rejected(self):
        raw = dict(VALID, currency="")

        with self.assertRaises(ValueError):
            si.normalize_record(raw)

    def test_invalid_price_rejected(self):
        raw = dict(VALID, price="not-a-price")

        with self.assertRaises(ValueError):
            si.normalize_record(raw)

    def test_negative_price_rejected(self):
        raw = dict(VALID, price="-1")

        with self.assertRaises(ValueError):
            si.normalize_record(raw)

    def test_invalid_gtin_rejected(self):
        raw = dict(VALID, gtin="4006381333932")

        with self.assertRaises(ValueError):
            si.normalize_record(raw)

    def test_missing_sku_rejected(self):
        raw = dict(VALID, sku="")

        with self.assertRaises(ValueError):
            si.normalize_record(raw)

    def test_missing_name_rejected(self):
        raw = dict(VALID, name="")

        with self.assertRaises(ValueError):
            si.normalize_record(raw)

    def test_negative_stock_rejected(self):
        raw = dict(VALID, stock="-1")

        with self.assertRaises(ValueError):
            si.normalize_record(raw)

    def test_boolean_stock_rejected(self):
        raw = dict(VALID, stock=True)

        with self.assertRaises(ValueError):
            si.normalize_record(raw)

    def test_invalid_moq_rejected(self):
        raw = dict(VALID, moq="0")

        with self.assertRaises(ValueError):
            si.normalize_record(raw)

    def test_custom_mapping(self):
        raw = {
            "StockCode": "ABC-001",
            "StockDescription": "Mapped Product",
            "GTIN": "4006381333931",
        }

        mapping = {
            "supplier_sku": "StockCode",
            "product_name": "StockDescription",
            "gtin": "GTIN",
        }

        record = si.normalize_record(raw, mapping)

        self.assertEqual(record["supplier_sku"], "ABC-001")


class FingerprintTests(unittest.TestCase):

    def test_fingerprint_deterministic(self):
        first = si.calculate_fingerprint(
            "supplier-a",
            [VALID],
        )

        second = si.calculate_fingerprint(
            "supplier-a",
            [VALID],
        )

        self.assertEqual(first, second)

    def test_fingerprint_independent_of_row_order(self):
        second_record = dict(
            VALID,
            sku="TEST-002",
        )

        first = si.calculate_fingerprint(
            "supplier-a",
            [VALID, second_record],
        )

        second = si.calculate_fingerprint(
            "supplier-a",
            [second_record, VALID],
        )

        self.assertEqual(first, second)

    def test_fingerprint_supplier_specific(self):
        first = si.calculate_fingerprint(
            "supplier-a",
            [VALID],
        )

        second = si.calculate_fingerprint(
            "supplier-b",
            [VALID],
        )

        self.assertNotEqual(first, second)

    def test_invalid_records_remain_fingerprintable(self):
        first = si.calculate_fingerprint(
            "supplier-a",
            [{"sku": "", "name": ""}],
        )

        second = si.calculate_fingerprint(
            "supplier-a",
            [{"sku": "", "name": ""}],
        )

        self.assertEqual(first, second)

    def test_empty_records_rejected(self):
        with self.assertRaises(ValueError):
            si.calculate_fingerprint("supplier-a", [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
