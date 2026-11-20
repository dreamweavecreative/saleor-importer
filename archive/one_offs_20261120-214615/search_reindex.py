#!/usr/bin/env python3
"""Rebuild product search_vector + search_document using Saleor's own helpers.
Importer-only. Dry-run default; --apply writes."""
import os, sys, inspect, argparse, time
sys.path.insert(0, "/app")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "saleor.settings")
import django; django.setup()
from django.conf import settings
from django.db import connection
from saleor.product.models import Product

ap = argparse.ArgumentParser()
ap.add_argument("--apply", action="store_true")
ap.add_argument("--batch", type=int, default=500)
A = ap.parse_args()

def sec(t): print("\n"+"="*80+"\n"+t+"\n"+"="*80, flush=True)

sec("0. PRE-FLIGHT")
rep = getattr(settings, "DATABASE_CONNECTION_REPLICA_NAME", None)
print("replica alias:", rep, "| configured:", rep in settings.DATABASES)
print("DATABASES aliases:", list(settings.DATABASES))
from saleor.product.tasks import update_products_search_vector
try:
    from saleor.core.search_tasks import set_product_search_document_values as docfn
    print("doc fn signature:", inspect.signature(docfn))
except Exception as e:
    docfn = None; print("doc fn import failed:", e)

total = Product.objects.count()
vec0 = Product.objects.exclude(search_vector=None).count()
doc0 = Product.objects.exclude(search_document="").count()
dirty0 = Product.objects.filter(search_index_dirty=True).count()
print(f"products={total} vector={vec0} document={doc0} dirty={dirty0}")
if not A.apply:
    print("\nDRY-RUN. Re-run with --apply to rebuild."); raise SystemExit(0)

ids = list(Product.objects.values_list("id", flat=True).order_by("id"))
sec("1. REBUILD search_vector")
mode = None; done = 0; t0 = time.time()
for i in range(0, len(ids), A.batch):
    batch = ids[i:i+A.batch]
    if mode is None or mode == "task":
        try:
            update_products_search_vector(batch); mode = "task"
        except Exception as e:
            if mode == "task": raise
            print("task path failed (%s) -> falling back to direct helpers" % e)
            mode = "direct"
    if mode == "direct":
        from saleor.core.search_tasks import set_search_vector_values
        from saleor.product.search import prepare_product_search_vector_value
        objs = list(Product.objects.filter(id__in=batch))
        set_search_vector_values(objs, prepare_product_search_vector_value)
    Product.objects.filter(id__in=batch).update(search_index_dirty=False)
    done += len(batch)
    if done % 5000 == 0:
        print(f"  vector {done}/{len(ids)} ({time.time()-t0:.0f}s, path={mode})", flush=True)
print(f"vector pass complete: {done} in {time.time()-t0:.0f}s via {mode}")

sec("2. REBUILD search_document (if needed)")
if Product.objects.exclude(search_document="").count() >= total * 0.99:
    print("already populated — skipping")
elif docfn is None:
    print("helper unavailable — skipped")
else:
    sig = inspect.signature(docfn); n = len([p for p in sig.parameters.values()
        if p.default is inspect.Parameter.empty and p.kind in
        (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)])
    print("required positional args:", n)
    ok = 0
    for i in range(0, len(ids), A.batch):
        batch = ids[i:i+A.batch]
        try:
            docfn(batch) if n == 1 else docfn(batch, len(ids))
            ok += len(batch)
        except Exception as e:
            print("doc call failed:", type(e).__name__, e); break
    print("document pass:", ok)

sec("3. VERIFICATION")
print("products:", Product.objects.count())
print("search_vector populated:", Product.objects.exclude(search_vector=None).count())
print("search_document populated:", Product.objects.exclude(search_document="").count())
print("search_index_dirty:", Product.objects.filter(search_index_dirty=True).count())
with connection.cursor() as c:
    c.execute("""SELECT count(*) FROM product_product
                 WHERE search_vector @@ plainto_tsquery('simple','laptop')""")
    print("FTS probe 'laptop':", c.fetchone()[0])
    c.execute("""SELECT count(*) FROM product_product
                 WHERE search_vector @@ plainto_tsquery('simple','cisco')""")
    print("FTS probe 'cisco':", c.fetchone()[0])
