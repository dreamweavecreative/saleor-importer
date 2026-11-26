#!/usr/bin/env python3
"""Gated supplier snapshot flow: sequence -> validate -> promote -> rollback.

Staging and reconciliation stay in supplier_cli.py. This CLI supplies the
snapshot steps that must run between reconcile and promotion.
Every write requires --confirm-write.
"""
import argparse
import json
import os
import sys
from pathlib import Path

IMPORTER_ROOT = Path(__file__).resolve().parent
IMPORTS_ROOT = IMPORTER_ROOT.parent


def emit(p):
    print(json.dumps(p, indent=2, default=str, sort_keys=True))


def bootstrap():
    for p in ("/app", str(IMPORTS_ROOT)):
        if Path(p).is_dir() and p not in sys.path:
            sys.path.insert(0, p)
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "saleor.settings")
    import django
    django.setup()


def _snapshot(batch_id):
    from django.db import connection
    with connection.cursor() as c:
        c.execute("""
            SELECT supplier_id, status, snapshot_status, feed_kind,
                   total_rows, source_sequence
            FROM catalogue.supplier_import_batches WHERE id=%s
        """, (batch_id,))
        row = c.fetchone()
        if not row:
            raise ValueError("Batch not found: " + str(batch_id))
        c.execute("""
            SELECT status, COUNT(*) FROM catalogue.supplier_import_rows
            WHERE batch_id=%s GROUP BY status
        """, (batch_id,))
        rows = dict(c.fetchall())
        c.execute("""
            SELECT COUNT(*) FROM catalogue.supplier_offers
            WHERE snapshot_batch_id=%s
        """, (batch_id,))
        offers = c.fetchone()[0]
    return {"supplier_id": row[0], "status": row[1], "snapshot_status": row[2],
            "feed_kind": row[3], "total_rows": row[4], "source_sequence": row[5],
            "row_statuses": rows, "offers": offers}


def cmd_status(args):
    from django.db import connection, transaction
    with transaction.atomic():
        with connection.cursor() as c:
            c.execute("SET TRANSACTION READ ONLY")
        info = _snapshot(args.batch_id)
    emit({"ok": True, "operation": "status", "read_only": True,
          "batch_id": args.batch_id, **info})
    return 0


def cmd_sequence(args):
    if not args.confirm_write:
        emit({"ok": False, "operation": "sequence",
              "error": "Database write refused: pass --confirm-write"})
        return 6
    from django.db import connection, transaction
    from importer.engine.canonical import snapshot_engine as se
    with transaction.atomic():
        with connection.cursor() as c:
            c.execute("""
                SELECT supplier_id, snapshot_status
                FROM catalogue.supplier_import_batches WHERE id=%s
            """, (args.batch_id,))
            row = c.fetchone()
            if not row:
                emit({"ok": False, "error": "Batch not found"}); return 5
            se.lock_supplier(c, row[0])
            if row[1] not in ("STAGING", "VALIDATING", "READY"):
                emit({"ok": False,
                      "error": "Batch already advanced: " + row[1]}); return 5
            c.execute("""
                UPDATE catalogue.supplier_import_batches
                SET source_sequence=%s WHERE id=%s
            """, (args.sequence, args.batch_id))
    emit({"ok": True, "operation": "sequence", "batch_id": args.batch_id,
          "source_sequence": args.sequence})
    return 0


def cmd_validate(args):
    from django.db import connection
    from importer.engine.canonical import snapshot_engine as se
    info = _snapshot(args.batch_id)
    if not args.confirm_write:
        emit({"ok": False, "operation": "validate",
              "error": "Database write refused: pass --confirm-write", **info})
        return 6
    rows = info["row_statuses"]
    counts = {
        "source_rows": int(info["total_rows"]),
        "eligible_rows": int(info["total_rows"]),
        "excluded_rows": 0,
        "matched_rows": int(rows.get("MATCHED", 0)),
        "inserted_offers": int(info["offers"]),
        "unmatched_rows": int(rows.get("UNMATCHED", 0)),
        "invalid_rows": int(rows.get("INVALID", 0)),
        "duplicate_skus": 0,
    }
    previous = None
    thresholds = None
    with connection.cursor() as c:
        c.execute("""
            SELECT b.total_rows
            FROM catalogue.supplier_catalogue_state s
            JOIN catalogue.supplier_import_batches b ON b.id=s.active_batch_id
            WHERE s.supplier_id=%s AND b.id <> %s
        """, (info["supplier_id"], args.batch_id))
        r = c.fetchone()
        if r:
            previous = int(r[0])
        # Load supplier-specific thresholds from policy
        c.execute("""
            SELECT thresholds FROM catalogue.supplier_policy
            WHERE supplier_id=%s
        """, (info["supplier_id"],))
        row = c.fetchone()
        if row and row[0]:
            import json
            thresholds = json.loads(row[0])
    result = se.mark_ready(connection, args.batch_id, counts,
                           previous_rows=previous, thresholds=thresholds)
    emit({"ok": bool(result["ready"]), "operation": "validate",
          "batch_id": args.batch_id, "previous_eligible_rows": previous,
          "thresholds_used": thresholds, **result})
    return 0 if result["ready"] else 2


def cmd_promote(args):
    if not args.confirm_write:
        emit({"ok": False, "operation": "promote",
              "error": "Database write refused: pass --confirm-write"})
        return 6
    from django.db import connection
    from importer.engine.canonical import snapshot_engine as se
    try:
        result = se.promote(connection, args.batch_id, actor=args.actor,
                            reason=args.reason,
                            allow_unordered=args.allow_unordered)
    except Exception as exc:
        emit({"ok": False, "operation": "promote",
              "error_type": type(exc).__name__, "error": str(exc)[:900]})
        return 5
    emit({"ok": True, "operation": "promote", **result})

    # Post-promotion visibility recalculation (separate transactions).
    if not args.skip_visibility:
        try:
            vis = _recalculate_visibility(connection, "post-promote")
            emit({"ok": True, "operation": "post_promote_visibility", **vis})
        except Exception as exc:
            print("WARNING: post-promote visibility failed: " + str(exc),
                  file=sys.stderr)
            emit({"ok": True, "operation": "post_promote_visibility",
                  "error": str(exc)[:500],
                  "note": "Promotion succeeded. Visibility needs manual recalculation."})
    else:
        print("Visibility recalculation skipped (--skip-visibility).")

    return 0


def cmd_rollback(args):
    if not args.confirm_write:
        emit({"ok": False, "operation": "rollback",
              "error": "Database write refused: pass --confirm-write"})
        return 6
    from django.db import connection
    from importer.engine.canonical import snapshot_engine as se
    try:
        result = se.rollback(connection, args.supplier_id, actor=args.actor,
                             reason=args.reason)
    except Exception as exc:
        emit({"ok": False, "operation": "rollback",
              "error_type": type(exc).__name__, "error": str(exc)[:900]})
        return 5
    emit({"ok": True, "operation": "rollback", **result})
    return 0




def _recalculate_visibility(connection, label):
    """Post-promotion catalogue state: prices, withdrawal, safe reactivation.

    Runs AFTER the promotion transaction commits. Delegates to post_promotion,
    which executes each phase in its own bounded transactions.
    """
    from importer.engine.canonical import post_promotion as pp

    print("\n--- Post-promotion catalogue sync (" + label + ") ---")
    before = pp.plan(connection)
    print("  price updates pending :", before["price_update_count"])
    print("  withdrawals pending   :", before["withdraw_count"])
    print("  reactivations pending :", before["reactivate_count"])
    print("  action ledger present :", before["ledger_present"])
    if not before["ledger_present"]:
        print("  NOTE: reactivation disabled until migration 0007 is applied.")

    if (before["price_update_count"] == 0 and before["withdraw_count"] == 0
            and before["reactivate_count"] == 0):
        print("  Catalogue already consistent. No action needed.")
        return {"prices": 0, "withdrawn": 0, "reactivated": 0, "errors": 0}

    out = pp.run(connection, confirm=True, max_visibility_changes=50000)
    ph = out.get("phases", {})
    print("  prices synced   :", ph.get("prices", {}).get("variant_listings_changed", 0))
    print("  withdrawn       :", ph.get("withdraw", {}).get("variant_listings_changed", 0))
    print("  reactivated     :", ph.get("reactivate", {}).get("variants", 0))
    return {"prices": ph.get("prices", {}).get("variant_listings_changed", 0),
            "withdrawn": ph.get("withdraw", {}).get("variant_listings_changed", 0),
            "reactivated": ph.get("reactivate", {}).get("variants", 0),
            "errors": 0}




def build_parser():
    p = argparse.ArgumentParser(description="Gated supplier snapshot flow")
    sub = p.add_subparsers(dest="command", required=True)
    s = sub.add_parser("status"); s.add_argument("--batch-id", required=True)
    s.set_defaults(handler=cmd_status)
    q = sub.add_parser("sequence")
    q.add_argument("--batch-id", required=True)
    q.add_argument("--sequence", type=int, required=True)
    q.add_argument("--confirm-write", action="store_true")
    q.set_defaults(handler=cmd_sequence)
    v = sub.add_parser("validate")
    v.add_argument("--batch-id", required=True)
    v.add_argument("--confirm-write", action="store_true")
    v.set_defaults(handler=cmd_validate)
    pr = sub.add_parser("promote")
    pr.add_argument("--batch-id", required=True)
    pr.add_argument("--actor", required=True)
    pr.add_argument("--reason", required=True)
    pr.add_argument("--allow-unordered", action="store_true")
    pr.add_argument("--confirm-write", action="store_true")
    pr.add_argument("--skip-visibility", action="store_true",
                    help="Skip post-promotion visibility recalculation")
    pr.set_defaults(handler=cmd_promote)
    rb = sub.add_parser("rollback")
    rb.add_argument("--supplier-id", required=True)
    rb.add_argument("--actor", required=True)
    rb.add_argument("--reason", required=True)
    rb.add_argument("--confirm-write", action="store_true")
    rb.set_defaults(handler=cmd_rollback)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    bootstrap()
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
