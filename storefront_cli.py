#!/usr/bin/env python3
"""Storefront price/cost sync CLI. Plan read-only; apply needs --confirm-write."""
import argparse, json, os, sys
from pathlib import Path
IMPORTER_ROOT = Path(__file__).resolve().parent
IMPORTS_ROOT = IMPORTER_ROOT.parent

def emit(p): print(json.dumps(p, indent=2, default=str, sort_keys=True))

def bootstrap():
    for p in ("/app", str(IMPORTS_ROOT)):
        if Path(p).is_dir() and p not in sys.path:
            sys.path.insert(0, p)
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "saleor.settings")
    import django; django.setup()

def cmd_plan(args):
    from django.db import connection
    from importer.engine.canonical import storefront_sync as ss
    r = ss.plan(connection)
    r["updates_sample"] = r.pop("updates")[:args.show]
    r["below_cost_sample"] = r.pop("below_cost_review")[:args.show]
    emit({"ok":True, "operation":"plan", **r})
    return 0

def cmd_apply(args):
    from django.db import connection
    from importer.engine.canonical import storefront_sync as ss
    if not args.confirm_write:
        emit({"ok":False, "operation":"apply",
              "error":"Write refused: pass --confirm-write"}); return 6
    r = ss.plan(connection)
    updates = r["updates"]
    if args.limit: updates = updates[:args.limit]
    out = ss.apply(connection, updates, confirm=True, max_changes=args.max_changes)
    emit({"ok":bool(out.get("applied")), "operation":"apply",
          "planned":len(updates), **out})
    return 0 if out.get("applied") else 5

def build_parser():
    p = argparse.ArgumentParser(description="Storefront price/cost sync")
    sub = p.add_subparsers(dest="command", required=True)
    pl = sub.add_parser("plan"); pl.add_argument("--show", type=int, default=10)
    pl.set_defaults(handler=cmd_plan)
    ap = sub.add_parser("apply")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--max-changes", type=int, default=2000)
    ap.add_argument("--confirm-write", action="store_true")
    ap.set_defaults(handler=cmd_apply)
    return p

def main(argv=None):
    args = build_parser().parse_args(argv); bootstrap(); return args.handler(args)

if __name__ == "__main__":
    sys.exit(main())
