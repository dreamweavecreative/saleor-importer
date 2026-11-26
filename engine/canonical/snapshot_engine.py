"""Authoritative supplier-scoped snapshot lifecycle.

Supersedes snapshot_lifecycle.py and snapshot_pipeline.py for new work.
Those modules are left untouched on disk; nothing imports them from here.

Invariants:
  * Supplier advisory lock is ALWAYS acquired before any row lock.
  * Only FULL, READY, COMPLETED, error-free snapshots may be promoted.
  * Promotion switches one supplier pointer; other suppliers untouched.
  * Older snapshots can never overwrite newer ones.
  * Repeated identical fingerprints are idempotent.
  * No Saleor product/variant deletion. No warehouse stock writes.
"""

import json

from .availability import classify, validate as validate_availability

TRANSITIONS = {
    "STAGING": {"VALIDATING", "READY", "REJECTED", "FAILED"},
    "VALIDATING": {"READY", "REJECTED", "FAILED"},
    "READY": {"PROMOTED", "FAILED", "REJECTED"},
    "PROMOTED": {"SUPERSEDED"},
    "SUPERSEDED": {"PROMOTED"},
    "REJECTED": set(),
    "FAILED": set(),
    "LEGACY": {"SUPERSEDED"},
}

DEFAULT_THRESHOLDS = {
    "min_eligible_rows": 1,
    "max_row_decrease_pct": 10.0,
    "max_row_increase_pct": 50.0,
    "max_invalid_pct": 0.0,
    "max_unmatched_pct": 0.0,
    "max_duplicate_skus": 0,
}


class SnapshotError(RuntimeError):
    pass


class SnapshotValidationError(SnapshotError):
    pass


class SnapshotOrderingError(SnapshotError):
    pass


class SnapshotTransitionError(SnapshotError):
    pass


class DuplicateSnapshot(SnapshotError):
    def __init__(self, batch_id):
        super().__init__("Feed already staged as batch " + str(batch_id))
        self.batch_id = batch_id


def _atomic():
    from django.db import transaction
    return transaction.atomic()


def lock_supplier(cursor, supplier_id):
    """MUST be the first lock acquired in every write path."""
    cursor.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        ("catalogue:supplier:" + str(supplier_id),),
    )


def assert_newer(cand_seq, cand_ts, act_seq, act_ts, allow_unordered=False):
    """Reject stale or ambiguous ordering. Never guess."""
    if cand_seq is not None or act_seq is not None:
        if cand_seq is None or act_seq is None:
            if not allow_unordered:
                raise SnapshotOrderingError(
                    "Only one snapshot carries source_sequence; ordering ambiguous")
            return True
        if int(cand_seq) <= int(act_seq):
            raise SnapshotOrderingError(
                "Candidate source_sequence is not newer than active snapshot")
        return True
    if cand_ts is not None or act_ts is not None:
        if cand_ts is None or act_ts is None:
            if not allow_unordered:
                raise SnapshotOrderingError(
                    "Only one snapshot carries source_updated_at; ordering ambiguous")
            return True
        if cand_ts <= act_ts:
            raise SnapshotOrderingError(
                "Candidate source_updated_at is not newer than active snapshot")
        return True
    if not allow_unordered:
        raise SnapshotOrderingError(
            "No comparable ordering metadata; explicit override required")
    return True


def check_transition(current, target):
    if target not in TRANSITIONS.get(current, set()):
        raise SnapshotTransitionError(
            "Transition " + repr(current) + " -> " + repr(target) + " not permitted")
    return True


def evaluate_thresholds(counts, previous_rows=None, thresholds=None):
    """Pure validation gate. Returns (ok, reasons, summary)."""
    t = dict(DEFAULT_THRESHOLDS)
    t.update(thresholds or {})
    c = {k: int(v) for k, v in counts.items()}
    reasons = []

    source = c.get("source_rows", 0)
    eligible = c.get("eligible_rows", 0)
    excluded = c.get("excluded_rows", 0)
    matched = c.get("matched_rows", 0)
    offers = c.get("inserted_offers", 0)
    unmatched = c.get("unmatched_rows", 0)
    invalid = c.get("invalid_rows", 0)
    dupes = c.get("duplicate_skus", 0)

    if source != eligible + excluded:
        reasons.append("SOURCE_ROW_TOTALS_DO_NOT_RECONCILE")
    # Variable-feed support (opt-in via supplier policy "strict_match": false).
    # Legitimate catalogue size variation and unmatched NEW products must never
    # block an update. When strict_match is disabled, unmatched rows are bounded
    # by max_unmatched_pct instead of a hard full-match requirement, and the
    # integrity rule becomes: every MATCHED row must have an offer. Default
    # strict_match=true preserves the original full-match behaviour.
    if bool(t.get("strict_match", True)):
        if eligible != matched:
            reasons.append("NOT_ALL_ELIGIBLE_ROWS_MATCHED")
        if eligible != offers:
            reasons.append("NOT_ALL_ELIGIBLE_ROWS_HAVE_OFFERS")
    else:
        if matched != offers:
            reasons.append("MATCHED_ROWS_MISSING_OFFERS")
    if eligible < int(t["min_eligible_rows"]):
        reasons.append("BELOW_MINIMUM_ELIGIBLE_ROWS")
    if dupes > int(t["max_duplicate_skus"]):
        reasons.append("DUPLICATE_SUPPLIER_SKUS")
    if source > 0:
        if (invalid / source) * 100.0 > float(t["max_invalid_pct"]):
            reasons.append("INVALID_ROW_PERCENTAGE_EXCEEDED")
        if (unmatched / source) * 100.0 > float(t["max_unmatched_pct"]):
            reasons.append("UNMATCHED_ROW_PERCENTAGE_EXCEEDED")
    if previous_rows:
        prev = int(previous_rows)
        if prev > 0:
            drop = ((prev - eligible) / prev) * 100.0
            rise = ((eligible - prev) / prev) * 100.0
            if drop > float(t["max_row_decrease_pct"]):
                reasons.append("ROW_COUNT_DECREASE_EXCEEDED")
            if rise > float(t["max_row_increase_pct"]):
                reasons.append("ROW_COUNT_INCREASE_EXCEEDED")
    summary = dict(c)
    summary["previous_eligible_rows"] = previous_rows
    summary["thresholds"] = t
    summary["reasons"] = reasons
    return (not reasons), reasons, summary


def register(connection, supplier_id, feed_format, fingerprint, total_rows,
             feed_kind="UNKNOWN", source_sequence=None, source_updated_at=None):
    """Create an immutable STAGING generation. Never defaults to FULL."""
    supplier_id = str(supplier_id or "").strip().lower()
    if not supplier_id or not fingerprint:
        raise SnapshotValidationError("supplier_id and fingerprint required")
    if feed_kind not in ("FULL", "DELTA", "UNKNOWN"):
        raise SnapshotValidationError("feed_kind must be FULL, DELTA or UNKNOWN")
    if int(total_rows) <= 0:
        raise SnapshotValidationError("total_rows must be positive")
    with _atomic():
        with connection.cursor() as cur:
            lock_supplier(cur, supplier_id)
            cur.execute(
                "SELECT id FROM catalogue.supplier_import_batches "
                "WHERE supplier_id=%s AND feed_fingerprint=%s "
                "ORDER BY created_at DESC LIMIT 1",
                (supplier_id, fingerprint),
            )
            row = cur.fetchone()
            if row:
                raise DuplicateSnapshot(row[0])
            cur.execute(
                "INSERT INTO catalogue.supplier_import_batches "
                "(supplier_id, feed_format, feed_fingerprint, status, total_rows, "
                " snapshot_status, feed_kind, source_sequence, source_updated_at, "
                " validation_summary) "
                "VALUES (%s,%s,%s,'PROCESSING',%s,'STAGING',%s,%s,%s,'{}'::jsonb) "
                "RETURNING id",
                (supplier_id, str(feed_format).upper(), fingerprint,
                 int(total_rows), feed_kind, source_sequence, source_updated_at),
            )
            return str(cur.fetchone()[0])


def stage_offer(connection, batch_id, supplier_link_id, unit_cost, currency,
                availability_state, available_quantity=None, tax_inclusive=False,
                minimum_order_quantity=1, pack_quantity=1, lead_time_days=None,
                supplier_product_url=None, source_updated_at=None,
                valid_until=None, raw_data=None):
    """Insert/update an offer inside ONE generation. Never touches live state."""
    from decimal import Decimal, InvalidOperation
    validate_availability(availability_state, available_quantity)
    try:
        cost = Decimal(str(unit_cost))
        moq = Decimal(str(minimum_order_quantity))
        pack = Decimal(str(pack_quantity))
    except (InvalidOperation, TypeError, ValueError):
        raise SnapshotValidationError("Invalid offer commercial values") from None
    if not cost.is_finite() or cost < 0:
        raise SnapshotValidationError("Unit cost must be finite and non-negative")
    if not moq.is_finite() or moq <= 0 or not pack.is_finite() or pack <= 0:
        raise SnapshotValidationError("MOQ and pack quantity must be positive")
    cur_code = str(currency or "").upper().strip()
    if len(cur_code) != 3 or not cur_code.isalpha():
        raise SnapshotValidationError("Three-letter currency required")
    with _atomic():
        with connection.cursor() as cur:
            cur.execute(
                "SELECT supplier_id FROM catalogue.supplier_import_batches "
                "WHERE id=%s", (batch_id,),
            )
            owner = cur.fetchone()
            if not owner:
                raise SnapshotError("Snapshot batch not found")
            lock_supplier(cur, owner[0])
            cur.execute(
                "SELECT b.supplier_id, b.snapshot_status, b.status, l.supplier_id "
                "FROM catalogue.supplier_import_batches b "
                "JOIN catalogue.product_supplier_links l ON l.id=%s "
                "WHERE b.id=%s FOR UPDATE OF b",
                (supplier_link_id, batch_id),
            )
            row = cur.fetchone()
            if not row:
                raise SnapshotError("Batch or supplier link not found")
            b_sup, snap_state, b_state, l_sup = row
            if str(b_sup) != str(l_sup):
                raise SnapshotError("Supplier link belongs to another supplier")
            if snap_state != "STAGING" or b_state != "PROCESSING":
                raise SnapshotError("Offers may only be staged into a STAGING batch")
            cur.execute(
                "INSERT INTO catalogue.supplier_offers "
                "(supplier_link_id, snapshot_batch_id, unit_cost, currency, "
                " tax_inclusive, available_quantity, availability_state, "
                " minimum_order_quantity, pack_quantity, lead_time_days, "
                " supplier_product_url, source_updated_at, valid_until, raw_data, "
                " is_active, observed_at, last_seen_at, updated_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,"
                "        TRUE, NOW(), NOW(), NOW()) "
                "ON CONFLICT (supplier_link_id, snapshot_batch_id) DO UPDATE SET "
                " unit_cost=EXCLUDED.unit_cost, currency=EXCLUDED.currency, "
                " tax_inclusive=EXCLUDED.tax_inclusive, "
                " available_quantity=EXCLUDED.available_quantity, "
                " availability_state=EXCLUDED.availability_state, "
                " minimum_order_quantity=EXCLUDED.minimum_order_quantity, "
                " pack_quantity=EXCLUDED.pack_quantity, "
                " lead_time_days=EXCLUDED.lead_time_days, "
                " supplier_product_url=EXCLUDED.supplier_product_url, "
                " source_updated_at=EXCLUDED.source_updated_at, "
                " valid_until=EXCLUDED.valid_until, raw_data=EXCLUDED.raw_data, "
                " is_active=TRUE, last_seen_at=NOW(), updated_at=NOW() "
                "RETURNING id",
                (supplier_link_id, batch_id, cost, cur_code, bool(tax_inclusive),
                 available_quantity, availability_state, moq, pack, lead_time_days,
                 supplier_product_url, source_updated_at, valid_until,
                 json.dumps(raw_data or {}, default=str)),
            )
            return str(cur.fetchone()[0])


def mark_ready(connection, batch_id, counts, previous_rows=None, thresholds=None):
    """Whole-feed validation gate. Only a clean FULL feed becomes READY."""
    ok, reasons, summary = evaluate_thresholds(counts, previous_rows, thresholds)
    with _atomic():
        with connection.cursor() as cur:
            cur.execute(
                "SELECT supplier_id FROM catalogue.supplier_import_batches "
                "WHERE id=%s", (batch_id,),
            )
            owner = cur.fetchone()
            if not owner:
                raise SnapshotError("Snapshot batch not found")
            lock_supplier(cur, owner[0])
            cur.execute(
                "SELECT snapshot_status, status, total_rows, feed_kind "
                "FROM catalogue.supplier_import_batches WHERE id=%s FOR UPDATE",
                (batch_id,),
            )
            state, b_state, total_rows, feed_kind = cur.fetchone()
            if state not in ("STAGING", "VALIDATING"):
                raise SnapshotError("Only STAGING/VALIDATING batches can be validated")
            cur.execute(
                "SELECT COUNT(*) FROM catalogue.supplier_offers "
                "WHERE snapshot_batch_id=%s", (batch_id,),
            )
            actual = int(cur.fetchone()[0])
            if actual != int(counts.get("inserted_offers", -1)):
                reasons = reasons + ["PERSISTED_OFFER_COUNT_MISMATCH"]
                ok = False
            if feed_kind == "FULL" and int(counts.get("source_rows", -1)) != int(total_rows):
                reasons = reasons + ["FULL_FEED_ROW_COUNT_MISMATCH"]
                ok = False
            summary["persisted_offers"] = actual
            summary["reasons"] = reasons
            target = "READY" if ok else "REJECTED"
            check_transition(state, target)
            cur.execute(
                "UPDATE catalogue.supplier_import_batches "
                "SET snapshot_status=%s, status=%s, validation_summary=%s::jsonb, "
                "    completed_at=CASE WHEN %s='READY' THEN NOW() ELSE completed_at END "
                "WHERE id=%s",
                (target, "COMPLETED" if ok else "COMPLETED_WITH_ERRORS",
                 json.dumps(summary, default=str), target, batch_id),
            )
    return {"ready": ok, "reasons": reasons, "summary": summary}


def promote(connection, batch_id, actor, reason,
            expected_active_batch_id="__UNSET__", allow_unordered=False):
    """Atomically switch ONE supplier pointer. Previous generation retained."""
    if not actor or not reason:
        raise SnapshotValidationError("actor and reason are required")
    with _atomic():
        with connection.cursor() as cur:
            cur.execute(
                "SELECT supplier_id FROM catalogue.supplier_import_batches "
                "WHERE id=%s", (batch_id,),
            )
            owner = cur.fetchone()
            if not owner:
                raise SnapshotError("Candidate snapshot not found")
            supplier_id = owner[0]
            lock_supplier(cur, supplier_id)
            cur.execute(
                "INSERT INTO catalogue.supplier_catalogue_state "
                "(supplier_id, updated_at) VALUES (%s, NOW()) "
                "ON CONFLICT (supplier_id) DO NOTHING", (supplier_id,),
            )
            cur.execute(
                "SELECT active_batch_id FROM catalogue.supplier_catalogue_state "
                "WHERE supplier_id=%s FOR UPDATE", (supplier_id,),
            )
            active_id = cur.fetchone()[0]
            if expected_active_batch_id != "__UNSET__":
                if str(active_id) != str(expected_active_batch_id):
                    raise SnapshotError("Active pointer changed; compare-and-swap failed")
            if str(active_id) == str(batch_id):
                raise SnapshotError("Candidate is already the active snapshot")
            cur.execute(
                "SELECT supplier_id, snapshot_status, status, feed_kind, total_rows, "
                "       validation_summary, source_sequence, source_updated_at "
                "FROM catalogue.supplier_import_batches WHERE id=%s FOR UPDATE",
                (batch_id,),
            )
            cand = cur.fetchone()
            if not cand:
                raise SnapshotError("Candidate snapshot disappeared")
            (c_sup, c_state, c_bstate, c_kind, c_rows, c_summary, c_seq, c_ts) = cand
            if str(c_sup) != str(supplier_id):
                raise SnapshotError("Candidate supplier ownership mismatch")
            if c_state != "READY" or c_bstate != "COMPLETED":
                raise SnapshotError("Candidate is not READY and COMPLETED")
            if c_kind != "FULL":
                raise SnapshotError("Only FULL snapshots may replace the active generation")
            c_summary = c_summary or {}
            if isinstance(c_summary, str):
                c_summary = json.loads(c_summary)
            if c_summary.get("reasons"):
                raise SnapshotValidationError("Stored validation contains failures")
            if int(c_summary.get("source_rows", -1)) != int(c_rows):
                raise SnapshotValidationError("Stored validation does not cover full batch")
            a_seq = a_ts = None
            if active_id is not None:
                cur.execute(
                    "SELECT source_sequence, source_updated_at "
                    "FROM catalogue.supplier_import_batches WHERE id=%s", (active_id,),
                )
                a_seq, a_ts = cur.fetchone()
            # A supplier's FIRST promotion has no active generation to
            # be newer than; ordering is trivially satisfied.
            if active_id is not None:
                assert_newer(c_seq, c_ts, a_seq, a_ts,
                             allow_unordered=allow_unordered)
            if active_id is not None:
                cur.execute(
                    "UPDATE catalogue.supplier_import_batches "
                    "SET snapshot_status='SUPERSEDED' "
                    "WHERE id=%s AND supplier_id=%s AND snapshot_status='PROMOTED'",
                    (active_id, supplier_id),
                )
                if cur.rowcount != 1:
                    raise SnapshotError("Active generation state is inconsistent")
            cur.execute(
                "UPDATE catalogue.supplier_import_batches "
                "SET snapshot_status='PROMOTED', promoted_at=NOW() "
                "WHERE id=%s AND supplier_id=%s AND snapshot_status='READY'",
                (batch_id, supplier_id),
            )
            if cur.rowcount != 1:
                raise SnapshotError("Candidate promotion state changed concurrently")
            cur.execute(
                "UPDATE catalogue.supplier_catalogue_state "
                "SET previous_batch_id=active_batch_id, active_batch_id=%s, "
                "    promoted_at=NOW(), updated_at=NOW() WHERE supplier_id=%s",
                (batch_id, supplier_id),
            )
            cur.execute(
                "INSERT INTO catalogue.supplier_snapshot_events "
                "(supplier_id, event_type, from_batch_id, to_batch_id, actor, reason) "
                "VALUES (%s,'PROMOTE',%s,%s,%s,%s)",
                (supplier_id, active_id, batch_id, actor, reason),
            )
    return {"supplier_id": str(supplier_id),
            "previous_batch_id": str(active_id) if active_id else None,
            "active_batch_id": str(batch_id)}


def rollback(connection, supplier_id, actor, reason):
    """One-step restore of the immediate previous generation."""
    supplier_id = str(supplier_id).strip().lower()
    if not actor or not reason:
        raise SnapshotValidationError("actor and reason are required")
    with _atomic():
        with connection.cursor() as cur:
            lock_supplier(cur, supplier_id)
            cur.execute(
                "SELECT active_batch_id, previous_batch_id "
                "FROM catalogue.supplier_catalogue_state "
                "WHERE supplier_id=%s FOR UPDATE", (supplier_id,),
            )
            row = cur.fetchone()
            if not row or not row[1]:
                raise SnapshotError("No previous snapshot available for rollback")
            active_id, target_id = row
            cur.execute(
                "SELECT supplier_id, snapshot_status, status, feed_kind "
                "FROM catalogue.supplier_import_batches WHERE id=%s FOR UPDATE",
                (target_id,),
            )
            t = cur.fetchone()
            if not t or str(t[0]) != supplier_id:
                raise SnapshotError("Rollback target supplier mismatch")
            if t[1] != "SUPERSEDED" or t[2] != "COMPLETED" or t[3] != "FULL":
                raise SnapshotError("Rollback target is not a completed FULL snapshot")
            cur.execute(
                "UPDATE catalogue.supplier_import_batches "
                "SET snapshot_status='SUPERSEDED' "
                "WHERE id=%s AND snapshot_status='PROMOTED'", (active_id,),
            )
            if cur.rowcount != 1:
                raise SnapshotError("Current generation state is inconsistent")
            cur.execute(
                "UPDATE catalogue.supplier_import_batches "
                "SET snapshot_status='PROMOTED', promoted_at=NOW() "
                "WHERE id=%s AND snapshot_status='SUPERSEDED'", (target_id,),
            )
            if cur.rowcount != 1:
                raise SnapshotError("Rollback target transition failed")
            cur.execute(
                "UPDATE catalogue.supplier_catalogue_state "
                "SET active_batch_id=%s, previous_batch_id=NULL, "
                "    promoted_at=NOW(), updated_at=NOW() WHERE supplier_id=%s",
                (target_id, supplier_id),
            )
            cur.execute(
                "INSERT INTO catalogue.supplier_snapshot_events "
                "(supplier_id, event_type, from_batch_id, to_batch_id, actor, reason) "
                "VALUES (%s,'ROLLBACK',%s,%s,%s,%s)",
                (supplier_id, active_id, target_id, actor, reason),
            )
    return {"supplier_id": supplier_id,
            "previous_batch_id": str(active_id),
            "active_batch_id": str(target_id)}
