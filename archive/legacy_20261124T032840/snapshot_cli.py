#!/usr/bin/env python3
"""Supplier snapshot lifecycle CLI.

Read-only by default. Every write requires --confirm-write.
Never invokes the legacy runner, Saleor writer, inventory or media workflows.
"""
import argparse
import json
import os
import sys
from pathlib import Path

IMPORTER_ROOT = Path(__file__).resolve().parent
IMPORTS_ROOT = IMPORTER_ROOT.parent


def emit(payload):
    print(json.dumps(payload, indent=2, default=str, sort_keys=True))


def bootstrap():
    for p in ("/app", str(IMPORTS_ROOT)):
        if Path(p).is_dir() and p not in sys.path:
            sys.path.insert(0, p)
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "saleor.settings")
    import django
    django.setup()


REQUIRED_COLUMNS = {
    "supplier_import_batches": ["snapshot_status", "feed_kind", "source_sequence",
                                "source_updated_at", "validation_summary", "promoted_at"],
    "supplier_offers": ["snapshot_batch_id", "availability_state", "last_seen_at"],
}
REQUIRED_TABLES = ["supplier_catalogue_state", "supplier_snapshot_events"]


def cmd_schema_check(args):
    from django.db import connection, transaction
    missing = {}
    with transaction.atomic():
        with connection.cursor() as cur:
            cur.execute("SET TRANSACTION READ ONLY")
            for table, cols in REQUIRED_COLUMNS.items():
                cur.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema='catalogue' AND table_name=%s", (table,))
                have = {r[0] for r in cur.fetchall()}
                gap = [c for c in cols if c not in have]
                if gap:
                    missing[table] = gap
            for table in REQUIRED_TABLES:
                cur.execute("SELECT to_regclass(%s)", ("catalogue." + table,))
                if cur.fetchone()[0] is None:
                    missing[table] = ["TABLE ABSENT"]
    emit({"ok": not missing, "operation": "schema-check", "read_only": True,
          "migration_required": bool(missing), "missing": missing})
    return 0 if not missing else 2


def cmd_status(args):
    from django.db import connection, transaction
    from importer.engine.canonical.active_catalogue import supplier_snapshot_status
    with transaction.atomic():
        with connection.cursor() as cur:
            cur.execute("SET TRANSACTION READ ONLY")
        rows = supplier_snapshot_status(connection)
    emit({"ok": True, "operation": "status", "read_only": True, "suppliers": rows})
    return 0


def cmd_visibility(args):
    from django.db import connection, transaction
    from importer.engine.canonical import active_catalogue as ac
    with transaction.atomic():
        with connection.cursor() as cur:
            cur.execute("SET TRANSACTION READ ONLY")
        report = ac.visibility_report(connection,
                                      allow_unknown=args.allow_unknown_stock,
                                      limit=args.limit)
    emit({"ok": True, "operation": "visibility", **report})
    return 0


def cmd_withdraw(args):
    from django.db import connection
    from importer.engine.canonical import active_catalogue as ac
    if not args.confirm_write:
        emit({"ok": False, "operation": "withdraw",
              "error": "Database write refused: pass --confirm-write explicitly"})
        return 6
    result = ac.apply_purchase_eligibility(
        connection, confirm=True, max_changes=args.max_changes,
        allow_unknown=args.allow_unknown_stock)
    emit({"ok": bool(result.get("applied")), "operation": "withdraw", **result})
    return 0 if result.get("applied") else 5


def cmd_promote(args):
    from django.db import connection
    from importer.engine.canonical import snapshot_engine as se
    if not args.confirm_write:
        emit({"ok": False, "operation": "promote",
              "error": "Database write refused: pass --confirm-write explicitly"})
        return 6
    try:
        result = se.promote(connection, args.batch_id, actor=args.actor,
                            reason=args.reason,
                            allow_unordered=args.allow_unordered)
    except Exception as exc:
        emit({"ok": False, "operation": "promote",
              "error_type": type(exc).__name__, "error": str(exc)[:800]})
        return 5
    emit({"ok": True, "operation": "promote", **result})
    return 0


def cmd_rollback(args):
    from django.db import connection
    from importer.engine.canonical import snapshot_engine as se
    if not args.confirm_write:
        emit({"ok": False, "operation": "rollback",
              "error": "Database write refused: pass --confirm-write explicitly"})
        return 6
    try:
        result = se.rollback(connection, args.supplier_id, actor=args.actor,
                             reason=args.reason)
    except Exception as exc:
        emit({"ok": False, "operation": "rollback",
              "error_type": type(exc).__name__, "error": str(exc)[:800]})
        return 5
    emit({"ok": True, "operation": "rollback", **result})
    return 0


def build_parser():
    p = argparse.ArgumentParser(description="Supplier snapshot lifecycle CLI")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("schema-check").set_defaults(handler=cmd_schema_check)
    sub.add_parser("status").set_defaults(handler=cmd_status)
    v = sub.add_parser("visibility")
    v.add_argument("--allow-unknown-stock", action="store_true")
    v.add_argument("--limit", type=int, default=None)
    v.set_defaults(handler=cmd_visibility)
    w = sub.add_parser("withdraw")
    w.add_argument("--allow-unknown-stock", action="store_true")
    w.add_argument("--max-changes", type=int, default=500)
    w.add_argument("--confirm-write", action="store_true")
    w.set_defaults(handler=cmd_withdraw)
    pr = sub.add_parser("promote")
    pr.add_argument("--batch-id", required=True)
    pr.add_argument("--actor", required=True)
    pr.add_argument("--reason", required=True)
    pr.add_argument("--allow-unordered", action="store_true")
    pr.add_argument("--confirm-write", action="store_true")
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
