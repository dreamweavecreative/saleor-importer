import csv
import json
import logging
from collections import defaultdict
from pathlib import Path

from imports.importer.engine.normalizers import normalize_row
from imports.importer.engine.canonical.identity import resolve_identity
from imports.importer.engine.canonical.reconciliation import reconcile
from imports.importer.engine.saleor_writer import write_batch

logger = logging.getLogger(__name__)
BATCH = 500


def run_import(csv_path, default_channel_id, default_warehouse_id, default_product_type_id):
    path = Path(csv_path)
    if not path.exists():
        raise FileNotFoundError(csv_path)

    stats = defaultdict(int)
    batch, failed = [], []

    def flush():
        if not batch:
            return
        try:
            for k, v in (write_batch(
                batch,
                default_product_type_id,
                default_channel_id,
                default_warehouse_id,
            ) or {}).items():
                stats[k] += v
        except Exception as e:
            logger.exception("batch failed (%d rows): %s", len(batch), e)
            stats["batch_failed_rows"] += len(batch)
            failed.extend({"error": str(e), "sku": p.get("sku")} for p in batch)
        batch.clear()

    with open(path, encoding="utf-8-sig", newline="") as f:
        for i, row in enumerate(csv.DictReader(f), start=2):
            stats["total"] += 1
            n = normalize_row(row)
            if not n or n.raw_errors:
                stats["failed"] += 1
                failed.append({
                    "row": i,
                    "error": (n.raw_errors if n else "normalize failed"),
                    "raw": row,
                })
                continue

            if str(n.import_flag).strip().lower() != "true":
                stats["skipped_inactive"] += 1
                continue

            match, variant, product = resolve_identity(n)
            stats["matched_" + match.lower()] += 1
            batch.append(reconcile(
                n, match, variant, product,
                default_channel_id, default_warehouse_id,
            ))

            if len(batch) >= BATCH:
                flush()

    flush()

    if failed:
        dlq = path.parent / "failed" / f"dlq_{path.stem}.jsonl"
        dlq.parent.mkdir(parents=True, exist_ok=True)
        with open(dlq, "w", encoding="utf-8") as fh:
            for rec in failed:
                fh.write(json.dumps(rec, default=str) + "\n")
        logger.warning("DLQ: %d records -> %s", len(failed), dlq)

    try:
        from imports.importer.engine.post_import import run as post_import_run, caag_run_post_import_fixes
        post_stats = post_import_run(
            channel_id=default_channel_id,
            warehouse_id=default_warehouse_id,
        )
        stats.update(post_stats)
        
        # CAAG Post-Import Hook
        caag_run_post_import_fixes()
    except Exception as e:
        logger.exception("post-import failed: %s", e)
        stats["post_import_failed"] = 1

    logger.info("Import complete. Stats: %s", dict(stats))
    return dict(stats)
