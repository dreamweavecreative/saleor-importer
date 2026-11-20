"""Icecat coverage probe. READ-ONLY: no product/media/cache writes.

Samples placeholder products (brand + supplier SKU) and asks Icecat for
each datasheet, reporting hits, image candidates and whether the first
image validates through the existing pipeline.
"""
import os, sys, json, time, argparse
from pathlib import Path
from datetime import datetime, timezone
from collections import defaultdict

ROOT = Path("/opt/saleor-caag/imports")
sys.path.insert(0, "/app"); sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "saleor.settings")
import django; django.setup()

from saleor.product.models import ProductMedia
from importer.engine import icecat_client as ic
from importer.engine import image_lookup as il

ap = argparse.ArgumentParser()
ap.add_argument("--limit", type=int, default=18)
ap.add_argument("--per-brand", type=int, default=3)
ap.add_argument("--sleep", type=float, default=1.0)
a = ap.parse_args()

by_brand = defaultdict(list)
for m in ProductMedia.objects.filter(metadata__placeholder=True).select_related(
        "product__default_variant"):
    p = m.product
    brand = ((p.metadata or {}).get("brand") or "").strip()
    sku = (p.default_variant.sku if p.default_variant else "") or ""
    if brand and sku:
        by_brand[brand].append((sku, p.name))

samples = []
for brand in sorted(by_brand):
    for sku, name in by_brand[brand][:a.per_brand]:
        samples.append((brand, sku, name))
samples = samples[:a.limit]

print("=" * 70)
print("CAAG ICECAT COVERAGE PROBE (read-only)")
print("=" * 70)
print("tokens configured:", ic.configured())
if not ic.configured():
    raise SystemExit("FAIL: ICECAT_API_TOKEN missing in config/icecat.env")
print("sampled:", len(samples))

results = []
for brand, sku, name in samples:
    rec = {"brand": brand, "sku": sku, "name": name[:60]}
    try:
        data = ic.get_datasheet(brand=brand, product_code=sku)
        urls = ic.image_urls(data)
        ident = ic.identities(data)
        rec.update({"status": "hit", "image_candidates": len(urls), **ident})
        if urls:
            try:
                raw, final = il.fetch(urls[0])
                d, ext, w, h, sha = il.validate_normalize(raw)
                rec["first_image"] = {"url": urls[0][:110], "w": w, "h": h, "ext": ext}
            except Exception as exc:
                rec["first_image_error"] = str(exc)[:120]
        print("HIT :", brand, sku, "| icecat_id", ident["icecat_id"],
              "| imgs", len(urls), "|", rec.get("first_image",
              rec.get("first_image_error", "no images in datasheet")))
    except ic.IcecatMiss as exc:
        rec.update({"status": "miss", "code": exc.status_code})
        print("MISS:", brand, sku, "| StatusCode", exc.status_code)
    except ic.IcecatError as exc:
        rec.update({"status": "restricted" if exc.transient else "error",
                    "code": exc.status_code, "error": str(exc)[:160]})
        print(rec["status"].upper() + ":", brand, sku, "| StatusCode",
              exc.status_code, "|", str(exc)[:100])
        results.append(rec)
        if rec["status"] == "error" and rec["code"] in (18, 19):
            raise SystemExit("STOP: token rejected - check api-token/content-token assignment")
        time.sleep(a.sleep)
        continue
    results.append(rec)
    time.sleep(a.sleep)

summary = defaultdict(lambda: defaultdict(int))
for r in results:
    summary[r["brand"]][r["status"]] += 1
print("\nPER-BRAND SUMMARY")
for b in sorted(summary):
    print(" ", b, dict(summary[b]))
hits = sum(1 for r in results if r["status"] == "hit")
print("\nhits: %d / %d sampled" % (hits, len(results)))
rep = ROOT/("logs/icecat-probe-%s.json" % datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S"))
rep.write_text(json.dumps(results, indent=2))
print("report:", rep)
print("PRODUCT/MEDIA/CACHE WRITES: NONE")
