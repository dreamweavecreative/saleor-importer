
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
from . import offer_scoped


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

        if len(currency) != 3 or not currency.isascii() or not currency.isalpha():
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




def stage_dickerdata_feed(path, supplier_id="dickerdata", feed_format="CSV", expected_fingerprint=None):
    """
    Bounded-memory DickerData staging.

    Pass 1 validates and normalizes eligible rows and externally sorts the
    normalized JSON records to reproduce calculate_fingerprint() exactly.

    Pass 2 inserts the batch and staged rows in one transaction, using
    bounded executemany batches. The source file is hashed before and after
    processing so a changed feed cannot be staged under a stale fingerprint.

    This function never reconciles, creates offers, or modifies Saleor data.
    """
    import csv
    import hashlib
    import heapq
    import os
    import tempfile
    from collections import Counter
    from pathlib import Path

    from importer.engine.dickerdata_adapter import (
        EXPECTED_COLUMNS,
        UNLIMITED_STOCK_SENTINEL,
        adapt_records,
    )

    supplier_id = str(supplier_id or "").strip().lower()
    feed_format = str(feed_format or "").strip().upper()

    if supplier_id != "dickerdata":
        raise ValueError("Streaming staging is restricted to dickerdata.")
    if feed_format != "CSV":
        raise ValueError("DickerData streaming staging supports CSV only.")

    feed_path = Path(path).expanduser()
    if not feed_path.is_file() or feed_path.stat().st_size <= 0:
        raise ValueError("Feed path is missing, not a regular file, or empty.")

    CHUNK_RECORDS = 1000
    INSERT_BATCH = 500

    def file_sha256():
        digest = hashlib.sha256()
        with feed_path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    def is_blank_row(row):
        return not any(
            value is not None and str(value).strip()
            for value in row.values()
        )

    def eligible(row):
        return (
            str(row.get("Status") or "").strip().upper() == "A"
            and str(row.get("Type") or "").strip().casefold() == "stockeditem"
        )

    source_digest_before = file_sha256()
    source_rows = 0
    eligible_rows = 0
    excluded_rows = 0
    sentinel_rows = 0
    status_counts = Counter()
    type_counts = Counter()
    chunk_values = []

    with tempfile.TemporaryDirectory(prefix="dickerdata_stage_") as temp_dir:
        temp_path = Path(temp_dir)
        chunk_paths = []

        def flush_sort_chunk():
            if not chunk_values:
                return
            chunk_values.sort()
            chunk_file = temp_path / f"sort_{len(chunk_paths):06d}.jsonl"
            with chunk_file.open("w", encoding="utf-8", newline="\n") as out:
                for value in chunk_values:
                    out.write(value)
                    out.write("\n")
            chunk_paths.append(chunk_file)
            chunk_values.clear()

        # PASS 1: validate, normalize and externally sort fingerprint records.
        with feed_path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            headers = reader.fieldnames or []

            clean_headers = [str(h).strip() if h else "" for h in headers]
            if len(clean_headers) != len(set(h.casefold() for h in clean_headers)):
                raise ValueError("DickerData CSV contains duplicate headers.")
            if set(clean_headers) != set(EXPECTED_COLUMNS):
                raise ValueError(
                    "DickerData CSV schema mismatch: "
                    f"missing={sorted(set(EXPECTED_COLUMNS)-set(clean_headers))}; "
                    f"unexpected={sorted(set(clean_headers)-set(EXPECTED_COLUMNS))}"
                )
            reader.fieldnames = clean_headers

            for row in reader:
                if is_blank_row(row):
                    continue
                source_rows += 1

                if None in row:
                    raise ValueError(
                        f"CSV row {source_rows + 1} contains extra columns."
                    )

                status = str(row.get("Status") or "").strip().upper()
                item_type = str(row.get("Type") or "").strip()
                status_counts[status or "<blank>"] += 1
                type_counts[item_type or "<blank>"] += 1

                if not eligible(row):
                    excluded_rows += 1
                    continue

                adapted_rows, adapter_summary = adapt_records([row])
                if len(adapted_rows) != 1:
                    raise RuntimeError("Eligible source row did not adapt to one record.")

                adapted = adapted_rows[0]
                normalized = normalize_record(adapted)

                # Match calculate_fingerprint(): sort canonical JSON strings.
                chunk_values.append(_canonical_json(normalized))
                eligible_rows += 1

                if str(row.get("StockAvailable") or "").strip() == str(UNLIMITED_STOCK_SENTINEL):
                    sentinel_rows += 1

                if len(chunk_values) >= CHUNK_RECORDS:
                    flush_sort_chunk()

        if not eligible_rows:
            raise ValueError("DickerData feed contains no eligible stocked items.")

        flush_sort_chunk()

        # Reproduce the exact JSON serialization shape used by
        # calculate_fingerprint(), without materializing the records array.
        fingerprint_hash = hashlib.sha256()
        fingerprint_hash.update(b'{"records":[')

        first = True
        iterators = [
            chunk.open("r", encoding="utf-8", newline="")
            for chunk in chunk_paths
        ]
        try:
            for line in heapq.merge(*iterators):
                value = line.rstrip("\n")
                if not first:
                    fingerprint_hash.update(b",")
                fingerprint_hash.update(value.encode("utf-8"))
                first = False
        finally:
            for iterator in iterators:
                iterator.close()

        fingerprint_hash.update(b'],"supplier_id":')
        fingerprint_hash.update(_canonical_json(supplier_id).encode("utf-8"))
        fingerprint_hash.update(b"}")
        fingerprint = fingerprint_hash.hexdigest()

        if expected_fingerprint and fingerprint != expected_fingerprint:
            raise RuntimeError(
                "Fingerprint guard rejected feed before database writes: "
                f"expected {expected_fingerprint}, calculated {fingerprint}"
            )

        # Ensure the feed was not modified while fingerprinting.
        if file_sha256() != source_digest_before:
            raise RuntimeError("Feed changed during fingerprint calculation.")

        inserted_rows = 0
        batch_id = None

        # PASS 2: one atomic batch insert and bounded row inserts.
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("SET LOCAL lock_timeout = '15s'")
                cursor.execute("SET LOCAL statement_timeout = '15min'")

                try:
                    with transaction.atomic():
                        cursor.execute("""
                            INSERT INTO catalogue.supplier_import_batches (
                                supplier_id,
                                feed_format,
                                feed_fingerprint,
                                status,
                                total_rows
                            )
                            VALUES (%s, %s, %s, 'PROCESSING', %s)
                            RETURNING id
                        """, [
                            supplier_id,
                            feed_format,
                            fingerprint,
                            eligible_rows,
                        ])
                        batch_id = cursor.fetchone()[0]

                except IntegrityError:
                    cursor.execute("""
                        SELECT id
                        FROM catalogue.supplier_import_batches
                        WHERE supplier_id = %s
                          AND feed_fingerprint = %s
                        LIMIT 1
                    """, [supplier_id, fingerprint])
                    existing = cursor.fetchone()
                    raise DuplicateFeedError(
                        "Duplicate supplier feed. Existing batch: "
                        + str(existing[0] if existing else "unknown")
                    )

                pending = []
                row_number = 0

                with feed_path.open("r", encoding="utf-8-sig", newline="") as handle:
                    reader = csv.DictReader(handle)
                    reader.fieldnames = [
                        str(h).strip() if h else ""
                        for h in (reader.fieldnames or [])
                    ]

                    for source_row in reader:
                        if is_blank_row(source_row):
                            continue
                        if None in source_row:
                            raise ValueError(
                                "CSV row contains extra columns during insert pass."
                            )
                        if not eligible(source_row):
                            continue

                        adapted_rows, _ = adapt_records([source_row])
                        if len(adapted_rows) != 1:
                            raise RuntimeError(
                                "Eligible source row failed adaptation on insert pass."
                            )

                        adapted = adapted_rows[0]
                        record = normalize_record(adapted)
                        row_number += 1

                        pending.append((
                            str(batch_id),
                            row_number,
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
                            _canonical_json(adapted),
                        ))

                        if len(pending) >= INSERT_BATCH:
                            cursor.executemany("""
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
                            """, pending)
                            inserted_rows += len(pending)
                            pending.clear()

                    if pending:
                        cursor.executemany("""
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
                        """, pending)
                        inserted_rows += len(pending)
                        pending.clear()

                if row_number != eligible_rows or inserted_rows != eligible_rows:
                    raise RuntimeError(
                        "Staging row-count mismatch: "
                        f"fingerprint_pass={eligible_rows}, "
                        f"insert_pass={row_number}, inserted={inserted_rows}"
                    )

                cursor.execute("""
                    SELECT
                        COUNT(*),
                        COUNT(*) FILTER (WHERE status = 'PENDING'),
                        COUNT(*) FILTER (WHERE status <> 'PENDING')
                    FROM catalogue.supplier_import_rows
                    WHERE batch_id = %s
                """, [str(batch_id)])
                actual_total, pending_total, non_pending = cursor.fetchone()

                if (
                    actual_total != eligible_rows
                    or pending_total != eligible_rows
                    or non_pending != 0
                ):
                    raise RuntimeError(
                        "Post-insert verification failed: "
                        f"total={actual_total}, pending={pending_total}, "
                        f"non_pending={non_pending}"
                    )

                # Detect a feed replacement/change during the DB insert pass.
                if file_sha256() != source_digest_before:
                    raise RuntimeError(
                        "Feed changed during staging; transaction will roll back."
                    )

        return {
            "batch_id": str(batch_id),
            "status": "PROCESSING",
            "supplier_id": supplier_id,
            "feed_format": feed_format,
            "fingerprint": fingerprint,
            "source_rows": source_rows,
            "eligible_rows": eligible_rows,
            "excluded_rows": excluded_rows,
            "inserted_rows": inserted_rows,
            "unlimited_stock_sentinel_rows": sentinel_rows,
            "source_status_counts": dict(sorted(status_counts.items())),
            "source_type_counts": dict(sorted(type_counts.items())),
            "memory_strategy": {
                "feed_processing": "two-pass streaming CSV",
                "fingerprint": "external disk-backed sort",
                "fingerprint_chunk_records": CHUNK_RECORDS,
                "database_insert_batch": INSERT_BATCH,
                "full_feed_loaded_into_memory": False,
            },
            "reconciliation_started": False,
            "offers_created": False,
        }


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


def _resolve_dickerdata_variant(cursor, supplier_sku):
    """
    Resolve DickerData StockCode to one exact Saleor variant and its
    canonical variant-level record.

    Returns (canonical_product_id, matching_method), or
    (None, "UNMATCHED") when either bridge is absent.

    Ambiguous Saleor SKUs, duplicate canonical variant bridges, and
    inconsistent Saleor product/variant associations are conflicts.
    No MPN or fuzzy fallback is permitted.
    """
    from .reconciliation import IdentityConflict

    sku = str(supplier_sku or "").strip().upper()
    if not sku:
        raise ValueError("DickerData StockCode is required")

    cursor.execute("""
        SELECT
            v.id,
            v.product_id
        FROM public.product_productvariant v
        WHERE UPPER(BTRIM(v.sku)) = %s
    """, [sku])

    variants = cursor.fetchall()

    if not variants:
        return None, "UNMATCHED"

    if len(variants) != 1:
        raise IdentityConflict(
            "DickerData StockCode resolves to multiple Saleor variants: "
            + sku
        )

    saleor_variant_id, saleor_product_id = variants[0]

    cursor.execute("""
        SELECT
            id,
            saleor_product_id,
            saleor_variant_id
        FROM catalogue.canonical_products
        WHERE saleor_variant_id = %s
    """, [str(saleor_variant_id)])

    canonical_rows = cursor.fetchall()

    if not canonical_rows:
        return None, "UNMATCHED"

    if len(canonical_rows) != 1:
        raise IdentityConflict(
            "Multiple canonical records reference Saleor variant: "
            + str(saleor_variant_id)
        )

    canonical_id, canonical_saleor_product_id, canonical_saleor_variant_id = (
        canonical_rows[0]
    )

    if str(canonical_saleor_variant_id) != str(saleor_variant_id):
        raise IdentityConflict(
            "Canonical Saleor variant bridge does not match resolved SKU"
        )

    if str(canonical_saleor_product_id) != str(saleor_product_id):
        raise IdentityConflict(
            "Canonical Saleor product/variant association is inconsistent"
        )

    return canonical_id, "DICKERDATA_STOCKCODE"


def _batch_completion_status(counts):
    """
    A batch is cleanly completed only when every row is matched.
    Unmatched rows require investigation and must not appear successful.
    """
    errors = (
        int(counts.get("UNMATCHED", 0))
        + int(counts.get("CONFLICT", 0))
        + int(counts.get("INVALID", 0))
    )
    return "COMPLETED_WITH_ERRORS" if errors else "COMPLETED"


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

    if str(supplier_id).strip().lower() == "dickerdata":
        # DickerData StockCode is the supplier's exact Saleor variant SKU.
        # Do not use manufacturer-scoped MPN as a fallback: MPN collisions
        # across different Saleor products have been observed in this feed.
        product_id, method = _resolve_dickerdata_variant(
            cursor,
            supplier_sku,
        )
    else:
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

    # DickerData identity is established by the Saleor variant SKU bridge.
    # Do not register VendorStockCode/MPN as a canonical identity: it is not
    # sufficiently unique for this supplier's catalogue.
    if str(supplier_id).strip().lower() != "dickerdata":
        register_identity(
            product_id,
            gtin=gtin,
            mpn=mpn,
            manufacturer=manufacturer,
        )

    try:
        CanonicalRepository.link_supplier(
            product_id,
            supplier_id,
            supplier_sku,
        )
    except ValueError as exc:
        if str(exc) == (
            "Supplier SKU already linked to another canonical product"
        ):
            raise IdentityConflict(str(exc)) from exc
        raise

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

        offer_scoped.upsert_scoped(
            cursor,
            batch_id,
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


def reconcile_import_batch(batch_id, chunk_size=250, max_chunks=None):
    """
    Reconcile pending rows in bounded, independently committed chunks.

    The batch row is locked for the duration of each chunk. This serializes
    row selection between concurrent reconcilers without holding one
    transaction open for the entire batch.

    Unexpected exceptions roll back only the current chunk. Previously
    committed chunks remain persisted, and a later invocation resumes from
    the remaining PENDING rows.

    max_chunks is a test/control parameter. Production callers should omit it.
    """

    if isinstance(chunk_size, bool) or not isinstance(chunk_size, int) or chunk_size < 1:
        raise ValueError("chunk_size must be a positive integer")

    if max_chunks is not None:
        if isinstance(max_chunks, bool) or not isinstance(max_chunks, int) or max_chunks < 1:
            raise ValueError("max_chunks must be a positive integer when supplied")

    batch_id = str(batch_id)
    chunks_processed = 0

    while True:

        with transaction.atomic():

            with connection.cursor() as cursor:

                # Only one reconciler may select/process rows from this batch
                # during this chunk transaction.
                cursor.execute("""
                    SELECT id, supplier_id
                    FROM catalogue.supplier_import_batches
                    WHERE id = %s
                    FOR UPDATE
                """, [batch_id])

                batch = cursor.fetchone()

                if not batch:
                    raise ValueError("Import batch not found")

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
                    LIMIT %s
                    FOR UPDATE
                """, [batch_id, chunk_size])

                rows = cursor.fetchall()

                for row in rows:

                    row_id = row[0]

                    try:
                        # Preserve per-row atomicity within the chunk.
                        with transaction.atomic():
                            status = _process_row(cursor, batch_id, row)

                        if status not in ("MATCHED", "UNMATCHED"):
                            raise RuntimeError(
                                "Unexpected reconciliation status: " + str(status)
                            )

                    except IdentityConflict as exc:
                        cursor.execute("""
                            UPDATE catalogue.supplier_import_rows
                            SET status = 'CONFLICT',
                                error_message = %s
                            WHERE id = %s
                        """, [str(exc)[:2000], str(row_id)])

                    except (ValueError, InvalidOperation) as exc:
                        cursor.execute("""
                            UPDATE catalogue.supplier_import_rows
                            SET status = 'INVALID',
                                error_message = %s
                            WHERE id = %s
                        """, [str(exc)[:2000], str(row_id)])

                    except IntegrityError as exc:
                        cursor.execute("""
                            UPDATE catalogue.supplier_import_rows
                            SET status = 'CONFLICT',
                                error_message = %s
                            WHERE id = %s
                        """, [str(exc)[:2000], str(row_id)])

                # Recalculate from persisted states, including earlier
                # committed chunks and any previous interrupted invocation.
                cursor.execute("""
                    SELECT status, COUNT(*)
                    FROM catalogue.supplier_import_rows
                    WHERE batch_id = %s
                    GROUP BY status
                """, [batch_id])

                totals = dict(cursor.fetchall())

                counts = {
                    "MATCHED": totals.get("MATCHED", 0),
                    "UNMATCHED": totals.get("UNMATCHED", 0),
                    "CONFLICT": totals.get("CONFLICT", 0),
                    "INVALID": totals.get("INVALID", 0),
                }

                pending_rows = totals.get("PENDING", 0)

                if pending_rows:
                    batch_status = "PROCESSING"
                    completed_at_sql = "NULL"
                else:
                    batch_status = _batch_completion_status(counts)
                    completed_at_sql = "NOW()"

                cursor.execute(f"""
                    UPDATE catalogue.supplier_import_batches
                    SET status = %s,
                        matched_rows = %s,
                        unmatched_rows = %s,
                        conflict_rows = %s,
                        invalid_rows = %s,
                        completed_at = {completed_at_sql}
                    WHERE id = %s
                """, [
                    batch_status,
                    counts["MATCHED"],
                    counts["UNMATCHED"],
                    counts["CONFLICT"],
                    counts["INVALID"],
                    batch_id,
                ])

        # The chunk transaction has committed here.
        if rows:
            chunks_processed += 1

        if pending_rows == 0:
            break

        if not rows:
            # Defensive stop: the batch lock was held while checking pending
            # rows, so an empty selection with pending rows indicates an
            # unexpected database/state inconsistency.
            raise RuntimeError(
                "No pending rows selected, but batch still reports pending rows"
            )

        if max_chunks is not None and chunks_processed >= max_chunks:
            break

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
