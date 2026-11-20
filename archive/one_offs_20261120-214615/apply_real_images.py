"""Replace placeholder product media with verified real images.

Dry-run by default. Only rows with metadata.placeholder=True are touched.
Placeholder is retained on any failure. Idempotent.
"""
import os, sys, json, argparse, hashlib
from pathlib import Path
from datetime import datetime, timezone

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "saleor.settings")
sys.path.insert(0, "/app")
sys.path.insert(0, "/opt/saleor-caag/imports")
import django
django.setup()

from django.db import transaction
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.utils.text import slugify
from saleor.product.models import ProductMedia
from importer.engine import image_lookup as il

ROOT = Path("/opt/saleor-caag/imports")
LOGS = ROOT / "logs"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=20, help="0 = all eligible")
    ap.add_argument("--max-searches", type=int, default=25)
    ap.add_argument("--sleep", type=float, default=1.0)
    ap.add_argument("--sku", action="append", default=[])
    a = ap.parse_args()

    cfg = il.api_config()
    providers = il.providers_available(cfg)
    index = il.load_index()
    cache = il.load_cache()
    budget = il.SearchBudget(a.max_searches)

    print("=" * 68)
    print("CAAG REAL IMAGE PIPELINE (7b)")
    print("=" * 68)
    print("Mode:", "APPLY" if a.apply else "DRY RUN")
    print("Index entries:", len(index))
    print("Cache entries:", len(cache["entries"]))
    print("Search providers:", providers or "NONE (index/cache only)")
    print("Search budget:", a.max_searches)

    by_product = {}
    for m in ProductMedia.objects.select_related("product").all():
        by_product.setdefault(m.product_id, []).append(m)

    targets = []
    for pid, medias in sorted(by_product.items()):
        real = [m for m in medias if not (m.metadata or {}).get("placeholder")]
        ph = [m for m in medias if (m.metadata or {}).get("placeholder")]
        if real or not ph:
            continue
        targets.append(ph[0])

    print("Eligible placeholder products:", len(targets))

    if a.sku:
        wanted = {s.strip().upper() for s in a.sku}
        targets = [m for m in targets
                   if (m.product.default_variant
                       and (m.product.default_variant.sku or "").upper() in wanted)]
        print("Filtered to SKUs:", len(targets))

    if a.limit and a.limit > 0:
        targets = targets[:a.limit]
    print("Processing this run:", len(targets))

    stats = {"updated": 0, "kept_placeholder": 0, "needs_search": 0,
             "not_found": 0, "failed": 0, "index_hits": 0, "cache_hits": 0}
    failures = []

    for m in targets:
        product = m.product
        variant = product.default_variant or product.variants.first()
        sku = (variant.sku if variant else "") or ""
        key = sku.upper()
        md = product.metadata or {}
        brand = md.get("brand") or ""
        pm = (variant.private_metadata or {}) if variant else {}
        sup = pm.get("supplier_data") or {}
        mpn = sku
        for rec in sup.values():
            mpn = rec.get("supplier_sku") or rec.get("stock_code") or mpn
            break
        query = (" ".join(x for x in [brand, mpn] if x)).strip() or sku

        url, source = il.candidates_for(
            key, mpn, query, cfg, index, cache, budget,
            allow_search=a.apply, sleep_s=a.sleep)

        if not url:
            stats["kept_placeholder"] += 1
            if source == "needs_search":
                stats["needs_search"] += 1
            elif source in ("not_found", "cache_negative"):
                stats["not_found"] += 1
            failures.append({"sku": sku, "reason": source})
            continue

        if source == "index":
            stats["index_hits"] += 1
        elif source == "cache":
            stats["cache_hits"] += 1

        if not a.apply:
            print("PLAN:", sku, "->", source, url[:110])
            continue

        try:
            raw, final_url = il.fetch(url)
            data, ext, w, h, sha = il.validate_normalize(raw)
            name = "products/caag/%s-%s.%s" % (
                (slugify(sku) or "product")[:80], sha[:12], ext)
            if not default_storage.exists(name):
                name = default_storage.save(name, ContentFile(data))
            with transaction.atomic():
                m.image = name
                m.external_url = final_url[:2048]
                m.alt = (product.name or "")[:250]
                meta = dict(m.metadata or {})
                meta.update({"placeholder": False, "source": source,
                             "source_url": final_url, "retrieved_at": il.utc_now(),
                             "sha256": sha, "width": w, "height": h})
                m.metadata = meta
                m.save(update_fields=["image", "external_url", "alt", "metadata"])
            cache["entries"][key] = {"status": "found", "url": final_url,
                                     "source": source, "checked_at": il.utc_now(),
                                     "file": name, "sha256": sha}
            stats["updated"] += 1
            print("OK:", sku, "%dx%d" % (w, h), source)
        except Exception as exc:
            stats["kept_placeholder"] += 1
            stats["failed"] += 1
            failures.append({"sku": sku, "reason": str(exc)[:200]})
            cache["entries"][key] = {"status": "error", "url": url,
                                     "checked_at": il.utc_now(),
                                     "error": str(exc)[:300]}
            print("FAIL:", sku, str(exc)[:160])

    if a.apply:
        il.save_cache(cache)

    remaining = sum(
        1 for m in ProductMedia.objects.all()
        if (m.metadata or {}).get("placeholder") is True)

    report = {"timestamp": il.utc_now(), "mode": "apply" if a.apply else "dry_run",
              "index_entries": len(index), "providers": providers,
              "searches_used": budget.used, "stats": stats,
              "remaining_placeholders": remaining, "failures": failures[:200]}
    LOGS.mkdir(parents=True, exist_ok=True)
    out = LOGS / ("images-%s-%s.json" % (
        "apply" if a.apply else "dryrun",
        datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")))
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print("\n" + "=" * 68)
    print(json.dumps(stats, indent=2))
    print("Searches used:", budget.used)
    print("Remaining placeholders:", remaining)
    print("Report:", out)
    if not a.apply:
        print("\nDRY RUN - no writes. Apply a small batch first:")
        print("  apply_real_images.py --apply --limit 10")
    print("=" * 68)


if __name__ == "__main__":
    main()
