"""Independent supplier snapshot lifecycle.

This module is deliberately separate from the legacy Saleor writer.
It does not create database connections, write Saleor inventory, or
publish products. The caller supplies a DB-API connection.
"""
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
import json


class SnapshotPipelineError(RuntimeError):
    pass


class SnapshotValidationError(SnapshotPipelineError):
    pass


AVAILABILITY_STATES = {"FINITE", "UNLIMITED", "UNKNOWN", "UNAVAILABLE"}
FEED_KINDS = {"FULL", "DELTA", "UNKNOWN"}


def availability_from_record(record):
    """DickerData's 999999999 sentinel is UNKNOWN, never unlimited."""
    raw = record.get("StockAvailable", record.get("stock"))
    if raw is None or str(raw).strip() == "":
        return "UNKNOWN", None
    try:
        quantity = int(str(raw).strip())
    except (TypeError, ValueError):
        raise SnapshotValidationError("Stock quantity is not an integer")
    if quantity < 0:
        raise SnapshotValidationError("Negative stock quantity is invalid")
    if quantity == 999999999:
        return "UNKNOWN", None
    if quantity == 0:
        return "UNAVAILABLE", 0
    return "FINITE", quantity


def validate_availability(state, quantity):
    if state not in AVAILABILITY_STATES:
        raise SnapshotValidationError("Invalid availability state")
    if state == "FINITE" and (quantity is None or int(quantity) <= 0):
        raise SnapshotValidationError("FINITE requires positive quantity")
    if state == "UNAVAILABLE" and quantity != 0:
        raise SnapshotValidationError("UNAVAILABLE requires quantity zero")
    if state in {"UNKNOWN", "UNLIMITED"} and quantity is not None:
        raise SnapshotValidationError(state + " requires a null quantity")


@contextmanager
def transaction(connection):
    try:
        yield
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def fetch_one(cursor):
    return cursor.fetchone()


class SnapshotPipeline:
    def __init__(self, connection):
        self.connection = connection

    @staticmethod
    def lock_supplier(cursor, supplier_id):
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            ("catalogue:supplier:" + str(supplier_id),),
        )

    def create_snapshot(
        self, supplier_id, fingerprint, total_rows, *,
        feed_kind="UNKNOWN", source_sequence=None, source_updated_at=None,
    ):
        if feed_kind not in FEED_KINDS:
            raise ValueError("Invalid feed_kind")
        if not supplier_id or not fingerprint or int(total_rows) <= 0:
            raise ValueError("Supplier, fingerprint and positive row count required")

        with transaction(self.connection):
            with self.connection.cursor() as cur:
                self.lock_supplier(cur, supplier_id)
                cur.execute(
                    """
                    SELECT id FROM catalogue.supplier_import_batches
                    WHERE supplier_id = %s AND feed_fingerprint = %s
                    LIMIT 1 FOR UPDATE
                    """,
                    (supplier_id, fingerprint),
                )
                if fetch_one(cur):
                    raise SnapshotPipelineError(
                        "This supplier feed fingerprint already exists"
                    )
                cur.execute(
                    """
                    INSERT INTO catalogue.supplier_import_batches
                        (supplier_id, feed_format, feed_fingerprint, status,
                         total_rows, snapshot_status, feed_kind,
                         source_sequence, source_updated_at,
                         validation_summary)
                    VALUES (%s, 'CANONICAL', %s, 'PROCESSING', %s,
                            'STAGING', %s, %s, %s, '{}'::jsonb)
                    RETURNING id
                    """,
                    (
                        supplier_id, fingerprint, int(total_rows), feed_kind,
                        source_sequence, source_updated_at,
                    ),
                )
                row = fetch_one(cur)
                if not row:
                    raise SnapshotPipelineError("Batch creation returned no ID")
                return str(row[0])

    def add_offer(
        self, batch_id, supplier_link_id, unit_cost, currency,
        availability_state, available_quantity=None, *,
        tax_inclusive=False, minimum_order_quantity=1, pack_quantity=1,
        lead_time_days=None, supplier_product_url=None,
        source_updated_at=None, valid_until=None, raw_data=None,
    ):
        validate_availability(availability_state, available_quantity)
        try:
            cost = Decimal(str(unit_cost))
            moq = Decimal(str(minimum_order_quantity))
            pack = Decimal(str(pack_quantity))
        except (InvalidOperation, TypeError, ValueError):
            raise SnapshotValidationError("Invalid cost, MOQ or pack quantity")
        if not cost.is_finite() or cost <= 0:
            raise SnapshotValidationError("Cost must be positive and finite")
        if not moq.is_finite() or moq <= 0 or not pack.is_finite() or pack <= 0:
            raise SnapshotValidationError("MOQ and pack quantity must be positive")
        if not currency or len(str(currency).strip()) != 3:
            raise SnapshotValidationError("Currency must be a three-character code")

        with transaction(self.connection):
            with self.connection.cursor() as cur:
                cur.execute(
                    """
                    SELECT b.supplier_id, b.snapshot_status, b.status,
                           l.supplier_id
                    FROM catalogue.supplier_import_batches b
                    JOIN catalogue.product_supplier_links l ON l.id = %s
                    WHERE b.id = %s
                    FOR UPDATE OF b
                    """,
                    (supplier_link_id, batch_id),
                )
                row = fetch_one(cur)
                if not row:
                    raise SnapshotPipelineError("Batch or supplier link not found")
                batch_supplier, snapshot_state, batch_state, link_supplier = row
                if str(batch_supplier) != str(link_supplier):
                    raise SnapshotPipelineError("Supplier link ownership mismatch")
                if snapshot_state != "STAGING" or batch_state != "PROCESSING":
                    raise SnapshotPipelineError("Batch is not open for staging")

                cur.execute(
                    """
                    INSERT INTO catalogue.supplier_offers
                        (supplier_link_id, snapshot_batch_id, unit_cost,
                         currency, tax_inclusive, available_quantity,
                         availability_state, minimum_order_quantity,
                         pack_quantity, lead_time_days, supplier_product_url,
                         source_updated_at, valid_until, raw_data,
                         is_active, last_seen_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, TRUE, NOW(), NOW())
                    ON CONFLICT (supplier_link_id, snapshot_batch_id)
                    DO UPDATE SET
                        unit_cost = EXCLUDED.unit_cost,
                        currency = EXCLUDED.currency,
                        tax_inclusive = EXCLUDED.tax_inclusive,
                        available_quantity = EXCLUDED.available_quantity,
                        availability_state = EXCLUDED.availability_state,
                        minimum_order_quantity = EXCLUDED.minimum_order_quantity,
                        pack_quantity = EXCLUDED.pack_quantity,
                        lead_time_days = EXCLUDED.lead_time_days,
                        supplier_product_url = EXCLUDED.supplier_product_url,
                        source_updated_at = EXCLUDED.source_updated_at,
                        valid_until = EXCLUDED.valid_until,
                        raw_data = EXCLUDED.raw_data,
                        is_active = TRUE,
                        last_seen_at = NOW(),
                        updated_at = NOW()
                    RETURNING id
                    """,
                    (
                        supplier_link_id, batch_id, cost, str(currency).upper(),
                        bool(tax_inclusive), available_quantity,
                        availability_state, moq, pack, lead_time_days,
                        supplier_product_url, source_updated_at, valid_until,
                        json.dumps(raw_data or {}, default=str),
                    ),
                )
                row = fetch_one(cur)
                if not row:
                    raise SnapshotPipelineError("Offer insert returned no ID")
                return str(row[0])

    def mark_ready(
        self, batch_id, *, source_rows, eligible_rows, inserted_offers,
        matched_rows, unmatched_rows=0, invalid_rows=0, duplicate_skus=0,
        excluded_rows=0, validation_summary=None,
    ):
        counts = {
            "source_rows": int(source_rows),
            "eligible_rows": int(eligible_rows),
            "inserted_offers": int(inserted_offers),
            "matched_rows": int(matched_rows),
            "unmatched_rows": int(unmatched_rows),
            "invalid_rows": int(invalid_rows),
            "duplicate_skus": int(duplicate_skus),
            "excluded_rows": int(excluded_rows),
        }
        if counts["source_rows"] != counts["eligible_rows"] + counts["excluded_rows"]:
            raise SnapshotValidationError("Source row totals do not reconcile")
        if counts["eligible_rows"] != counts["matched_rows"]:
            raise SnapshotValidationError("Eligible rows are not all matched")
        if counts["eligible_rows"] != counts["inserted_offers"]:
            raise SnapshotValidationError("Eligible rows are not all represented by offers")
        if any(counts[k] for k in ("unmatched_rows", "invalid_rows", "duplicate_skus")):
            raise SnapshotValidationError("Validation errors prevent readiness")

        with transaction(self.connection):
            with self.connection.cursor() as cur:
                cur.execute(
                    """
                    SELECT supplier_id, snapshot_status, status, total_rows, feed_kind
                    FROM catalogue.supplier_import_batches
                    WHERE id = %s FOR UPDATE
                    """,
                    (batch_id,),
                )
                row = fetch_one(cur)
                if not row:
                    raise SnapshotPipelineError("Snapshot not found")
                supplier_id, state, batch_state, total_rows, feed_kind = row
                self.lock_supplier(cur, supplier_id)
                if state != "STAGING" or batch_state != "PROCESSING":
                    raise SnapshotPipelineError("Snapshot is not staging")
                if feed_kind != "FULL":
                    raise SnapshotValidationError(
                        "Only explicitly declared FULL feeds can become READY"
                    )
                if counts["source_rows"] != int(total_rows):
                    raise SnapshotValidationError("Feed total differs from staged total")
                cur.execute(
                    "SELECT COUNT(*) FROM catalogue.supplier_offers WHERE snapshot_batch_id = %s",
                    (batch_id,),
                )
                persisted = int(fetch_one(cur)[0])
                if persisted != counts["inserted_offers"]:
                    raise SnapshotValidationError("Persisted offer count mismatch")
                summary = dict(counts)
                summary.update(validation_summary or {})
                cur.execute(
                    """
                    UPDATE catalogue.supplier_import_batches
                    SET snapshot_status = 'READY', status = 'COMPLETED',
                        validation_summary = %s::jsonb
                    WHERE id = %s AND snapshot_status = 'STAGING'
                    """,
                    (json.dumps(summary, default=str), batch_id),
                )
                if cur.rowcount != 1:
                    raise SnapshotPipelineError("READY transition did not update one row")
        return summary

    def promote(self, batch_id, *, expected_active_batch_id="__UNSET__", actor="system"):
        with transaction(self.connection):
            with self.connection.cursor() as cur:
                cur.execute(
                    "SELECT supplier_id FROM catalogue.supplier_import_batches WHERE id = %s",
                    (batch_id,),
                )
                row = fetch_one(cur)
                if not row:
                    raise SnapshotPipelineError("Candidate batch not found")
                supplier_id = row[0]
                self.lock_supplier(cur, supplier_id)

                cur.execute(
                    """
                    SELECT active_batch_id
                    FROM catalogue.supplier_catalogue_state
                    WHERE supplier_id = %s FOR UPDATE
                    """,
                    (supplier_id,),
                )
                state_row = fetch_one(cur)
                active_id = state_row[0] if state_row else None
                if expected_active_batch_id != "__UNSET__":
                    if str(active_id) != str(expected_active_batch_id):
                        raise SnapshotPipelineError("Promotion compare-and-swap failed")

                cur.execute(
                    """
                    SELECT supplier_id, snapshot_status, status, feed_kind,
                           total_rows, validation_summary
                    FROM catalogue.supplier_import_batches
                    WHERE id = %s FOR UPDATE
                    """,
                    (batch_id,),
                )
                candidate = fetch_one(cur)
                if not candidate:
                    raise SnapshotPipelineError("Candidate batch disappeared")
                owner, state, batch_state, feed_kind, total_rows, summary = candidate
                if str(owner) != str(supplier_id):
                    raise SnapshotPipelineError("Candidate supplier mismatch")
                if state != "READY" or batch_state != "COMPLETED" or feed_kind != "FULL":
                    raise SnapshotPipelineError("Candidate is not a completed FULL READY snapshot")
                summary = summary or {}
                if int(summary.get("source_rows", -1)) != int(total_rows):
                    raise SnapshotValidationError("Stored validation does not cover the full feed")
                if any(int(summary.get(k, 0)) for k in (
                    "unmatched_rows", "invalid_rows", "duplicate_skus"
                )):
                    raise SnapshotValidationError("Stored validation contains errors")

                if active_id is not None:
                    cur.execute(
                        """
                        UPDATE catalogue.supplier_import_batches
                        SET snapshot_status = 'SUPERSEDED'
                        WHERE id = %s AND supplier_id = %s
                          AND snapshot_status = 'PROMOTED'
                        """,
                        (active_id, supplier_id),
                    )
                    if cur.rowcount != 1:
                        raise SnapshotPipelineError("Existing active batch is inconsistent")

                cur.execute(
                    """
                    UPDATE catalogue.supplier_import_batches
                    SET snapshot_status = 'PROMOTED', promoted_at = NOW()
                    WHERE id = %s AND supplier_id = %s
                      AND snapshot_status = 'READY' AND status = 'COMPLETED'
                    """,
                    (batch_id, supplier_id),
                )
                if cur.rowcount != 1:
                    raise SnapshotPipelineError("Candidate promotion failed")

                cur.execute(
                    """
                    INSERT INTO catalogue.supplier_catalogue_state
                        (supplier_id, active_batch_id, previous_batch_id,
                         promoted_at, updated_at)
                    VALUES (%s, %s, %s, NOW(), NOW())
                    ON CONFLICT (supplier_id) DO UPDATE SET
                        previous_batch_id = EXCLUDED.previous_batch_id,
                        active_batch_id = EXCLUDED.active_batch_id,
                        promoted_at = NOW(), updated_at = NOW()
                    """,
                    (supplier_id, batch_id, active_id),
                )
                cur.execute(
                    """
                    INSERT INTO catalogue.supplier_snapshot_events
                        (supplier_id, event_type, from_batch_id, to_batch_id,
                         actor, reason)
                    VALUES (%s, 'PROMOTE', %s, %s, %s, %s)
                    """,
                    (supplier_id, active_id, batch_id, actor, "Validated full snapshot"),
                )
        return {
            "supplier_id": str(supplier_id),
            "previous_batch_id": active_id,
            "active_batch_id": str(batch_id),
        }

    def rollback(self, supplier_id, *, actor="system", reason="Operator rollback"):
        with transaction(self.connection):
            with self.connection.cursor() as cur:
                self.lock_supplier(cur, supplier_id)
                cur.execute(
                    """
                    SELECT active_batch_id, previous_batch_id
                    FROM catalogue.supplier_catalogue_state
                    WHERE supplier_id = %s FOR UPDATE
                    """,
                    (supplier_id,),
                )
                state = fetch_one(cur)
                if not state or not state[1]:
                    raise SnapshotPipelineError("No immediate previous snapshot is available")
                active_id, target_id = state
                cur.execute(
                    """
                    SELECT supplier_id, snapshot_status, status, feed_kind
                    FROM catalogue.supplier_import_batches
                    WHERE id = %s FOR UPDATE
                    """,
                    (target_id,),
                )
                target = fetch_one(cur)
                if (
                    not target or str(target[0]) != str(supplier_id)
                    or target[1] != "SUPERSEDED"
                    or target[2] != "COMPLETED"
                    or target[3] != "FULL"
                ):
                    raise SnapshotPipelineError("Rollback target is not a valid prior FULL snapshot")
                cur.execute(
                    """
                    UPDATE catalogue.supplier_import_batches
                    SET snapshot_status = 'SUPERSEDED'
                    WHERE id = %s AND snapshot_status = 'PROMOTED'
                    """,
                    (active_id,),
                )
                if cur.rowcount != 1:
                    raise SnapshotPipelineError("Current active snapshot is inconsistent")
                cur.execute(
                    """
                    UPDATE catalogue.supplier_import_batches
                    SET snapshot_status = 'PROMOTED', promoted_at = NOW()
                    WHERE id = %s AND snapshot_status = 'SUPERSEDED'
                    """,
                    (target_id,),
                )
                if cur.rowcount != 1:
                    raise SnapshotPipelineError("Rollback target transition failed")
                cur.execute(
                    """
                    UPDATE catalogue.supplier_catalogue_state
                    SET active_batch_id = %s, previous_batch_id = NULL,
                        promoted_at = NOW(), updated_at = NOW()
                    WHERE supplier_id = %s
                    """,
                    (target_id, supplier_id),
                )
                cur.execute(
                    """
                    INSERT INTO catalogue.supplier_snapshot_events
                        (supplier_id, event_type, from_batch_id, to_batch_id,
                         actor, reason)
                    VALUES (%s, 'ROLLBACK', %s, %s, %s, %s)
                    """,
                    (supplier_id, active_id, target_id, actor, reason),
                )
        return {"supplier_id": str(supplier_id), "active_batch_id": target_id}

    def active_offers(self, canonical_product_id=None):
        sql = """
            SELECT o.*, l.supplier_id, l.supplier_sku,
                   l.canonical_product_id
            FROM catalogue.supplier_offers o
            JOIN catalogue.product_supplier_links l ON l.id = o.supplier_link_id
            JOIN catalogue.supplier_catalogue_state s
              ON s.supplier_id = l.supplier_id
             AND s.active_batch_id = o.snapshot_batch_id
            JOIN catalogue.supplier_import_batches b
              ON b.id = o.snapshot_batch_id
             AND b.snapshot_status = 'PROMOTED'
            WHERE o.is_active = TRUE
              AND (o.valid_until IS NULL OR o.valid_until > NOW())
        """
        params = []
        if canonical_product_id is not None:
            sql += " AND l.canonical_product_id = %s"
            params.append(canonical_product_id)
        sql += " ORDER BY o.unit_cost ASC"
        with self.connection.cursor() as cur:
            cur.execute(sql, params)
            names = [column[0] for column in cur.description]
            return [dict(zip(names, row)) for row in cur.fetchall()]

    def eligible_offers(self, canonical_product_id, *, allow_unknown_stock=False):
        eligible, excluded = [], []
        for offer in self.active_offers(canonical_product_id):
            state = offer.get("availability_state")
            quantity = offer.get("available_quantity")
            reason = None
            if state == "FINITE":
                if quantity is None or int(quantity) <= 0:
                    reason = "INVALID_FINITE_QUANTITY"
            elif state == "UNLIMITED":
                if quantity is not None:
                    reason = "INVALID_UNLIMITED_QUANTITY"
            elif state == "UNKNOWN":
                if not allow_unknown_stock:
                    reason = "UNKNOWN_STOCK"
            elif state == "UNAVAILABLE":
                reason = "UNAVAILABLE"
            else:
                reason = "INVALID_AVAILABILITY_STATE"
            if reason:
                excluded.append({"offer": offer, "reason": reason})
            else:
                eligible.append(offer)
        return {"eligible": eligible, "excluded": excluded}

    def visibility_report(self, *, allow_unknown_stock=False):
        """Dry-run multi-supplier eligibility; deliberately makes no Saleor writes."""
        with self.connection.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT l.canonical_product_id
                FROM catalogue.supplier_offers o
                JOIN catalogue.product_supplier_links l ON l.id = o.supplier_link_id
                JOIN catalogue.supplier_catalogue_state s
                  ON s.supplier_id = l.supplier_id
                 AND s.active_batch_id = o.snapshot_batch_id
                JOIN catalogue.supplier_import_batches b
                  ON b.id = o.snapshot_batch_id
                 AND b.snapshot_status = 'PROMOTED'
                WHERE o.is_active = TRUE
                  AND (o.valid_until IS NULL OR o.valid_until > NOW())
                """
            )
            product_ids = [row[0] for row in cur.fetchall()]

        eligible_ids, ineligible_ids = [], []
        for product_id in product_ids:
            result = self.eligible_offers(
                product_id, allow_unknown_stock=allow_unknown_stock
            )
            target = eligible_ids if result["eligible"] else ineligible_ids
            target.append(str(product_id))
        return {
            "mode": "DRY_RUN",
            "eligible_product_ids": eligible_ids,
            "no_eligible_active_supplier_offer_product_ids": ineligible_ids,
            "saleor_publication_changed": False,
            "warehouse_inventory_changed": False,
            "products_deleted": False,
        }
