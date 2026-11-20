#!/usr/bin/env python3
"""Populate product_product.search_document. Discovers Saleor's real API at runtime;
no guessed imports. Importer-only, reads backend source but never modifies it.
Dry-run default; --apply writes."""
import os, sys, inspect, argparse, time
sys.path.insert(0, "/app")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "saleor.settings")
import django; django.setup()

from django.db import connection
from django.db.models.query import prefetch_related_objects
from saleor.product.models import Product, ProductVariant
import saleor.core.search_tasks as st
import saleor.product.search as ps

ap = argparse.ArgumentParser()
ap.add_argument("--apply", action="store_true")
ap.add_argument("--batch", type=int, default=200)
ap.add_argument("--show-source", action="store_true")
A = ap.parse_args()

def sec(t): print("\n"+"="*84+"\n"+t+"\n"+"="*84, flush=True)

sec("1. REAL API DISCOVERY (nothing guessed)")
for mod, label in ((st, "saleor.core.search_tasks"), (ps, "saleor.product.search")):
    print("\n%s callables:" % label)
    for n, f in inspect.getmembers(mod, inspect.isfunction):
        if n.startswith("_"): continue
        try: print("  %-46s %s" % (n, inspect.signature(f)))
        except Exception: print("  %s (signature unavailable)" % n)

DOCFNS = [n for n, f in inspect.getmembers(st, inspect.isfunction)
          if "search_document" in n.lower()]
PREP = None; PREP_NAME = None
for n, f in inspect.getmembers(ps, inspect.isfunction):
    if "document" in n.lower() and "prepare" in n.lower():
        PREP, PREP_NAME = f, n; break
print("\ndiscovered document tasks :", DOCFNS)
print("discovered prepare fn     :", PREP_NAME)

if A.show_source:
    for n in DOCFNS:
        if "product" in n.lower():
            sec("SOURCE: %s.%s (read-only)" % (st.__name__, n))
            print(inspect.getsource(getattr(st, n))[:2600])
    if PREP:
        sec("SOURCE: %s.%s (read-only)" % (ps.__name__, PREP_NAME))
        print(inspect.getsource(PREP)[:2600])

PREFETCH = None
for n in dir(ps):
    if "PREFETCH" in n.upper():
        v = getattr(ps, n)
        if isinstance(v, (list, tuple)): PREFETCH = v; print("prefetch constant:", n); break

KW = {}
if PREP:
    params = inspect.signature(PREP).parameters
    if "already_prefetched" in params and PREFETCH: KW["already_prefetched"] = True
    print("kwargs for prepare:", KW)

def fallback_document(p):
    """Used only if Saleor exposes no prepare helper. Fields confirmed from schema."""
    parts = [p.name or "", p.description_plaintext or ""]
    if p.category_id and p.category: parts.append(p.category.name)
    parts += [c.name for c in p.collections.all()]
    for v in p.variants.all():
        parts += [v.name or "", v.sku or ""]
    for av in p.attributevalues.all() if hasattr(p, "attributevalues") else []:
        parts.append(av.name or "")
    return " ".join(x for x in parts if x).strip()

sec("2. STATE")
total = Product.objects.count()
have = Product.objects.exclude(search_document="").count()
print("products=%d  search_document_populated=%d  missing=%d" % (total, have, total-have))
print("search_vector populated:", Product.objects.exclude(search_vector=None).count())
print("search_index_dirty     :", Product.objects.filter(search_index_dirty=True).count())
print("mode:", "helper=%s" % PREP_NAME if PREP else "ORM fallback")
if not A.apply:
    print("\nDRY-RUN. Re-run with --apply (add --show-source to dump the real source).")
    raise SystemExit(0)

sec("3. POPULATE search_document")
ids = list(Product.objects.values_list("id", flat=True).order_by("id"))
done = 0; t0 = time.time()
for i in range(0, len(ids), A.batch):
    batch = list(Product.objects.filter(id__in=ids[i:i+A.batch])
                 .prefetch_related("variants", "collections"))
    if PREFETCH and KW:
        prefetch_related_objects(batch, *PREFETCH)
    for p in batch:
        if PREP:
            p.search_document = PREP(p, **KW)
        else:
            p.search_document = fallback_document(p)
    Product.objects.bulk_update(batch, ["search_document"], batch_size=A.batch)
    done += len(batch)
    if done % 5000 == 0:
        print("  %d/%d (%.0fs)" % (done, len(ids), time.time()-t0), flush=True)
print("complete: %d in %.0fs" % (done, time.time()-t0))

sec("4. VERIFICATION")
print("products                  :", Product.objects.count())
print("search_document populated :", Product.objects.exclude(search_document="").count())
print("search_vector populated   :", Product.objects.exclude(search_vector=None).count())
print("search_index_dirty        :", Product.objects.filter(search_index_dirty=True).count())
with connection.cursor() as c:
    c.execute("SELECT count(*) FROM product_product WHERE similarity(search_document,%s) > 0.3",
              ["thinkpad"])
    print("trigram 'thinkpad' matches:", c.fetchone()[0])
    c.execute("SELECT count(*) FROM product_product WHERE search_vector @@ plainto_tsquery('simple','cisco')")
    print("FTS 'cisco' matches       :", c.fetchone()[0])
    c.execute("SELECT id, left(search_document,120) FROM product_product "
              "WHERE search_document <> '' ORDER BY id LIMIT 3")
    for r in c.fetchall(): print("  sample:", r)
