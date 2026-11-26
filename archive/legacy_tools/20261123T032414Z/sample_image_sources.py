#!/usr/bin/env python3
"""READ-ONLY sourcing sample. No Saleor writes. Reports candidate accuracy."""
import os, sys, argparse, collections
sys.path.insert(0, "/app"); sys.path.insert(0, "/tmp")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "saleor.settings")
import django; django.setup()

from saleor.product.models import Product, ProductVariant
from imports.importer.engine import image_identity as ident
from imports.importer.engine import image_sources as sources

ap = argparse.ArgumentParser()
ap.add_argument("--limit", type=int, default=20)
ap.add_argument("--brands", default="HP,Lenovo,Dell,Cisco,Logitech,Jabra")
ap.add_argument("--no-search", action="store_true")
A = ap.parse_args()

print("uid:", os.geteuid(), "| ACCEPT_THRESHOLD:", ident.ACCEPT_THRESHOLD)
wanted = [b.strip() for b in A.brands.split(",") if b.strip()]
qs = (ProductVariant.objects.filter(product__metadata__brand__in=wanted)
      .select_related("product").order_by("product_id"))

picked, per_brand = [], collections.Counter()
for v in qs.iterator(chunk_size=500):
    b = (v.product.metadata or {}).get("brand")
    if per_brand[b] >= max(1, A.limit // max(1, len(wanted))):
        continue
    per_brand[b] += 1
    picked.append(v)
    if len(picked) >= A.limit:
        break

tiers = collections.Counter(); accepted = 0
for v in picked:
    idn = ident.product_identity(v.product, v)
    cands = sources.candidates(idn, allow_search=not A.no_search)
    best = cands[0] if cands else None
    if best:
        accepted += 1; tiers[best["tier"]] += 1
    print("\n[%s] %s" % (v.product_id, idn["name"][:70]))
    print("   brand=%s mpn=%s (%s)" % (idn["brand"], idn["mpn"], idn["mpn_source"]))
    print("   tokens=%s" % idn["model_tokens"])
    if not best:
        print("   NO CANDIDATE")
        continue
    print("   BEST tier=%s conf=%.2f reason=%s" % (best["tier"], best["confidence"], best["reason"]))
    print("   stored_url=%s" % best["source_url"])
    print("   fetch_url_has_token=%s" % ("content_token" in best["fetch_url"]))
    print("   title=%s" % (best.get("title","")[:90]))

print("\n" + "="*70)
print("sampled=%d  with_candidate=%d  tiers=%s" % (len(picked), accepted, dict(tiers)))
print("READ-ONLY — nothing attached, nothing written.")
