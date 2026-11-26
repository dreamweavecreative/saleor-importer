"""Snapshot-scoped supplier offer persistence.

Key difference from the legacy repository: offers are keyed by
(supplier_link_id, snapshot_batch_id), so a new generation never overwrites
the previous one. Promotion switches a pointer; history stays intact.

Generation model consequences:
  * Retirement is implicit. An offer in a superseded generation is not current.
  * Reactivation is implicit. A returning SKU gets a fresh offer row in the
    new generation with is_active=TRUE.
  * No mass UPDATE is required at promotion time.

Supplier availability is NEVER written to Saleor warehouse stock here.
"""
import json
import re
from decimal import Decimal, InvalidOperation

from .availability import validate as validate_availability


class OfferWriteError(ValueError):
    pass


def validate_offer(unit_cost, currency, availability_state, available_quantity,
                   minimum_order_quantity=1, pack_quantity=1, lead_time_days=None):
    """Pure validation. Returns normalised values or raises OfferWriteError."""
    validate_availability(availability_state, available_quantity)
    try:
        cost = Decimal(str(unit_cost))
        moq = Decimal(str(minimum_order_quantity))
        pack = Decimal(str(pack_quantity))
    except (InvalidOperation, TypeError, ValueError):
        raise OfferWriteError("Invalid offer numeric value") from None
    if not cost.is_finite() or cost < 0:
        raise OfferWriteError("Unit cost must be finite and non-negative")
    if not moq.is_finite() or moq <= 0:
        raise OfferWriteError("Minimum order quantity must be positive")
    if not pack.is_finite() or pack <= 0:
        raise OfferWriteError("Pack quantity must be positive")
    code = str(currency or "").strip().upper()
    if not re.fullmatch(r"[A-Z]{3}", code):
        raise OfferWriteError("Three-letter currency code required")
    if lead_time_days is not None:
        if isinstance(lead_time_days, bool) or not isinstance(lead_time_days, int):
            raise OfferWriteError("Lead time must be an integer or None")
        if lead_time_days < 0:
            raise OfferWriteError("Lead time cannot be negative")
    qty = None if available_quantity is None else Decimal(str(available_quantity))
    return {
        "unit_cost": cost, "currency": code, "moq": moq, "pack": pack,
        "available_quantity": qty, "availability_state": availability_state,
        "lead_time_days": lead_time_days,
    }


def upsert(connection, batch_id, supplier_link_id, unit_cost, currency,
           availability_state, available_quantity=None, tax_inclusive=False,
           minimum_order_quantity=1, pack_quantity=1, lead_time_days=None,
           supplier_product_url=None, source_updated_at=None,
           valid_until=None, raw_data=None):
    """Write one offer into one STAGING generation. Never touches live state."""
    from django.db import transaction
    from .snapshot_engine import lock_supplier, SnapshotError

    v = validate_offer(unit_cost, currency, availability_state, available_quantity,
                       minimum_order_quantity, pack_quantity, lead_time_days)
    with transaction.atomic():
        with connection.cursor() as cur:
            cur.execute(
                "SELECT supplier_id FROM catalogue.supplier_import_batches WHERE id=%s",
                (batch_id,))
            owner = cur.fetchone()
            if not owner:
                raise SnapshotError("Snapshot batch not found")
            lock_supplier(cur, owner[0])          # supplier lock FIRST
            cur.execute("""
                SELECT b.supplier_id, b.snapshot_status, b.status, l.supplier_id
                FROM catalogue.supplier_import_batches b
                JOIN catalogue.product_supplier_links l ON l.id=%s
                WHERE b.id=%s FOR UPDATE OF b
            """, (supplier_link_id, batch_id))
            row = cur.fetchone()
            if not row:
                raise SnapshotError("Batch or supplier link not found")
            b_sup, snap, status, l_sup = row
            if str(b_sup) != str(l_sup):
                raise SnapshotError("Supplier link belongs to another supplier")
            if snap != "STAGING" or status != "PROCESSING":
                raise SnapshotError("Offers may only be written to a STAGING batch")
            cur.execute("""
                INSERT INTO catalogue.supplier_offers
                  (supplier_link_id, snapshot_batch_id, unit_cost, currency,
                   tax_inclusive, available_quantity, availability_state,
                   minimum_order_quantity, pack_quantity, lead_time_days,
                   supplier_product_url, source_updated_at, valid_until, raw_data,
                   is_active, observed_at, last_seen_at, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,
                        TRUE, NOW(), NOW(), NOW())
                ON CONFLICT (supplier_link_id, snapshot_batch_id) DO UPDATE SET
                   unit_cost=EXCLUDED.unit_cost,
                   currency=EXCLUDED.currency,
                   tax_inclusive=EXCLUDED.tax_inclusive,
                   available_quantity=EXCLUDED.available_quantity,
                   availability_state=EXCLUDED.availability_state,
                   minimum_order_quantity=EXCLUDED.minimum_order_quantity,
                   pack_quantity=EXCLUDED.pack_quantity,
                   lead_time_days=EXCLUDED.lead_time_days,
                   supplier_product_url=EXCLUDED.supplier_product_url,
                   source_updated_at=EXCLUDED.source_updated_at,
                   valid_until=EXCLUDED.valid_until,
                   raw_data=EXCLUDED.raw_data,
                   is_active=TRUE,
                   retired_at=NULL,
                   retirement_reason=NULL,
                   last_seen_at=NOW(),
                   updated_at=NOW()
                RETURNING id
            """, (supplier_link_id, batch_id, v["unit_cost"], v["currency"],
                  bool(tax_inclusive), v["available_quantity"],
                  v["availability_state"], v["moq"], v["pack"],
                  v["lead_time_days"], supplier_product_url, source_updated_at,
                  valid_until, json.dumps(raw_data or {}, default=str)))
            return str(cur.fetchone()[0])


def prepare_batch(connection, batch_id, feed_kind="FULL",
                  source_sequence=None, source_updated_at=None):
    """Mark a freshly staged batch as a STAGING generation of a known kind.

    The migration defaults new rows to LEGACY/UNKNOWN for historical data.
    A new import must declare its kind explicitly; UNKNOWN can never promote.
    """
    from django.db import transaction
    from .snapshot_engine import lock_supplier, SnapshotError, SnapshotValidationError
    if feed_kind not in ("FULL", "DELTA", "UNKNOWN"):
        raise SnapshotValidationError("feed_kind must be FULL, DELTA or UNKNOWN")
    with transaction.atomic():
        with connection.cursor() as cur:
            cur.execute(
                "SELECT supplier_id FROM catalogue.supplier_import_batches WHERE id=%s",
                (batch_id,))
            owner = cur.fetchone()
            if not owner:
                raise SnapshotError("Snapshot batch not found")
            lock_supplier(cur, owner[0])
            cur.execute("""
                SELECT snapshot_status, status FROM catalogue.supplier_import_batches
                WHERE id=%s FOR UPDATE
            """, (batch_id,))
            snap, status = cur.fetchone()
            if status != "PROCESSING":
                raise SnapshotError("Only a PROCESSING batch can be prepared")
            if snap not in ("LEGACY", "STAGING"):
                raise SnapshotError("Batch already advanced past STAGING")
            cur.execute("""
                SELECT COUNT(*) FROM catalogue.supplier_offers
                WHERE snapshot_batch_id=%s
            """, (batch_id,))
            if cur.fetchone()[0]:
                raise SnapshotError("Batch already carries offers")
            cur.execute("""
                UPDATE catalogue.supplier_import_batches
                SET snapshot_status='STAGING', feed_kind=%s,
                    source_sequence=COALESCE(%s, source_sequence),
                    source_updated_at=COALESCE(%s, source_updated_at)
                WHERE id=%s
            """, (feed_kind, source_sequence, source_updated_at, batch_id))
    return {"batch_id": str(batch_id), "snapshot_status": "STAGING",
            "feed_kind": feed_kind}


def retirement_report(connection, supplier_id, candidate_batch_id):
    """READ-ONLY: which supplier SKUs disappear if this generation is promoted."""
    with connection.cursor() as cur:
        cur.execute("""
            SELECT active_batch_id FROM catalogue.supplier_catalogue_state
            WHERE supplier_id=%s
        """, (str(supplier_id).strip().lower(),))
        row = cur.fetchone()
        active = row[0] if row else None
        if active is None:
            return {"active_batch_id": None, "retiring": [], "returning": [],
                    "retiring_count": 0, "returning_count": 0}
        cur.execute("""
            WITH cur AS (
              SELECT l.supplier_sku FROM catalogue.supplier_offers o
              JOIN catalogue.product_supplier_links l ON l.id=o.supplier_link_id
              WHERE o.snapshot_batch_id=%s AND o.is_active=TRUE),
            nxt AS (
              SELECT l.supplier_sku FROM catalogue.supplier_offers o
              JOIN catalogue.product_supplier_links l ON l.id=o.supplier_link_id
              WHERE o.snapshot_batch_id=%s AND o.is_active=TRUE)
            SELECT
              (SELECT array_agg(supplier_sku) FROM (
                 SELECT supplier_sku FROM cur EXCEPT SELECT supplier_sku FROM nxt
                 ORDER BY 1 LIMIT 200) a),
              (SELECT array_agg(supplier_sku) FROM (
                 SELECT supplier_sku FROM nxt EXCEPT SELECT supplier_sku FROM cur
                 ORDER BY 1 LIMIT 200) b),
              (SELECT COUNT(*) FROM (
                 SELECT supplier_sku FROM cur EXCEPT SELECT supplier_sku FROM nxt) c),
              (SELECT COUNT(*) FROM (
                 SELECT supplier_sku FROM nxt EXCEPT SELECT supplier_sku FROM cur) d)
        """, (active, candidate_batch_id))
        retiring, returning, n_ret, n_ret2 = cur.fetchone()
    return {"active_batch_id": str(active),
            "retiring": retiring or [], "returning": returning or [],
            "retiring_count": int(n_ret or 0), "returning_count": int(n_ret2 or 0)}
