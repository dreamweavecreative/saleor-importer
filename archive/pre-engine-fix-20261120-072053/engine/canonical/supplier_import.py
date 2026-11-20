
import csv
import io
import json
import hashlib
import xml.etree.ElementTree as ET

from decimal import Decimal, InvalidOperation

from django.db import (
    connection,
    transaction,
    IntegrityError,
)

from .identity import (
    normalize_gtin,
    normalize_mpn,
    normalize_manufacturer,
)

from .reconciliation import (
    resolve_identity,
    register_identity,
    IdentityConflict,
)

from .repository import CanonicalRepository

from .supplier_offers import SupplierOfferRepository


class DuplicateFeedError(Exception):
    """Feed has already been imported for this supplier."""


def parse_feed(data, feed_format):
    """Parse CSV, JSON or XML into a validated list of source records."""

    if isinstance(data, bytes):
        try:
            data = data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError("Feed must use UTF-8 encoding") from exc

    if not isinstance(data, str):
        raise ValueError("Feed data must be text or bytes")

    data = data.lstrip(chr(0xfeff)).strip()

    if not data:
        raise ValueError("Empty supplier feed")

    feed_format = str(feed_format or "").strip().upper()

    if feed_format == "CSV":
        try:
            stream = io.StringIO(data, newline="")
            reader = csv.DictReader(stream)

            headers = reader.fieldnames

            if not headers:
                raise ValueError("CSV header row required")

            headers = [str(h).strip() if h else "" for h in headers]

            if any(not h for h in headers):
                raise ValueError("CSV contains an empty column header")

            normalized_headers = [h.casefold() for h in headers]

            if len(normalized_headers) != len(set(normalized_headers)):
                raise ValueError("CSV contains duplicate column headers")

            reader.fieldnames = headers
            records = []

            for row_number, row in enumerate(reader, 2):
                if None in row:
                    raise ValueError(
                        f"CSV row {row_number} contains extra columns"
                    )

                if not any(
                    value is not None and str(value).strip()
                    for value in row.values()
                ):
                    continue

                records.append(row)

            if not records:
                raise ValueError("CSV contains no product records")

            return records

        except csv.Error as exc:
            raise ValueError("Malformed CSV feed") from exc

    if feed_format == "JSON":
        try:
            parsed = json.loads(data)
        except json.JSONDecodeError as exc:
            raise ValueError("Malformed JSON feed") from exc

        if isinstance(parsed, dict):
            parsed = parsed.get("products", parsed.get("items"))

        if not isinstance(parsed, list) or not parsed:
            raise ValueError("JSON product list required")

        if not all(isinstance(item, dict) for item in parsed):
            raise ValueError("Every JSON product must be an object")

        return parsed

    if feed_format == "XML":
        try:
            root = ET.fromstring(data)
        except ET.ParseError as exc:
            raise ValueError("Malformed XML feed") from exc

        def local_name(tag):
            return str(tag).split("}")[-1].lower()

        product_tags = {"product", "item", "record"}

        elements = [
            element
            for element in root.iter()
            if local_name(element.tag) in product_tags
        ]

        if local_name(root.tag) in product_tags and root not in elements:
            elements.insert(0, root)

        records = []

        for element in elements:
            record = {}

            for child in element:
                if len(child) == 0:
                    key = str(child.tag).split("}")[-1]

                    if key in record:
                        raise ValueError(
                            "XML product contains duplicate field: " + key
                        )

                    record[key] = child.text.strip() if child.text else None

            if record:
                records.append(record)

        if not records:
            raise ValueError("XML contains no product records")

        return records

    raise ValueError("Unsupported feed format")



MAPPING = {
    "supplier_sku": "sku",
    "product_name": "name",
    "manufacturer": "manufacturer",
    "brand": "brand",
    "gtin": "gtin",
    "mpn": "mpn",
    "supplier_price": "price",
    "currency": "currency",
    "stock_quantity": "stock",
    "minimum_order_quantity": "moq",
}


def normalize_record(raw, mapping=None):
    """Convert a supplier source record into canonical import fields."""

    if not isinstance(raw, dict):
        raise ValueError("Product record must be an object")

    mapping = MAPPING if mapping is None else mapping

    if not isinstance(mapping, dict):
        raise ValueError("Field mapping must be a dictionary")

    record = {}

    for target, source in mapping.items():
        value = raw.get(source)

        if isinstance(value, str):
            value = value.strip()

        if value == "":
            value = None

        record[target] = value

    record["supplier_sku"] = str(
        record.get("supplier_sku") or ""
    ).strip().upper()

    record["product_name"] = str(
        record.get("product_name") or ""
    ).strip()

    if not record["supplier_sku"]:
        raise ValueError("Supplier SKU required")

    if not record["product_name"]:
        raise ValueError("Product name required")

    record["manufacturer"] = (
        normalize_manufacturer(record.get("manufacturer")) or None
    )

    record["brand"] = (
        str(record["brand"]).strip() or None
        if record.get("brand") is not None
        else None
    )

    record["gtin"] = (
        normalize_gtin(record["gtin"])
        if record.get("gtin")
        else None
    )

    record["mpn"] = (
        normalize_mpn(record["mpn"])
        if record.get("mpn")
        else None
    )

    if not record["gtin"] and not record["mpn"]:
        raise ValueError("GTIN or MPN required")

    if record["mpn"] and not record["manufacturer"]:
        raise ValueError("Manufacturer required with MPN")

    price = record.get("supplier_price")

    if price is not None:
        try:
            price = Decimal(str(price))
        except (InvalidOperation, ValueError):
            raise ValueError("Invalid supplier price") from None

        if not price.is_finite() or price < 0:
            raise ValueError("Invalid supplier price")

    record["supplier_price"] = price

    currency = record.get("currency")

    if currency is not None:
        currency = str(currency).strip().upper()

        if len(currency) != 3 or not currency.isalpha():
            raise ValueError("Invalid currency")

    if price is not None and not currency:
        raise ValueError("Currency required with supplier price")

    if price is None and currency:
        raise ValueError("Currency cannot be supplied without a price")

    record["currency"] = currency or None

    for field in ("stock_quantity", "minimum_order_quantity"):
        value = record.get(field)

        if value is None:
            continue

        if isinstance(value, bool):
            raise ValueError("Invalid " + field)

        try:
            integer = int(str(value))
        except (ValueError, TypeError):
            raise ValueError("Invalid " + field) from None

        if str(value).strip() != str(integer):
            raise ValueError("Invalid " + field)

        if integer < 0:
            raise ValueError("Invalid " + field)

        if field == "minimum_order_quantity" and integer == 0:
            raise ValueError("Invalid minimum order quantity")

        record[field] = integer

    return record



def _canonical_json(value):

    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def calculate_fingerprint(supplier_id, records):
    """
    Produce an order-independent fingerprint.

    Valid records use canonical normalized values.
    Invalid records remain fingerprintable in their raw form,
    allowing malformed batches to be recorded and diagnosed.
    """

    supplier_id = str(supplier_id or "").strip().lower()

    if not supplier_id:
        raise ValueError("Supplier required")

    if not isinstance(records, (list, tuple)) or not records:
        raise ValueError("Empty supplier feed")

    normalized = []

    for raw in records:
        try:
            item = normalize_record(raw)
        except (ValueError, TypeError, InvalidOperation):
            item = {"invalid_record": raw}

        normalized.append(item)

    normalized.sort(key=_canonical_json)

    payload = {
        "supplier_id": supplier_id,
        "records": normalized,
    }

    return hashlib.sha256(
        _canonical_json(payload).encode("utf-8")
    ).hexdigest()



def create_import_batch(supplier_id, feed_format, records):

    if not supplier_id or not str(supplier_id).strip():
        raise ValueError("Supplier required")

    if not records:
        raise ValueError("Empty supplier feed")

    supplier_id = str(supplier_id).strip().lower()
    feed_format = str(feed_format).upper()

    if feed_format not in ("CSV", "JSON", "XML"):
        raise ValueError("Unsupported feed format")

    fingerprint = calculate_fingerprint(
        supplier_id,
        records,
    )

    with transaction.atomic():

        with connection.cursor() as cursor:

            try:

                # Nested atomic creates a PostgreSQL savepoint.
                # A uniqueness violation therefore does not
                # poison the surrounding transaction.

                with transaction.atomic():

                    cursor.execute("""
                        INSERT INTO catalogue.supplier_import_batches (
                            supplier_id,
                            feed_format,
                            feed_fingerprint,
                            status,
                            total_rows
                        )
                        VALUES (%s,%s,%s,'PROCESSING',%s)
                        RETURNING id
                    """, [
                        supplier_id,
                        feed_format,
                        fingerprint,
                        len(records),
                    ])

                    batch_id = cursor.fetchone()[0]

            except IntegrityError:

                cursor.execute("""
                    SELECT id
                    FROM catalogue.supplier_import_batches
                    WHERE supplier_id = %s
                      AND feed_fingerprint = %s
                    LIMIT 1
                """, [
                    supplier_id,
                    fingerprint,
                ])

                existing = cursor.fetchone()

                raise DuplicateFeedError(
                    "Duplicate supplier feed. Existing batch: "
                    + str(existing[0] if existing else "unknown")
                )

            for index, raw in enumerate(records, 1):

                try:

                    record = normalize_record(raw)

                    cursor.execute("""
                        INSERT INTO catalogue.supplier_import_rows (
                            batch_id,
                            row_number,
                            supplier_sku,
                            product_name,
                            manufacturer,
                            brand,
                            gtin,
                            mpn,
                            supplier_price,
                            currency,
                            stock_quantity,
                            minimum_order_quantity,
                            raw_record,
                            status
                        )
                        VALUES (
                            %s,%s,%s,%s,%s,%s,%s,%s,
                            %s,%s,%s,%s,%s::jsonb,'PENDING'
                        )
                    """, [
                        str(batch_id),
                        index,
                        record["supplier_sku"],
                        record["product_name"],
                        record.get("manufacturer"),
                        record.get("brand"),
                        record.get("gtin"),
                        record.get("mpn"),
                        record.get("supplier_price"),
                        record.get("currency"),
                        record.get("stock_quantity"),
                        record.get("minimum_order_quantity"),
                        _canonical_json(raw),
                    ])

                except (ValueError, TypeError, InvalidOperation) as exc:

                    cursor.execute("""
                        INSERT INTO catalogue.supplier_import_rows (
                            batch_id,
                            row_number,
                            raw_record,
                            status,
                            error_message
                        )
                        VALUES (
                            %s,%s,%s::jsonb,'INVALID',%s
                        )
                    """, [
                        str(batch_id),
                        index,
                        _canonical_json(raw),
                        str(exc)[:2000],
                    ])

            return batch_id


def _process_row(cursor, batch_id, row):

    (
        row_id,
        supplier_sku,
        product_name,
        manufacturer,
        brand,
        gtin,
        mpn,
        price,
        currency,
        stock,
        moq,
        raw_record,
    ) = row

    supplier_id = None

    cursor.execute("""
        SELECT supplier_id
        FROM catalogue.supplier_import_batches
        WHERE id = %s
    """, [str(batch_id)])

    supplier_id = cursor.fetchone()[0]

    # Serialize ownership registration for the same supplier SKU.
    # This also protects imports arriving through different batches.

    lock_key = supplier_id + ":" + supplier_sku

    cursor.execute("""
        SELECT pg_advisory_xact_lock(
            hashtextextended(%s, 0)
        )
    """, [lock_key])

    product_id, method = resolve_identity(
        gtin=gtin,
        mpn=mpn,
        manufacturer=manufacturer,
    )

    if not product_id:

        cursor.execute("""
            UPDATE catalogue.supplier_import_rows
            SET status = 'UNMATCHED',
                matching_method = 'UNMATCHED',
                error_message = NULL
            WHERE id = %s
        """, [str(row_id)])

        return "UNMATCHED"

    # All following operations occur inside the row savepoint
    # established by reconcile_import_batch.

    register_identity(
        product_id,
        gtin=gtin,
        mpn=mpn,
        manufacturer=manufacturer,
    )

    CanonicalRepository.link_supplier(
        product_id,
        supplier_id,
        supplier_sku,
    )

    cursor.execute("""
        SELECT id, canonical_product_id
        FROM catalogue.product_supplier_links
        WHERE supplier_id = %s
          AND supplier_sku = %s
        FOR UPDATE
    """, [
        supplier_id,
        supplier_sku,
    ])

    link = cursor.fetchone()

    if not link:
        raise RuntimeError("Supplier link registration failed")

    supplier_link_id, linked_product_id = link

    if str(linked_product_id) != str(product_id):
        raise IdentityConflict(
            "Supplier SKU belongs to another canonical product"
        )

    if price is not None:

        SupplierOfferRepository.upsert(
            supplier_link_id=supplier_link_id,
            unit_cost=price,
            currency=currency,
            available_quantity=stock,
            minimum_order_quantity=moq or 1,
            raw_data=raw_record,
        )

    CanonicalRepository.record_reconciliation(
        product_id=product_id,
        supplier_id=supplier_id,
        supplier_sku=supplier_sku,
        matching_method=method,
        confidence=1,
        decision="MATCHED",
        details={
            "batch_id": str(batch_id),
            "row_id": str(row_id),
        },
    )

    cursor.execute("""
        UPDATE catalogue.supplier_import_rows
        SET status = 'MATCHED',
            canonical_product_id = %s,
            matching_method = %s,
            error_message = NULL
        WHERE id = %s
    """, [
        str(product_id),
        method,
        str(row_id),
    ])

    return "MATCHED"


def reconcile_import_batch(batch_id):

    counts = {
        "MATCHED": 0,
        "UNMATCHED": 0,
        "CONFLICT": 0,
        "INVALID": 0,
    }

    with transaction.atomic():

        with connection.cursor() as cursor:

            # One worker at a time may reconcile a batch.

            cursor.execute("""
                SELECT id, supplier_id
                FROM catalogue.supplier_import_batches
                WHERE id = %s
                FOR UPDATE
            """, [str(batch_id)])

            batch = cursor.fetchone()

            if not batch:
                raise ValueError("Import batch not found")

            supplier_id = batch[1]

            cursor.execute("""
                SELECT
                    id,
                    supplier_sku,
                    product_name,
                    manufacturer,
                    brand,
                    gtin,
                    mpn,
                    supplier_price,
                    currency,
                    stock_quantity,
                    minimum_order_quantity,
                    raw_record
                FROM catalogue.supplier_import_rows
                WHERE batch_id = %s
                  AND status = 'PENDING'
                ORDER BY row_number
                FOR UPDATE
            """, [str(batch_id)])

            rows = cursor.fetchall()

            for row in rows:

                row_id = row[0]

                try:

                    # Each row is an independent savepoint.
                    # A failed offer/link operation cannot leave
                    # a partially registered supplier relationship.

                    with transaction.atomic():

                        status = _process_row(
                            cursor,
                            batch_id,
                            row,
                        )

                    counts[status] += 1

                except IdentityConflict as exc:

                    cursor.execute("""
                        UPDATE catalogue.supplier_import_rows
                        SET status = 'CONFLICT',
                            error_message = %s
                        WHERE id = %s
                    """, [
                        str(exc)[:2000],
                        str(row_id),
                    ])

                    counts["CONFLICT"] += 1

                except (ValueError, InvalidOperation) as exc:

                    cursor.execute("""
                        UPDATE catalogue.supplier_import_rows
                        SET status = 'INVALID',
                            error_message = %s
                        WHERE id = %s
                    """, [
                        str(exc)[:2000],
                        str(row_id),
                    ])

                    counts["INVALID"] += 1

                except IntegrityError as exc:

                    cursor.execute("""
                        UPDATE catalogue.supplier_import_rows
                        SET status = 'CONFLICT',
                            error_message = %s
                        WHERE id = %s
                    """, [
                        str(exc)[:2000],
                        str(row_id),
                    ])

                    counts["CONFLICT"] += 1

            cursor.execute("""
                SELECT status, COUNT(*)
                FROM catalogue.supplier_import_rows
                WHERE batch_id = %s
                GROUP BY status
            """, [str(batch_id)])

            totals = dict(cursor.fetchall())

            counts = {
                "MATCHED": totals.get("MATCHED", 0),
                "UNMATCHED": totals.get("UNMATCHED", 0),
                "CONFLICT": totals.get("CONFLICT", 0),
                "INVALID": totals.get("INVALID", 0),
            }

            errors = counts["CONFLICT"] + counts["INVALID"]

            cursor.execute("""
                UPDATE catalogue.supplier_import_batches
                SET status = %s,
                    matched_rows = %s,
                    unmatched_rows = %s,
                    conflict_rows = %s,
                    invalid_rows = %s,
                    completed_at = NOW()
                WHERE id = %s
            """, [
                "COMPLETED_WITH_ERRORS" if errors
                else "COMPLETED",
                counts["MATCHED"],
                counts["UNMATCHED"],
                counts["CONFLICT"],
                counts["INVALID"],
                str(batch_id),
            ])

    return counts


def recover_import_batch(batch_id):

    """
    Recover a batch interrupted before reconciliation committed.

    Reconciliation uses one outer transaction, so unfinished
    row processing is rolled back atomically.
    """

    with transaction.atomic():

        with connection.cursor() as cursor:

            cursor.execute("""
                SELECT status
                FROM catalogue.supplier_import_batches
                WHERE id = %s
                FOR UPDATE
            """, [str(batch_id)])

            row = cursor.fetchone()

            if not row:
                raise ValueError("Import batch not found")

            if row[0] not in ("PROCESSING", "PENDING"):
                raise ValueError(
                    "Only unfinished batches can be recovered"
                )

            cursor.execute("""
                UPDATE catalogue.supplier_import_batches
                SET status = 'PENDING',
                    completed_at = NULL
                WHERE id = %s
            """, [str(batch_id)])

    return True


def retry_import_rows(batch_id, statuses=None):

    """
    Explicitly retry rows after identity or catalogue corrections.

    INVALID rows are deliberately excluded by default.
    """

    statuses = statuses or ("UNMATCHED", "CONFLICT")

    allowed = {"UNMATCHED", "CONFLICT"}

    if not statuses or not set(statuses).issubset(allowed):
        raise ValueError("Unsupported retry status")

    with transaction.atomic():

        with connection.cursor() as cursor:

            cursor.execute("""
                SELECT id
                FROM catalogue.supplier_import_batches
                WHERE id = %s
                FOR UPDATE
            """, [str(batch_id)])

            if not cursor.fetchone():
                raise ValueError("Import batch not found")

            cursor.execute("""
                UPDATE catalogue.supplier_import_rows
                SET status = 'PENDING',
                    canonical_product_id = NULL,
                    matching_method = NULL,
                    error_message = NULL
                WHERE batch_id = %s
                  AND status = ANY(%s)
            """, [
                str(batch_id),
                list(statuses),
            ])

            recovered = cursor.rowcount

            cursor.execute("""
                UPDATE catalogue.supplier_import_batches
                SET status = 'PENDING',
                    completed_at = NULL
                WHERE id = %s
            """, [str(batch_id)])

    return recovered
