"""Supplier-independent snapshot ingest.

Calls only the SupplierAdapter contract plus the authoritative snapshot
engine and offer writer. Contains no supplier field names or sentinels.

Flow: stage rows -> declare generation -> stage offers -> whole-feed gate.
Nothing becomes current until snapshot_engine.promote() is called separately.
Never writes Saleor products, variants, listings, media or warehouse stock.
"""
import hashlib
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

INSERT_BATCH = 500


def _file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, default=str)


def ingest(adapter, path, source_sequence=None, source_updated_at=None,
           expected_fingerprint=None):
    """Stage a complete supplier generation. Returns a report; promotes nothing."""
    from django.db import connection, transaction, IntegrityError
    from importer.engine.adapters.base import FeedValidationError
    from importer.engine.canonical import snapshot_engine as se
    from importer.engine.canonical import offer_writer as ow
    from importer.engine.canonical.supplier_import import (
        normalize_record, DuplicateFeedError, CanonicalRepository,
    )

    supplier_id = adapter.supplier_id
    feed_path = Path(path).expanduser()
    if not feed_path.is_file() or feed_path.stat().st_size <= 0:
        raise FeedValidationError("Feed missing, not a regular file, or empty")

    fmt = feed_path.suffix.lstrip(".").upper()
    if fmt not in adapter.supported_formats:
        raise FeedValidationError("Unsupported feed format: " + fmt)

    digest_before = _file_sha256(feed_path)

    # ---- pass 1: validate, normalise, fingerprint (bounded memory) -------
    import heapq, tempfile
    seen = set()
    duplicates = 0
    invalid = 0
    excluded = 0
    source_rows = 0
    eligible_rows = 0
    chunk = []
    reasons = {}

    with tempfile.TemporaryDirectory(prefix="feed_stage_") as tmp:
        tmp_path = Path(tmp)
        chunks = []

        def flush():
            if not chunk:
                return
            chunk.sort()
            target = tmp_path / ("sort_%06d.jsonl" % len(chunks))
            with target.open("w", encoding="utf-8", newline="\n") as out:
                for line in chunk:
                    out.write(line + "\n")
            chunks.append(target)
            chunk.clear()

        for raw in adapter.stream_records(str(feed_path)):
            source_rows += 1
            ok, reason = adapter.is_eligible(raw)
            if not ok:
                excluded += 1
                reasons[reason] = reasons.get(reason, 0) + 1
                continue
            try:
                adapted = adapter.normalise(raw)
                adapter.validate_record(adapted)
                record = normalize_record(adapted)
            except (FeedValidationError, ValueError, TypeError) as exc:
                invalid += 1
                logger.warning("row %s invalid: %s", source_rows, exc)
                continue
            sku = record["supplier_sku"]
            if sku in seen:
                duplicates += 1
                continue
            seen.add(sku)
            eligible_rows += 1
            chunk.append(_canonical_json(record))
            if len(chunk) >= 1000:
                flush()

        if not eligible_rows:
            raise FeedValidationError("Feed contains no eligible records")
        flush()

        h = hashlib.sha256()
        h.update(b'{"records":[')
        first = True
        handles = [c.open("r", encoding="utf-8", newline="") for c in chunks]
        try:
            for line in heapq.merge(*handles):
                if not first:
                    h.update(b",")
                h.update(line.rstrip("\n").encode("utf-8"))
                first = False
        finally:
            for fh in handles:
                fh.close()
        h.update(b'],"supplier_id":')
        h.update(_canonical_json(supplier_id).encode("utf-8"))
        h.update(b"}")
        fingerprint = h.hexdigest()

    if expected_fingerprint and fingerprint != expected_fingerprint:
        raise FeedValidationError("Fingerprint guard rejected feed before writes")
    if _file_sha256(feed_path) != digest_before:
        raise FeedValidationError("Feed changed during validation")

    # ---- pass 2: create batch + rows, then offers, in one transaction ----
    seq, ts = adapter.ordering(str(feed_path), source_sequence, source_updated_at)
    inserted_rows = 0
    written_offers = 0
    unmatched = 0

    with transaction.atomic():
        with connection.cursor() as cur:
            cur.execute("SET LOCAL lock_timeout='15s'")
            cur.execute("SET LOCAL statement_timeout='30min'")
            se.lock_supplier(cur, supplier_id)
            try:
                with transaction.atomic():
                    cur.execute("""
                        INSERT INTO catalogue.supplier_import_batches
                          (supplier_id, feed_format, feed_fingerprint, status,
                           total_rows, snapshot_status, feed_kind,
                           source_sequence, source_updated_at,
                           validation_summary)
                        VALUES (%s,%s,%s,'PROCESSING',%s,'STAGING',%s,%s,%s,
                                '{}'::jsonb)
                        RETURNING id
                    """, (supplier_id, fmt, fingerprint, eligible_rows,
                          adapter.feed_kind, seq, ts))
                    batch_id = str(cur.fetchone()[0])
            except IntegrityError:
                cur.execute("""
                    SELECT id FROM catalogue.supplier_import_batches
                    WHERE supplier_id=%s AND feed_fingerprint=%s LIMIT 1
                """, (supplier_id, fingerprint))
                row = cur.fetchone()
                raise DuplicateFeedError(
                    "Duplicate supplier feed. Existing batch: "
                    + str(row[0] if row else "unknown"))

            pending = []
            number = 0
            for raw in adapter.stream_records(str(feed_path)):
                ok, _ = adapter.is_eligible(raw)
                if not ok:
                    continue
                try:
                    adapted = adapter.normalise(raw)
                    adapter.validate_record(adapted)
                    record = normalize_record(adapted)
                except (FeedValidationError, ValueError, TypeError):
                    continue
                number += 1
                pending.append((batch_id, number, record["supplier_sku"],
                                record["product_name"],
                                record.get("manufacturer"), record.get("brand"),
                                record.get("gtin"), record.get("mpn"),
                                record.get("supplier_price"),
                                record.get("currency"),
                                record.get("stock_quantity"),
                                record.get("minimum_order_quantity"),
                                _canonical_json(adapted)))
                if len(pending) >= INSERT_BATCH:
                    cur.executemany("""
                        INSERT INTO catalogue.supplier_import_rows
                          (batch_id,row_number,supplier_sku,product_name,
                           manufacturer,brand,gtin,mpn,supplier_price,currency,
                           stock_quantity,minimum_order_quantity,raw_record,status)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                                %s::jsonb,'PENDING')
                    """, pending)
                    inserted_rows += len(pending)
                    pending.clear()
            if pending:
                cur.executemany("""
                    INSERT INTO catalogue.supplier_import_rows
                      (batch_id,row_number,supplier_sku,product_name,
                       manufacturer,brand,gtin,mpn,supplier_price,currency,
                       stock_quantity,minimum_order_quantity,raw_record,status)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                            %s::jsonb,'PENDING')
                """, pending)
                inserted_rows += len(pending)
                pending.clear()

            if inserted_rows != eligible_rows:
                raise FeedValidationError(
                    "Staging row-count mismatch: %s vs %s"
                    % (inserted_rows, eligible_rows))

            # ---- identity resolution + snapshot-scoped offers ------------
            cur.execute("""
                SELECT id, supplier_sku, supplier_price, currency,
                       stock_quantity, minimum_order_quantity, raw_record
                FROM catalogue.supplier_import_rows
                WHERE batch_id=%s ORDER BY row_number
            """, (batch_id,))
            staged = cur.fetchall()

            for row_id, sku, price, currency, qty, moq, raw_record in staged:
                canonical_id, method = adapter.resolve_identity(cur, sku)
                if not canonical_id:
                    unmatched += 1
                    cur.execute("""
                        UPDATE catalogue.supplier_import_rows
                        SET status='UNMATCHED', matching_method='UNMATCHED'
                        WHERE id=%s
                    """, (str(row_id),))
                    continue

                payload = raw_record
                if isinstance(payload, str):
                    payload = json.loads(payload)
                state = (payload or {}).get("availability_state")
                if state is None:
                    state, qty = adapter.availability(payload or {})

                CanonicalRepository.link_supplier(canonical_id, supplier_id, sku)
                cur.execute("""
                    SELECT id FROM catalogue.product_supplier_links
                    WHERE supplier_id=%s AND supplier_sku=%s
                """, (supplier_id, sku))
                link = cur.fetchone()
                if not link:
                    unmatched += 1
                    continue

                ow.upsert(connection, batch_id, link[0],
                          unit_cost=price, currency=currency,
                          availability_state=state,
                          available_quantity=qty,
                          tax_inclusive=adapter.tax_inclusive,
                          minimum_order_quantity=moq or 1,
                          lead_time_days=adapter.lead_time_days(payload or {}),
                          raw_data=payload)
                written_offers += 1
                cur.execute("""
                    UPDATE catalogue.supplier_import_rows
                    SET status='MATCHED', canonical_product_id=%s,
                        matching_method=%s, error_message=NULL
                    WHERE id=%s
                """, (str(canonical_id), method, str(row_id)))

            cur.execute("""
                UPDATE catalogue.supplier_import_batches
                SET matched_rows=%s, unmatched_rows=%s, invalid_rows=%s
                WHERE id=%s
            """, (written_offers, unmatched, invalid, batch_id))

    if _file_sha256(feed_path) != digest_before:
        raise FeedValidationError("Feed changed during staging")

    counts = {
        "source_rows": source_rows,
        "eligible_rows": eligible_rows,
        "excluded_rows": excluded,
        "matched_rows": written_offers,
        "inserted_offers": written_offers,
        "unmatched_rows": unmatched,
        "invalid_rows": invalid,
        "duplicate_skus": duplicates,
    }

    previous = None
    with connection.cursor() as cur:
        cur.execute("""
            SELECT b.total_rows
            FROM catalogue.supplier_catalogue_state s
            JOIN catalogue.supplier_import_batches b ON b.id=s.active_batch_id
            WHERE s.supplier_id=%s
        """, (supplier_id,))
        row = cur.fetchone()
        if row:
            previous = int(row[0])

    ok, reasons = adapter.validate_feed(counts, previous)
    ready = se.mark_ready(connection, batch_id, counts, previous_rows=previous,
                          thresholds=adapter.thresholds)

    return {
        "supplier_id": supplier_id,
        "batch_id": batch_id,
        "feed_kind": adapter.feed_kind,
        "fingerprint": fingerprint,
        "source_sha256": digest_before,
        "counts": counts,
        "excluded_by_reason": reasons if isinstance(reasons, dict) else {},
        "previous_eligible_rows": previous,
        "validation": ready,
        "promoted": False,
        "saleor_catalogue_written": False,
        "warehouse_stock_written": False,
    }
