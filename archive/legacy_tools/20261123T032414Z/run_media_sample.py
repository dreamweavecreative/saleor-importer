#!/usr/bin/env python3
"""Stratified media sample. Dry-run exercises the REAL waterfall (read-only)."""
import os, sys, random, argparse, collections
sys.path.insert(0, "/app"); sys.path.insert(0, "/tmp")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "saleor.settings")
import django; django.setup()

from saleor.product.models import Product, ProductType
from imports.importer.engine import image_identity as ident
from imports.importer.engine import image_sources as sources

ap = argparse.ArgumentParser()
ap.add_argument("--limit", type=int, default=30)
ap.add_argument("--seed", type=int, default=42)
ap.add_argument("--type", default="all")
ap.add_argument("--apply", action="store_true")
ap.add_argument("--no-search", action="store_true")
ap.add_argument("--show", type=int, default=6, help="detail lines per segment")
A = ap.parse_args()

KEYS = sources.provider_keys()
print("uid=%s apply=%s limit=%d  serp=%s exa=%s  threshold=%.2f"
      % (os.geteuid(), A.apply, A.limit, bool(KEYS["serp"]), bool(KEYS["exa"]),
         ident.ACCEPT_THRESHOLD))

types = {t["id"]: t for t in ProductType.objects.values("id", "name", "slug")}
buckets = collections.defaultdict(lambda: collections.defaultdict(list))
for pid, md, ptid in Product.objects.values_list(
        "id", "metadata", "product_type_id").iterator(chunk_size=2000):
    kind = "digital" if types.get(ptid, {}).get("slug") == "digital-services" else "physical"
    buckets[kind][(md or {}).get("brand") or "(none)"].append(pid)

grand = sum(len(v) for b in buckets.values() for v in b.values())
rng = random.Random(A.seed)
kinds = [A.type] if A.type in ("physical", "digital") else ["physical", "digital"]
tier_tally = collections.Counter()
summary = {}

for kind in kinds:
    total_kind = sum(len(v) for v in buckets[kind].values())
    if not total_kind:
        continue
    quota = A.limit if len(kinds) == 1 else max(1, round(A.limit * total_kind / grand))
    sample = []
    for brand, ids in sorted(buckets[kind].items(), key=lambda x: -len(x[1])):
        n = min(len(ids), max(1, round(quota * len(ids) / total_kind)))
        sample.extend((brand, pid) for pid in rng.sample(ids, n))
    sample = sample[:quota]

    print("\n" + "="*78)
    print("%s — sample %d of %d" % (kind.upper(), len(sample), total_kind))
    print("="*78)
    per = collections.defaultdict(collections.Counter)
    shown = 0
    for brand, pid in sample:
        p = Product.objects.get(id=pid)
        v = p.variants.first()
        if v is None:
            per[brand]["no_variant"] += 1; continue
        idn = ident.product_identity(p, v)
        if A.apply:
            from imports.importer.engine.media_tasks import process_product_media_sync
            res = process_product_media_sync(pid, allow_search=not A.no_search)
            key = res.split(":")[0]
            if res.startswith("resolved:"):
                tier_tally[res.split(":", 1)[1]] += 1
            best = None
        else:
            if kind == "digital":
                key, best, outs = "branded_card", None, ["non_physical"]
            else:
                cands, outs = sources.candidates(idn, allow_search=not A.no_search,
                                                 keys=KEYS)
                best = cands[0] if cands else None
                key = "resolved" if best else next(
                    (o for o in outs if o not in ("no_candidate", "search_disabled")),
                    "no_candidate")
                if best:
                    tier_tally[best["tier"]] += 1
        per[brand][key] += 1
        if best and shown < A.show:
            shown += 1
            print("  [%s] %s" % (pid, idn["name"][:64]))
            print("      mpn=%s  tier=%s conf=%.2f  %s"
                  % (idn["mpn"], best["tier"], best["confidence"], best["reason"]))
            print("      img=%s" % best["source_url"][:110])
            print("      page=%s" % (best.get("source_page") or "-")[:110])
            print("      token_in_fetch_url=%s"
                  % ("content_token" in best["fetch_url"]))

    print("\n  %-14s %5s %9s %11s %6s %7s" % ("BRAND","n","resolved","restricted","miss","other"))
    for brand, c in sorted(per.items(), key=lambda x: -sum(x[1].values())):
        n = sum(c.values())
        other = n - c["resolved"] - c["icecat_restricted"] - c["icecat_miss"]
        print("  %-14s %5d %9d %11d %6d %7d"
              % (brand[:14], n, c["resolved"], c["icecat_restricted"],
                 c["icecat_miss"], other))
    tot = sum(sum(c.values()) for c in per.values())
    res = sum(c["resolved"] for c in per.values())
    summary[kind] = (res, tot)

print("\n" + "="*78)
print("SUMMARY")
for kind, (res, tot) in summary.items():
    print("  %-9s resolved %d/%d = %.1f%%" % (kind, res, tot, 100.0*res/max(1,tot)))
print("  winning tiers:", dict(tier_tally))
print("MODE:", "APPLIED" if A.apply else "READ-ONLY (no writes)")
