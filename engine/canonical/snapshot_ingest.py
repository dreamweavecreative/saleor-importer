"""Bounded-memory snapshot ingest: staged rows AND snapshot-scoped offers.

Reuses the proven streaming staging function for rows and fingerprint parity,
then streams the feed a second time to write offers into the SAME generation.
Nothing becomes current until snapshot_engine.promote() is called separately.

Never writes Saleor products, variants, listings, media or warehouse stock.
"""
import csv
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

OFFER_BATCH = 500


def ingest_dickerdata(connection, feed_path, supplier_id="dickerdata",
                      source_sequence=None, source_updated_at=None,
                      offer_batch=OFFER_BATCH):
    """Stage rows, declare the generation, write offers, then validate READY."""
    from django.db import transaction
    from importer.engine.canonical import supplier_import as si
    from importer.engine.canonical import snapshot_engine as se
    from importer.engine.canonical import offer_writer as ow
    from importer.engine.dickerdata_adapter import EXPECTED_COLUMNS, adapt_records

    feed_path = Path(feed_path).expanduser()
    if not feed_path.is_file() or feed_path.stat().st_size <= 0:
        raise ValueError("Feed path missing, not a file, or empty")

    # 1. Rows + fingerprint, using the existing proven streaming stager.
    staged = si.stage_dickerdata_feed(str(feed_path), supplier_id=supplier_id,
                                      feed_format="CSV")
    batch_id = staged["batch_id"]

    # 2. Declare this batch a FULL STAGING generation (migration default is LEGACY).
    ow.prepare_batch(connection, batch_id, feed_kind="FULL",
                     source_sequence=source_sequence,
                     source_updated_at=source_updated_at)

    # 3. Stream again, writing offers into the same generation.
    written = 0
    unmatched = 0
    invalid = 0
    seen = set()
    duplicates = 0
    with feed_path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        headers = [str(h).strip() if h else "" for h in (reader.fieldnames or [])]
        if set(headers) != set(EXPECTED_COLUMNS):
            raise ValueError("DickerData CSV schema mismatch on offer pass")
        reader.fieldnames = headers
        for raw in reader:
            if not any(v is not None and str(v).strip() for v in raw.values()):
                continue
            if str(raw.get("Status") or "").strip().upper() != "A":
                continue
            if str(raw.get("Type") or "").strip().casefold() != "stockeditem":
                continue
            try:
                adapted = adapt_records([raw])[0]
                record = si.normalize_record(adapted)
            except (ValueError, TypeError):
                invalid += 1
                continue
            sku = record["supplier_sku"]
            if sku in seen:
                duplicates += 1
                continue
            seen.add(sku)
            with transaction.atomic():
                with connection.cursor() as cur:
                    canonical_id, method = si._resolve_dickerdata_variant(cur, sku)
                    if not canonical_id:
                        unmatched += 1
                        continue
                    si.CanonicalRepository.link_supplier(canonical_id, supplier_id, sku)
                    cur.execute("""
                        SELECT id FROM catalogue.product_supplier_links
                        WHERE supplier_id=%s AND supplier_sku=%s
                    """, (supplier_id, sku))
                    link = cur.fetchone()
                    if not link:
                        unmatched += 1
                        continue
                    state = adapted.get("availability_state") or "UNKNOWN"
                    ow.upsert(connection, batch_id, link[0],
                              unit_cost=record["supplier_price"],
                              currency=record["currency"],
                              availability_state=state,
                              available_quantity=record.get("stock_quantity"),
                              tax_inclusive=False,
                              minimum_order_quantity=record.get("minimum_order_quantity") or 1,
                              raw_data=adapted)
                    written += 1

    counts = {
        "source_rows": int(staged["source_rows"]),
        "eligible_rows": int(staged["eligible_rows"]),
        "excluded_rows": int(staged["excluded_rows"]),
        "matched_rows": written,
        "inserted_offers": written,
        "unmatched_rows": unmatched,
        "invalid_rows": invalid,
        "duplicate_skus": duplicates,
    }

    # 4. Whole-feed validation gate. Only a clean FULL feed becomes READY.
    with connection.cursor() as cur:
        cur.execute("""
            SELECT validation_summary FROM catalogue.supplier_import_batches
            WHERE id=%s
        """, (batch_id,))
        row = cur.fetchone()
    previous = None
    ready = se.mark_ready(connection, batch_id, counts, previous_rows=previous)
    return {"batch_id": batch_id, "staged": staged, "offer_counts": counts,
            "validation": ready, "promoted": False,
            "saleor_catalogue_written": False, "warehouse_stock_written": False}
