"""Supplier-independent immutable snapshot lifecycle repository.

Connections are injected by the caller. This module does not create
database connections or run migrations.
"""
from __future__ import annotations

from contextlib import contextmanager


class SnapshotLifecycleError(RuntimeError):
    pass


class InvalidSnapshotTransition(SnapshotLifecycleError):
    pass


class SnapshotOrderingError(SnapshotLifecycleError):
    pass


ALLOWED_TRANSITIONS = {
    "STAGING": {"VALIDATING", "FAILED"},
    "VALIDATING": {"READY", "REJECTED", "FAILED"},
    "READY": {"FAILED"},
    "PROMOTED": set(),
    "SUPERSEDED": set(),
    "FAILED": set(),
    "REJECTED": set(),
    "LEGACY": set(),
}


class SnapshotLifecycleRepository:
    def __init__(self, connection):
        self.connection = connection

    @contextmanager
    def _transaction(self):
        try:
            yield
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    @staticmethod
    def assert_newer(
        candidate_sequence,
        candidate_timestamp,
        active_sequence,
        active_timestamp,
        *,
        allow_unordered=False,
    ):
        """Reject stale or ambiguous candidates; never guess ordering."""
        if candidate_sequence is not None or active_sequence is not None:
            if candidate_sequence is None or active_sequence is None:
                if not allow_unordered:
                    raise SnapshotOrderingError(
                        "Only one snapshot has a source sequence; ordering is ambiguous"
                    )
            else:
                if candidate_sequence <= active_sequence:
                    raise SnapshotOrderingError(
                        "Candidate source sequence is not newer than active snapshot"
                    )
                return

        if candidate_timestamp is not None or active_timestamp is not None:
            if candidate_timestamp is None or active_timestamp is None:
                if not allow_unordered:
                    raise SnapshotOrderingError(
                        "Only one snapshot has a source timestamp; ordering is ambiguous"
                    )
            else:
                if candidate_timestamp <= active_timestamp:
                    raise SnapshotOrderingError(
                        "Candidate source timestamp is not newer than active snapshot"
                    )
                return

        if not allow_unordered:
            raise SnapshotOrderingError(
                "No comparable supplier ordering metadata; explicit override required"
            )

    @staticmethod
    def validate_transition(current, target):
        if target not in ALLOWED_TRANSITIONS.get(current, set()):
            raise InvalidSnapshotTransition(
                f"Transition {current!r} -> {target!r} is not permitted"
            )

    @staticmethod
    def _lock_supplier(cursor, supplier_id):
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"catalogue:supplier:{supplier_id}",),
        )

    def get_active_batch_id(self, supplier_id):
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT active_batch_id
                FROM catalogue.supplier_catalogue_state
                WHERE supplier_id = %s
                """,
                (supplier_id,),
            )
            row = cursor.fetchone()
        return row[0] if row else None

    def transition(self, batch_id, target_state, validation_summary=None):
        with self._transaction():
            with self.connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT snapshot_status
                    FROM catalogue.supplier_import_batches
                    WHERE id = %s
                    FOR UPDATE
                    """,
                    (batch_id,),
                )
                row = cursor.fetchone()
                if not row:
                    raise SnapshotLifecycleError(
                        f"Import batch not found: {batch_id}"
                    )

                current = row[0]
                self.validate_transition(current, target_state)

                if validation_summary is None:
                    cursor.execute(
                        """
                        UPDATE catalogue.supplier_import_batches
                        SET snapshot_status = %s
                        WHERE id = %s
                        """,
                        (target_state, batch_id),
                    )
                else:
                    import json
                    cursor.execute(
                        """
                        UPDATE catalogue.supplier_import_batches
                        SET snapshot_status = %s,
                            validation_summary = %s::jsonb
                        WHERE id = %s
                        """,
                        (target_state, json.dumps(validation_summary), batch_id),
                    )

    def promote(
        self,
        batch_id,
        *,
        actor,
        reason,
        expected_active_batch_id=None,
        allow_unordered=False,
    ):
        if not actor or not reason:
            raise ValueError("actor and reason are required")

        with self._transaction():
            with self.connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id, supplier_id, snapshot_status,
                           source_sequence, source_updated_at
                    FROM catalogue.supplier_import_batches
                    WHERE id = %s
                    FOR UPDATE
                    """,
                    (batch_id,),
                )
                candidate = cursor.fetchone()
                if not candidate:
                    raise SnapshotLifecycleError(
                        f"Import batch not found: {batch_id}"
                    )

                (
                    _, supplier_id, candidate_status,
                    candidate_seq, candidate_ts
                ) = candidate

                if candidate_status != "READY":
                    raise SnapshotLifecycleError(
                        f"Only READY snapshots can be promoted; found {candidate_status}"
                    )

                self._lock_supplier(cursor, supplier_id)

                cursor.execute(
                    """
                    INSERT INTO catalogue.supplier_catalogue_state
                        (supplier_id, updated_at)
                    VALUES (%s, NOW())
                    ON CONFLICT (supplier_id) DO NOTHING
                    """,
                    (supplier_id,),
                )
                cursor.execute(
                    """
                    SELECT active_batch_id, previous_batch_id
                    FROM catalogue.supplier_catalogue_state
                    WHERE supplier_id = %s
                    FOR UPDATE
                    """,
                    (supplier_id,),
                )
                active_id, previous_id = cursor.fetchone()

                if expected_active_batch_id is not None:
                    if str(active_id) != str(expected_active_batch_id):
                        raise SnapshotLifecycleError(
                            "Active snapshot changed since caller's preflight"
                        )

                if str(active_id) == str(batch_id):
                    raise SnapshotLifecycleError(
                        "Candidate is already the active snapshot"
                    )

                if active_id is not None:
                    cursor.execute(
                        """
                        SELECT source_sequence, source_updated_at
                        FROM catalogue.supplier_import_batches
                        WHERE id = %s
                        """,
                        (active_id,),
                    )
                    active = cursor.fetchone()
                    if not active:
                        raise SnapshotLifecycleError(
                            "Active pointer references a missing batch"
                        )

                    self.assert_newer(
                        candidate_seq,
                        candidate_ts,
                        active[0],
                        active[1],
                        allow_unordered=allow_unordered,
                    )

                cursor.execute(
                    """
                    UPDATE catalogue.supplier_catalogue_state
                    SET previous_batch_id = active_batch_id,
                        active_batch_id = %s,
                        promoted_at = NOW(),
                        updated_at = NOW()
                    WHERE supplier_id = %s
                    """,
                    (batch_id, supplier_id),
                )

                if active_id is not None:
                    cursor.execute(
                        """
                        UPDATE catalogue.supplier_import_batches
                        SET snapshot_status = 'SUPERSEDED'
                        WHERE id = %s AND snapshot_status = 'PROMOTED'
                        """,
                        (active_id,),
                    )

                cursor.execute(
                    """
                    UPDATE catalogue.supplier_import_batches
                    SET snapshot_status = 'PROMOTED',
                        promoted_at = NOW()
                    WHERE id = %s AND snapshot_status = 'READY'
                    """,
                    (batch_id,),
                )
                if cursor.rowcount != 1:
                    raise SnapshotLifecycleError(
                        "Candidate state changed during promotion"
                    )

                cursor.execute(
                    """
                    INSERT INTO catalogue.supplier_snapshot_events
                        (supplier_id, event_type, from_batch_id,
                         to_batch_id, actor, reason)
                    VALUES (%s, 'PROMOTE', %s, %s, %s, %s)
                    """,
                    (supplier_id, active_id, batch_id, actor, reason),
                )

        return {
            "supplier_id": supplier_id,
            "previous_batch_id": active_id,
            "active_batch_id": batch_id,
        }

    def rollback(self, supplier_id, *, actor, reason, target_batch_id=None):
        if not actor or not reason:
            raise ValueError("actor and reason are required")

        with self._transaction():
            with self.connection.cursor() as cursor:
                self._lock_supplier(cursor, supplier_id)

                cursor.execute(
                    """
                    SELECT active_batch_id, previous_batch_id
                    FROM catalogue.supplier_catalogue_state
                    WHERE supplier_id = %s
                    FOR UPDATE
                    """,
                    (supplier_id,),
                )
                state = cursor.fetchone()
                if not state:
                    raise SnapshotLifecycleError(
                        f"No snapshot state exists for supplier {supplier_id}"
                    )

                active_id, previous_id = state
                target_id = target_batch_id or previous_id

                if target_id is None or str(target_id) != str(previous_id):
                    raise SnapshotLifecycleError(
                        "Rollback target must be the immediately previous snapshot"
                    )

                cursor.execute(
                    """
                    SELECT supplier_id, snapshot_status
                    FROM catalogue.supplier_import_batches
                    WHERE id = %s
                    FOR UPDATE
                    """,
                    (target_id,),
                )
                target = cursor.fetchone()

                if not target or target[0] != supplier_id:
                    raise SnapshotLifecycleError(
                        "Rollback target does not belong to this supplier"
                    )

                if target[1] != "SUPERSEDED":
                    raise SnapshotLifecycleError(
                        "Rollback target must be a SUPERSEDED snapshot"
                    )

                if active_id is not None:
                    cursor.execute(
                        """
                        UPDATE catalogue.supplier_import_batches
                        SET snapshot_status = 'SUPERSEDED'
                        WHERE id = %s AND snapshot_status = 'PROMOTED'
                        """,
                        (active_id,),
                    )

                cursor.execute(
                    """
                    UPDATE catalogue.supplier_import_batches
                    SET snapshot_status = 'PROMOTED',
                        promoted_at = NOW()
                    WHERE id = %s
                    """,
                    (target_id,),
                )

                cursor.execute(
                    """
                    UPDATE catalogue.supplier_catalogue_state
                    SET active_batch_id = %s,
                        previous_batch_id = %s,
                        promoted_at = NOW(),
                        updated_at = NOW()
                    WHERE supplier_id = %s
                    """,
                    (target_id, active_id, supplier_id),
                )

                cursor.execute(
                    """
                    INSERT INTO catalogue.supplier_snapshot_events
                        (supplier_id, event_type, from_batch_id,
                         to_batch_id, actor, reason)
                    VALUES (%s, 'ROLLBACK', %s, %s, %s, %s)
                    """,
                    (supplier_id, active_id, target_id, actor, reason),
                )

        return {
            "supplier_id": supplier_id,
            "previous_batch_id": active_id,
            "active_batch_id": target_id,
        }

    def get_current_offers(self, canonical_product_id):
        """Read offers belonging to each supplier's active generation."""
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT o.*, l.supplier_id
                FROM catalogue.supplier_offers o
                JOIN catalogue.product_supplier_links l
                  ON l.id = o.supplier_link_id
                JOIN catalogue.supplier_catalogue_state s
                  ON s.supplier_id = l.supplier_id
                 AND s.active_batch_id = o.snapshot_batch_id
                WHERE l.canonical_product_id = %s
                  AND o.is_active = TRUE
                  AND (o.valid_until IS NULL OR o.valid_until > NOW())
                ORDER BY o.unit_cost ASC
                """,
                (canonical_product_id,),
            )
            columns = [item[0] for item in cursor.description]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]
