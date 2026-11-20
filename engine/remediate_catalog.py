"""Phase 5: idempotent remediation. Dry-run default; --apply commits one transaction.
Preserves price, stock, publication, track_inventory and existing descriptions."""
import os, sys, json, argparse
from pathlib import Path
from datetime import datetime, timezone
from collections import Counter
sys.path.insert(0, "/app"); sys.path.insert(0, "/opt/saleor-caag/imports")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "saleor.settings")
import django; django.setup()
from django.db import transaction
from saleor.product.models import ProductVariant, Collection
from importer.engine.readers import read_supplier_file
from importer.engine.validators import validate_record
from importer.engine.categories import resolve_canonical_category
from importer.engine.catalog_enrichment import enrich_seo, build_metadata
from importer.config import taxonomy

CSV = Path("/opt/saleor-caag/imports/incoming/DickerData.csv")
LOGS = Path("/opt/saleor-caag/imports/logs")

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--apply", action="store_true")
    args = ap.parse_args(); apply = args.apply
    rows = []
    for rn, raw in read_supplier_file(CSV):
        row, errs = validate_record(raw)
        if not errs and row.get("Status") == "A": rows.append(row)
    cols = {c.slug: c for c in Collection.objects.all()}
    stats = Counter(); reasons = Counter(); targets = Counter()
    missing = []; samples = []

    def work():
        for row in rows:
            sku = row["StockCode"]
            v = ProductVariant.objects.filter(sku__iexact=sku).select_related("product").first()
            if not v:
                missing.append(sku); stats["missing_sku"] += 1; continue
            p = v.product; changed = False
            cat, reason = resolve_canonical_category(row)
            reasons[reason] += 1
            if cat: targets[cat.slug] += 1
            else: stats["no_category"] += 1
            if cat and p.category_id != cat.id:
                stats["category_remap"] += 1; changed = True
                if apply: p.category = cat
            want = [cols[s] for s, _ in taxonomy.collections_for(
                row.get("Vendor"), row.get("Type"), row.get("StockDescription")) if s in cols]
            have = set(p.collections.values_list("id", flat=True))
            add = [c for c in want if c.id not in have]
            if add:
                stats["collection_add"] += len(add); changed = True
                if apply: p.collections.add(*add)
            seo = enrich_seo(p, row)
            if seo:
                stats["seo_fill"] += 1; changed = True
                if apply:
                    for k, val in seo.items(): setattr(p, k, val)
            meta = build_metadata(p, row)
            if meta:
                stats["metadata"] += 1; changed = True
                if apply:
                    md = dict(p.metadata or {}); md.update(meta); p.metadata = md
            if changed:
                stats["products_changed"] += 1
                if apply: p.save()
                if len(samples) < 12:
                    samples.append({"sku": sku, "category": cat.slug if cat else None, "reason": reason})
            else:
                stats["unchanged"] += 1

    if apply:
        with transaction.atomic(): work()
    else:
        work()

    report = {"mode": "APPLY" if apply else "DRY_RUN",
              "timestamp": datetime.now(timezone.utc).isoformat(),
              "active_rows": len(rows), "stats": dict(stats),
              "category_targets": dict(targets), "reasons": dict(reasons),
              "missing_skus": missing, "samples": samples,
              "preserved": ["price", "stock", "publication", "track_inventory", "description"]}
    LOGS.mkdir(exist_ok=True)
    out = LOGS / f"remediation-{'apply' if apply else 'dryrun'}-{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"
    out.write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    print("\nREPORT:", out)
    if not apply:
        print("\nDRY RUN — no writes. To commit:")
        print("docker exec saleor-caag-api python /opt/saleor-caag/imports/importer/remediate_catalog.py --apply")

if __name__ == "__main__":
    main()
