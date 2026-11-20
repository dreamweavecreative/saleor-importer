"""Turn product-page URLs into a validated direct-image index.

Input : media_index/product_pages.csv   (key,page_url)
Output: media_index/sku_images.csv      (key,image_url)  merged, deduped
Dry-run by default; --apply writes the index. No product writes.
"""
import os, sys, csv, json, time, argparse
from pathlib import Path
from datetime import datetime, timezone
ROOT = Path("/opt/saleor-caag/imports")
sys.path.insert(0, "/app"); sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "saleor.settings")
import django; django.setup()
from importer.engine import image_lookup as il

ap = argparse.ArgumentParser()
ap.add_argument("--apply", action="store_true")
ap.add_argument("--limit", type=int, default=20)
ap.add_argument("--sleep", type=float, default=1.0)
a = ap.parse_args()

src = ROOT/"media_index/product_pages.csv"
dst = ROOT/"media_index/sku_images.csv"
if not src.is_file():
    src.write_text("key,page_url\n", encoding="utf-8")
    print("CREATED template:", src); raise SystemExit(0)

existing = {}
if dst.is_file():
    with dst.open(newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            k = (r.get("key") or "").strip().upper()
            u = (r.get("image_url") or "").strip()
            if k and u: existing[k] = u

rows = []
with src.open(newline="", encoding="utf-8-sig") as f:
    for r in csv.DictReader(f):
        k = (r.get("key") or "").strip().upper()
        p = (r.get("page_url") or "").strip()
        if k and p and k not in existing: rows.append((k, p))
if a.limit: rows = rows[:a.limit]
print("Mode:", "APPLY" if a.apply else "DRY RUN", "| to resolve:", len(rows))

ok, fail = {}, []
for k, p in rows:
    try:
        url, meta = il.resolve_page_image(p)
        ok[k] = url
        print("OK:", k, meta["width"], "x", meta["height"], url[:100])
    except Exception as exc:
        fail.append({"key": k, "page": p, "error": str(exc)[:200]})
        print("FAIL:", k, str(exc)[:140])
    time.sleep(a.sleep)

if a.apply and ok:
    existing.update(ok)
    tmp = dst.with_suffix(".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f); w.writerow(["key", "image_url"])
        for k in sorted(existing): w.writerow([k, existing[k]])
    tmp.replace(dst)
    print("INDEX UPDATED:", dst, "entries:", len(existing))

rep = ROOT/("logs/page-resolve-%s.json" % datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S"))
rep.write_text(json.dumps({"resolved": ok, "failed": fail}, indent=2))
print("resolved:", len(ok), "| failed:", len(fail), "| report:", rep)
