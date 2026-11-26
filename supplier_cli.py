#!/usr/bin/env python3
"""
Dedicated canonical supplier import CLI.

This command deliberately does not invoke the legacy catalogue importer,
Saleor product writers, inventory workflows, image workflows or media tasks.

Input schema:
    sku, name, manufacturer, brand, gtin, mpn,
    price, currency, stock, moq

Supported feed formats: CSV, JSON, XML.

Supplier profiles are opt-in; DickerData requires --profile dickerdata.
"""

import argparse
import json
import sys
from pathlib import Path

MAX_FEED_BYTES = 100 * 1024 * 1024
EXIT_OK = 0
EXIT_VALIDATION = 2
EXIT_INPUT = 3
EXIT_DUPLICATE = 4
EXIT_OPERATIONAL = 5
EXIT_CONFIRMATION = 6

IMPORTER_ROOT = Path(__file__).resolve().parent
IMPORTS_ROOT = IMPORTER_ROOT.parent


def emit(payload):
    print(json.dumps(payload, indent=2, default=str, sort_keys=True))


def bootstrap():
    # Script execution by absolute path does not automatically add /app,
    # where the Saleor package is installed in this container.
    app_root = Path("/app")
    if app_root.is_dir():
        sys.path.insert(0, str(app_root))

    sys.path.insert(0, str(IMPORTS_ROOT))

    import os
    import django

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "saleor.settings")
    django.setup()


def load_feed(path):
    feed_path = Path(path).expanduser()

    if not feed_path.exists():
        raise FileNotFoundError(f"Feed file does not exist: {feed_path}")

    if not feed_path.is_file():
        raise ValueError("Feed path must identify a regular file")

    size = feed_path.stat().st_size
    if size <= 0:
        raise ValueError("Feed file is empty")

    if size > MAX_FEED_BYTES:
        raise ValueError(
            f"Feed exceeds the {MAX_FEED_BYTES // (1024 * 1024)} MB limit"
        )

    # Read-only input. The CLI never moves, renames, deletes or rewrites it.
    return feed_path.read_bytes()


def inspect_feed(path, feed_format, supplier_id, profile=None):
    from importer.engine.canonical import supplier_import as si

    raw_bytes = load_feed(path)
    records = si.parse_feed(raw_bytes, feed_format)

    adapter_summary = None
    selected_profile = str(profile or "").strip().lower()

    if selected_profile:
        if selected_profile != "dickerdata":
            raise ValueError(f"Unsupported supplier profile: {profile}")

        if str(supplier_id).strip().lower() != "dickerdata":
            raise ValueError(
                "The dickerdata profile requires --supplier-id dickerdata"
            )

        if str(feed_format).strip().upper() != "CSV":
            raise ValueError(
                "The dickerdata profile currently supports CSV only"
            )

        from importer.engine.dickerdata_adapter import adapt_records
        records, adapter_summary = adapt_records(records)

    valid = 0
    invalid = []

    for index, record in enumerate(records, 1):
        try:
            si.normalize_record(record)
            valid += 1
        except (ValueError, TypeError) as exc:
            invalid.append({
                "row_number": index,
                "error": str(exc)[:500],
            })

    fingerprint = si.calculate_fingerprint(supplier_id, records)

    summary = {
        "supplier_id": str(supplier_id).strip().lower(),
        "feed_format": str(feed_format).upper(),
        "total_rows": len(records),
        "valid_rows": valid,
        "invalid_rows": len(invalid),
        "fingerprint": fingerprint,
        "invalid_samples": invalid[:20],
        "input_file": str(Path(path).expanduser()),
        "input_modified": False,
    }

    if adapter_summary is not None:
        summary["adapter"] = adapter_summary

    return {
        "records": records,
        "summary": summary,
    }

def command_validate(args):
    try:
        result = inspect_feed(args.file, args.format, args.supplier_id, profile=getattr(args, 'profile', None))
        emit({
            "ok": True,
            "operation": "validate",
            **result["summary"],
        })
        return EXIT_VALIDATION if result["summary"]["invalid_rows"] else EXIT_OK
    except (OSError, ValueError, TypeError) as exc:
        emit({"ok": False, "operation": "validate", "error": str(exc)})
        return EXIT_INPUT



def command_preview(args):
    """
    Bounded-memory, read-only DickerData variant mapping preview.

    Streams the CSV and queries Saleor/canonical mappings in small batches.
    Never stages rows, reconciles batches, or writes database records.
    """
    import csv
    import json
    from collections import defaultdict
    from django.db import connection, transaction
    from importer.engine.dickerdata_adapter import (
        EXPECTED_COLUMNS,
        adapt_records,
    )
    from importer.engine.canonical import supplier_import as si

    BATCH_SIZE = 500

    def norm(value):
        return str(value or "").strip().upper()

    def metadata_dict(value):
        if isinstance(value, dict):
            return value
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
                return parsed if isinstance(parsed, dict) else {}
            except (TypeError, ValueError):
                return {}
        return {}

    try:
        supplier_id = str(args.supplier_id).strip().lower()
        profile = str(getattr(args, "profile", "") or "").strip().lower()

        if supplier_id != "dickerdata" or profile != "dickerdata":
            raise ValueError(
                "Preview requires --supplier-id dickerdata "
                "and --profile dickerdata"
            )

        if str(args.format).strip().upper() != "CSV":
            raise ValueError("DickerData preview supports CSV only")

        feed_path = Path(args.file).expanduser()
        if not feed_path.is_file():
            raise FileNotFoundError(f"Feed file not found: {feed_path}")
        if feed_path.stat().st_size <= 0:
            raise ValueError("Feed file is empty")
        if feed_path.stat().st_size > MAX_FEED_BYTES:
            raise ValueError("Feed exceeds the configured maximum size")

        counts = {
            "source_rows": 0,
            "eligible_feed_rows": 0,
            "excluded_feed_rows": 0,
            "invalid_feed_rows": 0,
            "saleor_variant_matched": 0,
            "saleor_variant_missing": 0,
            "duplicate_saleor_sku": 0,
            "canonical_bridge_matched": 0,
            "canonical_bridge_missing": 0,
            "duplicate_canonical_variant_bridge": 0,
            "canonical_parent_mismatch": 0,
            "brand_mismatch": 0,
        }

        samples = {
            "missing_saleor_sku": [],
            "duplicate_saleor_sku": [],
            "missing_canonical_bridge": [],
            "duplicate_canonical_bridge": [],
            "canonical_parent_mismatch": [],
            "brand_mismatch": [],
            "invalid_feed_rows": [],
        }

        def sample(key, value):
            if len(samples[key]) < 20:
                samples[key].append(value)

        def process_batch(cursor, batch):
            if not batch:
                return

            sku_keys = [norm(row["sku"]) for row in batch]

            cursor.execute("""
                SELECT
                    v.id,
                    v.sku,
                    v.product_id,
                    p.metadata
                FROM public.product_productvariant AS v
                JOIN public.product_product AS p
                  ON p.id = v.product_id
                WHERE v.sku IS NOT NULL
                  AND UPPER(BTRIM(v.sku)) = ANY(%s)
            """, [sku_keys])

            saleor_by_sku = defaultdict(list)
            variants = cursor.fetchall()

            for variant_id, sku, product_id, metadata in variants:
                saleor_by_sku[norm(sku)].append({
                    "variant_id": str(variant_id),
                    "product_id": str(product_id),
                    "metadata": metadata_dict(metadata),
                })

            variant_ids = list({
                item["variant_id"]
                for found in saleor_by_sku.values()
                for item in found
            })

            canonical_by_variant = defaultdict(list)
            if variant_ids:
                cursor.execute("""
                    SELECT id, saleor_product_id, saleor_variant_id
                    FROM catalogue.canonical_products
                    WHERE saleor_variant_id = ANY(%s)
                """, [variant_ids])

                for canonical_id, saleor_product_id, saleor_variant_id in cursor.fetchall():
                    canonical_by_variant[str(saleor_variant_id)].append({
                        "canonical_id": str(canonical_id),
                        "saleor_product_id": (
                            str(saleor_product_id)
                            if saleor_product_id is not None else None
                        ),
                    })

            for row in batch:
                sku = norm(row["sku"])
                variants_for_sku = saleor_by_sku.get(sku, [])

                if not variants_for_sku:
                    counts["saleor_variant_missing"] += 1
                    sample("missing_saleor_sku", sku)
                    continue

                if len(variants_for_sku) != 1:
                    counts["duplicate_saleor_sku"] += 1
                    sample("duplicate_saleor_sku", {
                        "stock_code": sku,
                        "variant_ids": [
                            item["variant_id"] for item in variants_for_sku
                        ],
                    })
                    continue

                counts["saleor_variant_matched"] += 1
                variant = variants_for_sku[0]

                feed_vendor = norm(row.get("manufacturer"))
                saleor_brand = norm(variant["metadata"].get("brand"))

                if feed_vendor and saleor_brand and feed_vendor != saleor_brand:
                    counts["brand_mismatch"] += 1
                    sample("brand_mismatch", {
                        "stock_code": sku,
                        "feed_vendor": row.get("manufacturer"),
                        "saleor_brand": variant["metadata"].get("brand"),
                    })

                bridges = canonical_by_variant.get(variant["variant_id"], [])

                if not bridges:
                    counts["canonical_bridge_missing"] += 1
                    sample("missing_canonical_bridge", {
                        "stock_code": sku,
                        "saleor_variant_id": variant["variant_id"],
                        "saleor_product_id": variant["product_id"],
                    })
                    continue

                if len(bridges) != 1:
                    counts["duplicate_canonical_variant_bridge"] += 1
                    sample("duplicate_canonical_bridge", {
                        "stock_code": sku,
                        "saleor_variant_id": variant["variant_id"],
                        "canonical_ids": [
                            item["canonical_id"] for item in bridges
                        ],
                    })
                    continue

                bridge = bridges[0]
                if bridge["saleor_product_id"] != variant["product_id"]:
                    counts["canonical_parent_mismatch"] += 1
                    sample("canonical_parent_mismatch", {
                        "stock_code": sku,
                        "saleor_variant_id": variant["variant_id"],
                        "saleor_product_id": variant["product_id"],
                        "canonical_id": bridge["canonical_id"],
                        "canonical_saleor_product_id": bridge["saleor_product_id"],
                    })
                    continue

                counts["canonical_bridge_matched"] += 1

        batch = []

        # Open the feed as a stream. No whole-file byte read, parse_feed(),
        # inspect_feed(), or full-feed adapted-record list is used here.
        with feed_path.open(
            "r",
            encoding="utf-8-sig",
            newline="",
        ) as feed_handle:
            reader = csv.DictReader(feed_handle)

            headers = reader.fieldnames or []
            if len(headers) != len(set(headers)):
                raise ValueError("DickerData CSV contains duplicate headers")

            expected = set(EXPECTED_COLUMNS)
            actual = set(headers)
            missing = sorted(expected - actual)
            unexpected = sorted(actual - expected)

            if missing or unexpected:
                raise ValueError(
                    "DickerData CSV schema mismatch; "
                    f"missing={missing}; unexpected={unexpected}"
                )

            # The transaction is explicitly read-only. SET TRANSACTION is
            # the first SQL statement issued within this atomic block.
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("SET TRANSACTION READ ONLY")

                    for source_row in reader:
                        counts["source_rows"] += 1

                        if None in source_row:
                            counts["invalid_feed_rows"] += 1
                            sample("invalid_feed_rows", {
                                "row_number": counts["source_rows"],
                                "error": "Unexpected extra CSV fields",
                            })
                            continue

                        status = str(source_row.get("Status") or "").strip().upper()
                        item_type = str(source_row.get("Type") or "").strip().casefold()

                        if status != "A" or item_type != "stockeditem":
                            counts["excluded_feed_rows"] += 1
                            continue

                        try:
                            adapted_rows, _ = adapt_records([source_row])
                            adapted = adapted_rows[0]
                            si.normalize_record(adapted)
                        except (ValueError, TypeError) as exc:
                            counts["invalid_feed_rows"] += 1
                            sample("invalid_feed_rows", {
                                "row_number": counts["source_rows"],
                                "stock_code": source_row.get("StockCode"),
                                "error": str(exc)[:300],
                            })
                            continue

                        counts["eligible_feed_rows"] += 1
                        batch.append(adapted)

                        if len(batch) >= BATCH_SIZE:
                            process_batch(cursor, batch)
                            batch.clear()

                    if batch:
                        process_batch(cursor, batch)
                        batch.clear()

        mapping_ready = (
            counts["invalid_feed_rows"] == 0
            and counts["saleor_variant_missing"] == 0
            and counts["duplicate_saleor_sku"] == 0
            and counts["canonical_bridge_missing"] == 0
            and counts["duplicate_canonical_variant_bridge"] == 0
            and counts["canonical_parent_mismatch"] == 0
            and counts["brand_mismatch"] == 0
        )

        emit({
            "ok": True,
            "operation": "preview",
            "read_only": True,
            "database_transaction_read_only": True,
            "memory_strategy": {
                "feed_processing": "streaming CSV",
                "mapping_batch_size": BATCH_SIZE,
                "full_feed_loaded_into_memory": False,
                "full_saleor_catalogue_loaded_into_memory": False,
            },
            "feed_file": str(feed_path),
            "supplier_id": supplier_id,
            "profile": profile,
            "mapping_ready": mapping_ready,
            "mapping_policy": (
                "Exact DickerData StockCode -> Saleor ProductVariant.sku "
                "-> catalogue.canonical_products.saleor_variant_id. "
                "No MPN fallback."
            ),
            "counts": counts,
            "samples": samples,
            "writes_performed": False,
            "batches_staged": False,
            "reconciliation_run": False,
        })
        return EXIT_OK

    except Exception as exc:
        emit({
            "ok": False,
            "operation": "preview",
            "read_only": True,
            "error_type": type(exc).__name__,
            "error": str(exc)[:1500],
            "writes_performed": False,
            "batches_staged": False,
            "reconciliation_run": False,
        })
        return EXIT_OPERATIONAL



def command_stage(args):
    if not args.confirm_write:
        emit({
            "ok": False,
            "operation": "stage",
            "error": "Database write refused: pass --confirm-write explicitly",
        })
        return EXIT_CONFIRMATION

    try:
        selected_profile = str(
            getattr(args, "profile", None) or ""
        ).strip().lower()

        if selected_profile == "dickerdata":
            from importer.engine.canonical.supplier_import import (
                stage_dickerdata_feed,
            )

            result = stage_dickerdata_feed(
                args.file,
                supplier_id=args.supplier_id,
                feed_format=args.format,
            )
            from django.db import connection as _conn
            from importer.engine.canonical.offer_writer import prepare_batch
            prepare_batch(_conn, result["batch_id"], feed_kind="FULL")
            emit({
                "ok": True,
                "operation": "stage",
                **result,
            })
            return EXIT_OK

        result = inspect_feed(
            args.file,
            args.format,
            args.supplier_id,
            profile=getattr(args, "profile", None),
        )
        summary = result["summary"]

        if summary["invalid_rows"] and not args.allow_invalid:
            emit({
                "ok": False,
                "operation": "stage",
                "error": (
                    "Feed contains invalid rows. No batch was created. "
                    "Review validate output; use --allow-invalid only "
                    "if intentionally staging invalid rows for diagnosis."
                ),
                **summary,
            })
            return EXIT_VALIDATION

        from importer.engine.canonical.supplier_import import (
            create_import_batch,
        )

        batch_id = create_import_batch(
            args.supplier_id,
            args.format,
            result["records"],
        )

        emit({
            "ok": True,
            "operation": "stage",
            "batch_id": str(batch_id),
            "status": "PROCESSING",
            **summary,
            "reconciliation_started": False,
        })
        return EXIT_OK

    except Exception as exc:
        from importer.engine.canonical.supplier_import import DuplicateFeedError

        if isinstance(exc, DuplicateFeedError):
            emit({
                "ok": False,
                "operation": "stage",
                "error_type": "DuplicateFeedError",
                "error": str(exc),
            })
            return EXIT_DUPLICATE

        emit({
            "ok": False,
            "operation": "stage",
            "error_type": type(exc).__name__,
            "error": str(exc)[:1500],
        })
        return EXIT_OPERATIONAL



def command_reconcile(args):
    if not args.confirm_write:
        emit({
            "ok": False,
            "operation": "reconcile",
            "error": "Database write refused: pass --confirm-write explicitly",
        })
        return EXIT_CONFIRMATION

    try:
        from importer.engine.canonical.supplier_import import (
            reconcile_import_batch,
        )

        counts = reconcile_import_batch(args.batch_id)

        emit({
            "ok": True,
            "operation": "reconcile",
            "batch_id": args.batch_id,
            "counts": counts,
        })
        return EXIT_OK

    except Exception as exc:
        emit({
            "ok": False,
            "operation": "reconcile",
            "error_type": type(exc).__name__,
            "error": str(exc)[:1000],
        })
        return EXIT_OPERATIONAL


def command_status(args):
    try:
        bootstrap()
        from django.db import connection

        with connection.cursor() as cursor:
            cursor.execute("""
                SELECT
                    id::text,
                    supplier_id,
                    feed_format,
                    status,
                    total_rows,
                    matched_rows,
                    unmatched_rows,
                    conflict_rows,
                    invalid_rows,
                    created_at,
                    completed_at,
                    feed_fingerprint
                FROM catalogue.supplier_import_batches
                WHERE id = %s::uuid
            """, [args.batch_id])

            row = cursor.fetchone()

            if row is None:
                emit({
                    "ok": False,
                    "operation": "status",
                    "error": "Batch not found",
                    "batch_id": args.batch_id,
                })
                return EXIT_INPUT

            columns = [item[0] for item in cursor.description]
            batch = dict(zip(columns, row))

            cursor.execute("""
                SELECT status, COUNT(*)
                FROM catalogue.supplier_import_rows
                WHERE batch_id = %s::uuid
                GROUP BY status
                ORDER BY status
            """, [args.batch_id])

            row_counts = {
                status: count for status, count in cursor.fetchall()
            }

            cursor.execute("""
                SELECT row_number, status, error_message
                FROM catalogue.supplier_import_rows
                WHERE batch_id = %s::uuid
                  AND error_message IS NOT NULL
                ORDER BY row_number
                LIMIT 20
            """, [args.batch_id])

            errors = [
                {
                    "row_number": item[0],
                    "status": item[1],
                    "error": item[2],
                }
                for item in cursor.fetchall()
            ]

        emit({
            "ok": True,
            "operation": "status",
            "batch": batch,
            "row_counts": row_counts,
            "error_samples": errors,
        })
        return EXIT_OK

    except Exception as exc:
        emit({
            "ok": False,
            "operation": "status",
            "error_type": type(exc).__name__,
            "error": str(exc)[:1000],
        })
        return EXIT_OPERATIONAL


def build_parser():
    parser = argparse.ArgumentParser(
        description="Dedicated canonical supplier import CLI"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser(
        "validate",
        help="Parse, normalize and fingerprint a feed without database writes",
    )
    validate.add_argument("--supplier-id", required=True)
    validate.add_argument("--format", required=True, choices=("CSV", "JSON", "XML"))
    validate.add_argument("--file", required=True)
    validate.add_argument(
        "--profile", choices=("dickerdata",), default=None,
        help="Explicit supplier adapter profile (default: canonical input)",
    )
    validate.set_defaults(handler=command_validate)

    preview = subparsers.add_parser(
        "preview",
        help="Read-only supplier-to-Saleor variant mapping preview",
    )
    preview.add_argument("--supplier-id", required=True)
    preview.add_argument("--format", required=True, choices=("CSV", "JSON", "XML"))
    preview.add_argument("--file", required=True)
    preview.add_argument(
        "--profile", choices=("dickerdata",), default=None,
        help="Explicit supplier adapter profile",
    )
    preview.set_defaults(handler=command_preview)

    stage = subparsers.add_parser(
        "stage",
        help="Create a supplier batch; does not reconcile it",
    )
    stage.add_argument("--supplier-id", required=True)
    stage.add_argument("--format", required=True, choices=("CSV", "JSON", "XML"))
    stage.add_argument("--file", required=True)
    stage.add_argument(
        "--profile", choices=("dickerdata",), default=None,
        help="Explicit supplier adapter profile (default: canonical input)",
    )
    stage.add_argument("--confirm-write", action="store_true")
    stage.add_argument("--allow-invalid", action="store_true")
    stage.set_defaults(handler=command_stage)

    reconcile = subparsers.add_parser(
        "reconcile",
        help="Reconcile one explicitly identified supplier batch",
    )
    reconcile.add_argument("--batch-id", required=True)
    reconcile.add_argument("--confirm-write", action="store_true")
    reconcile.set_defaults(handler=command_reconcile)

    status = subparsers.add_parser(
        "status",
        help="Read-only supplier batch and row status",
    )
    status.add_argument("--batch-id", required=True)
    status.set_defaults(handler=command_status)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    # Enforce write confirmation before any Django/database initialization.
    if (
        args.command in ("stage", "reconcile")
        and not args.confirm_write
    ):
        return args.handler(args)

    try:
        if args.command in ("validate", "preview", "stage", "reconcile"):
            bootstrap()
        return args.handler(args)
    except Exception as exc:
        emit({
            "ok": False,
            "operation": getattr(args, "command", "unknown"),
            "error_type": type(exc).__name__,
            "error": str(exc)[:1000],
        })
        return EXIT_OPERATIONAL


if __name__ == "__main__":
    raise SystemExit(main())
