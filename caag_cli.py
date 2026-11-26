#!/usr/bin/env python3
"""Unified CAAG importer CLI - one entry point for the snapshot lifecycle.

Read-only by default. Every write requires --confirm-write.
Never invokes the legacy runner, Saleor writer, inventory or media workflows
except through the explicitly gated `images` subcommand.
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


def readonly():
    from django.db import connection, transaction
    return connection, transaction


def cmd_doctor(args):
    from importer.engine.canonical import lifecycle_authority as la
    from django.db import connection, transaction
    inv = la.inventory(IMPORTER_ROOT / "engine/canonical")
    missing = {}
    with transaction.atomic():
        with connection.cursor() as cur:
            cur.execute("SET TRANSACTION READ ONLY")
            for table, cols in {
                "supplier_import_batches": ["snapshot_status", "feed_kind",
                                            "source_sequence", "validation_summary"],
                "supplier_offers": ["snapshot_batch_id", "availability_state",
                                    "last_seen_at", "retired_at"],
            }.items():
                cur.execute("SELECT column_name FROM information_schema.columns "
                            "WHERE table_schema='catalogue' AND table_name=%s", (table,))
                have = {r[0] for r in cur.fetchall()}
                gap = [c for c in cols if c not in have]
                if gap:
                    missing[table] = gap
            for t in ("supplier_catalogue_state", "supplier_snapshot_events"):
                cur.execute("SELECT to_regclass(%s)", ("catalogue." + t,))
                if cur.fetchone()[0] is None:
                    missing[t] = ["TABLE ABSENT"]
    emit({"ok": inv["authoritative_complete"] and not missing,
          "operation": "doctor", "read_only": True,
          "authority": inv, "schema_missing": missing,
          "migration_required": bool(missing)})
    return 0 if not missing else 2


def cmd_migrate(args):
    from django.db import connection
    from importer.migrations.migration_runner import run_migrations
    result = run_migrations(connection, IMPORTER_ROOT / "migrations",
                            apply=bool(args.confirm_write))
    emit({"ok": True, "operation": "migrate",
          "applied": bool(args.confirm_write), **result})
    return 0


def cmd_status(args):
    from importer.engine.canonical.active_catalogue import supplier_snapshot_status
    from django.db import connection, transaction
    with transaction.atomic():
        with connection.cursor() as cur:
            cur.execute("SET TRANSACTION READ ONLY")
        rows = supplier_snapshot_status(connection)
    emit({"ok": True, "operation": "status", "read_only": True, "suppliers": rows})
    return 0


def cmd_ingest(args):
    if not args.confirm_write:
        emit({"ok": False, "operation": "ingest",
              "error": "Database write refused: pass --confirm-write explicitly"})
        return 6
    from django.db import connection
    from importer.engine.canonical import snapshot_ingest as si
    try:
        result = si.ingest_dickerdata(connection, args.file,
                                      supplier_id=args.supplier_id,
                                      source_sequence=args.source_sequence)
    except Exception as exc:
        emit({"ok": False, "operation": "ingest",
              "error_type": type(exc).__name__, "error": str(exc)[:1200]})
        return 5
    emit({"ok": bool(result["validation"]["ready"]), "operation": "ingest", **result})
    return 0 if result["validation"]["ready"] else 2


def cmd_retirement(args):
    from django.db import connection, transaction
    from importer.engine.canonical import offer_writer as ow
    with transaction.atomic():
        with connection.cursor() as cur:
            cur.execute("SET TRANSACTION READ ONLY")
        report = ow.retirement_report(connection, args.supplier_id, args.batch_id)
    emit({"ok": True, "operation": "retirement", "read_only": True, **report})
    return 0


def cmd_promote(args):
    if not args.confirm_write:
        emit({"ok": False, "operation": "promote",
              "error": "Database write refused: pass --confirm-write explicitly"})
        return 6
    from django.db import connection
    from importer.engine.canonical import snapshot_engine as se
    try:
        result = se.promote(connection, args.batch_id, actor=args.actor,
                            reason=args.reason,
                            allow_unordered=args.allow_unordered)
    except Exception as exc:
        emit({"ok": False, "operation": "promote",
              "error_type": type(exc).__name__, "error": str(exc)[:1200]})
        return 5
    emit({"ok": True, "operation": "promote", **result})
    return 0


def cmd_rollback(args):
    if not args.confirm_write:
        emit({"ok": False, "operation": "rollback",
              "error": "Database write refused: pass --confirm-write explicitly"})
        return 6
    from django.db import connection
    from importer.engine.canonical import snapshot_engine as se
    try:
        result = se.rollback(connection, args.supplier_id, actor=args.actor,
                             reason=args.reason)
    except Exception as exc:
        emit({"ok": False, "operation": "rollback",
              "error_type": type(exc).__name__, "error": str(exc)[:1200]})
        return 5
    emit({"ok": True, "operation": "rollback", **result})
    return 0


def cmd_select(args):
    from django.db import connection, transaction
    from importer.engine.canonical import offer_selection as osel
    with transaction.atomic():
        with connection.cursor() as cur:
            cur.execute("SET TRANSACTION READ ONLY")
        result = osel.select(connection, args.canonical_product_id,
                             requested_quantity=args.quantity,
                             currency=args.currency,
                             allow_unknown=args.allow_unknown_stock)
    emit({"ok": True, "operation": "select", "read_only": True, **result})
    return 0


def cmd_visibility(args):
    from django.db import connection, transaction
    from importer.engine.canonical import visibility_policy as vp
    with transaction.atomic():
        with connection.cursor() as cur:
            cur.execute("SET TRANSACTION READ ONLY")
        report = vp.evaluate(connection, allow_unknown=args.allow_unknown_stock,
                             limit=args.limit)
    if not args.apply:
        emit({"ok": True, "operation": "visibility",
              "decisions": report.pop("decisions")[:20], **report})
        return 0
    if not args.confirm_write:
        emit({"ok": False, "operation": "visibility",
              "error": "Database write refused: pass --confirm-write explicitly"})
        return 6
    actions = [vp.WITHDRAW] + ([vp.REACTIVATE] if args.reactivate else [])
    result = vp.apply(connection, report["decisions"], confirm=True,
                      max_changes=args.max_changes, actions=tuple(actions))
    emit({"ok": bool(result.get("applied")), "operation": "visibility",
          "counts": report["counts"], **result})
    return 0 if result.get("applied") else 5


def cmd_images(args):
    from django.db import connection, transaction
    from importer.engine import image_pipeline as ip
    if not args.apply:
        with transaction.atomic():
            with connection.cursor() as cur:
                cur.execute("SET TRANSACTION READ ONLY")
            report = ip.plan(connection, limit=args.limit,
                             allow_unknown=args.allow_unknown_stock,
                             force=args.force)
        emit({"ok": True, "operation": "images",
              "decisions": report.pop("decisions")[:20], **report})
        return 0
    if not args.confirm_write:
        emit({"ok": False, "operation": "images",
              "error": "Media write refused: pass --confirm-write explicitly"})
        return 6
    result = ip.run(connection, limit=args.limit,
                    allow_unknown=args.allow_unknown_stock,
                    force=args.force, confirm=True)
    result.pop("decisions", None)
    emit({"ok": bool(result.get("applied")), "operation": "images", **result})
    return 0 if result.get("applied") else 5


def build_parser():
    p = argparse.ArgumentParser(description="CAAG unified importer CLI")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor").set_defaults(handler=cmd_doctor)
    m = sub.add_parser("migrate")
    m.add_argument("--confirm-write", action="store_true")
    m.set_defaults(handler=cmd_migrate)
    sub.add_parser("status").set_defaults(handler=cmd_status)
    i = sub.add_parser("ingest")
    i.add_argument("--file", required=True)
    i.add_argument("--supplier-id", default="dickerdata")
    i.add_argument("--source-sequence", type=int, default=None)
    i.add_argument("--confirm-write", action="store_true")
    i.set_defaults(handler=cmd_ingest)
    r = sub.add_parser("retirement")
    r.add_argument("--supplier-id", required=True)
    r.add_argument("--batch-id", required=True)
    r.set_defaults(handler=cmd_retirement)
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
    s = sub.add_parser("select")
    s.add_argument("--canonical-product-id", required=True)
    s.add_argument("--quantity", type=int, default=1)
    s.add_argument("--currency", default="AUD")
    s.add_argument("--allow-unknown-stock", action="store_true")
    s.set_defaults(handler=cmd_select)
    v = sub.add_parser("visibility")
    v.add_argument("--limit", type=int, default=None)
    v.add_argument("--allow-unknown-stock", action="store_true")
    v.add_argument("--apply", action="store_true")
    v.add_argument("--reactivate", action="store_true")
    v.add_argument("--max-changes", type=int, default=500)
    v.add_argument("--confirm-write", action="store_true")
    v.set_defaults(handler=cmd_visibility)
    im = sub.add_parser("images")
    im.add_argument("--limit", type=int, default=20)
    im.add_argument("--allow-unknown-stock", action="store_true")
    im.add_argument("--force", action="store_true")
    im.add_argument("--apply", action="store_true")
    im.add_argument("--confirm-write", action="store_true")
    im.set_defaults(handler=cmd_images)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    bootstrap()
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
