#!/usr/bin/env python3
"""Publication withdrawal CLI. Plan is read-only; apply needs --confirm-write."""
import argparse, json, os, sys
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


def cmd_plan(args):
    from django.db import connection
    from importer.engine.canonical import visibility_apply as va
    result = va.plan(connection, reactivate=args.reactivate, limit=args.limit)
    actions = result.pop("actions")
    result["protected_checkout_items"] = actions[va.PROTECTED][:20]
    result["withdraw_sample"] = actions[va.WITHDRAW][:args.show] if args.show else []
    emit({"ok": True, "operation": "plan", **result})
    return 0


def cmd_apply(args):
    from django.db import connection
    from importer.engine.canonical import visibility_apply as va
    if not args.confirm_write:
        emit({"ok": False, "operation": "apply",
              "error": "Write refused: pass --confirm-write explicitly"})
        return 6
    result = va.plan(connection)
    targets = result["actions"][va.WITHDRAW]
    if args.limit:
        targets = targets[:args.limit]

    def progress(done, processed, total):
        print(f"chunk {done}: {processed}/{total}", file=sys.stderr, flush=True)

    outcome = va.apply_withdrawal(
        connection, targets, confirm=True, max_changes=args.max_changes,
        chunk_size=args.chunk_size, progress=progress)
    emit({"ok": bool(outcome.get("applied")), "operation": "apply",
          "planned": len(targets),
          "protected_checkout": result["protected_checkout_count"], **outcome})
    return 0 if outcome.get("applied") else 5


def build_parser():
    p = argparse.ArgumentParser(description="Visibility withdrawal CLI")
    sub = p.add_subparsers(dest="command", required=True)
    pl = sub.add_parser("plan")
    pl.add_argument("--reactivate", action="store_true")
    pl.add_argument("--limit", type=int, default=None)
    pl.add_argument("--show", type=int, default=0)
    pl.set_defaults(handler=cmd_plan)
    ap = sub.add_parser("apply")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--max-changes", type=int, default=20000)
    ap.add_argument("--chunk-size", type=int, default=500)
    ap.add_argument("--confirm-write", action="store_true")
    ap.set_defaults(handler=cmd_apply)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    bootstrap()
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
