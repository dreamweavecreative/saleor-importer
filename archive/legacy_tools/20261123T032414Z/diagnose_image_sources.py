#!/usr/bin/env python3
"""READ-ONLY: brand inventory, Google 403 reason, Icecat entitlement matrix."""
import os, sys, collections, time, json
sys.path.insert(0, "/app"); sys.path.insert(0, "/tmp")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "saleor.settings")
import django; django.setup()
import requests
from saleor.product.models import Product, ProductVariant
from imports.importer.engine import image_identity as ident
from imports.importer.engine import image_sources as sources
from imports.importer.engine import image_lookup as lookup
from imports.importer.engine import icecat_client as icecat

def sec(t): print("\n" + "="*78 + "\n" + t + "\n" + "="*78, flush=True)

sec("1. BRAND INVENTORY (actual stored metadata values)")
counts = collections.Counter()
for p in Product.objects.only("id", "metadata").iterator(chunk_size=2000):
    counts[(p.metadata or {}).get("brand", "(none)")] += 1
for b, n in counts.most_common(25):
    print("  %-24s %d" % (b, n))

sec("2. GOOGLE CSE — FULL ERROR BODY")
cfg = lookup.api_config()
print("  key present:", bool(cfg.get("google_key")), "cx present:", bool(cfg.get("google_cx")))
r = requests.get("https://www.googleapis.com/customsearch/v1",
                 params={"key": cfg["google_key"], "cx": cfg["google_cx"],
                         "q": '"Lenovo" "21TD0016AU"', "searchType": "image", "num": 3},
                 timeout=25)
print("  HTTP", r.status_code)
print("  body:", r.text[:1200])

sec("3. ICECAT ENTITLEMENT MATRIX (5 products per brand)")
brands = [b for b, _ in counts.most_common(8) if b != "(none)"]
tally = collections.Counter()
for b in brands:
    vs = list(ProductVariant.objects.filter(product__metadata__brand=b)
              .select_related("product")[:5])
    print("\n  --- %s ---" % b)
    for v in vs:
        idn = ident.product_identity(v.product, v)
        cands, outs = sources.candidates(idn, allow_search=False)
        best = cands[0] if cands else None
        tally[outs[0] if outs else "no_candidate"] += 1
        print("    %-26s %-22s %s" % (
            idn["mpn"][:26], outs[0] if outs else "-",
            ("conf=%.2f deriv=%s" % (best["confidence"], best["mpn_derivation"])) if best else ""))
        time.sleep(0.2)

sec("4. TALLY")
for k, n in tally.most_common():
    print("  %-22s %d" % (k, n))
print("\nREAD-ONLY — nothing attached, nothing written.")
