import os, sys, csv, inspect, collections
sys.path.insert(0, "/app")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "saleor.settings")
import django; django.setup()

from decimal import Decimal
from django.db import connection
from django.core.management import load_command_class
from saleor.product.models import Product, ProductVariant, ProductType
from saleor.warehouse.models import Stock

SENT = 999999999
CSV_CANDIDATES = [os.environ.get("CSV_PATH",""),
                  "/tmp/imports/incoming/DickerData.csv",
                  "/tmp/DickerData.csv"]
CSV_PATH = next((p for p in CSV_CANDIDATES if p and os.path.exists(p)), None)

def sec(t): print("\n"+"="*88+"\n"+t+"\n"+"="*88)
def run(t, fn):
    sec(t)
    try: fn()
    except Exception as e: print("  ERROR %s: %s" % (type(e).__name__, e))

def weight_truth():
    f = Product._meta.get_field("weight")
    print("field class:", type(f).__module__ + "." + type(f).__name__)
    with connection.cursor() as c:
        c.execute("SELECT id, weight FROM product_product WHERE weight IS NOT NULL ORDER BY id LIMIT 5")
        print("RAW DB product.weight:", c.fetchall())
        c.execute("SELECT id, minimum_order_weight, maximum_order_weight FROM shipping_shippingmethod LIMIT 5")
        print("RAW DB shipping weights:", c.fetchall())
    p = Product.objects.exclude(weight__isnull=True).first()
    print("descriptor repr:", repr(p.weight), "| str:", str(p.weight))
    try:
        src = inspect.getsource(type(f))
        print("--- WeightField source (read-only) ---")
        print(src[:3000])
    except Exception as e:
        print("source unavailable:", e)

def classify_sentinel():
    if not CSV_PATH:
        print("CSV not found in container; set CSV_PATH"); return
    print("CSV:", CSV_PATH)
    sent = set(Stock.objects.filter(quantity=SENT).values_list("product_variant__sku", flat=True))
    fin  = set(Stock.objects.filter(quantity__lt=SENT).values_list("product_variant__sku", flat=True))
    print("sentinel SKUs:", len(sent), "| finite SKUs:", len(fin))
    gs = {"sentinel": collections.Counter(), "finite": collections.Counter()}
    ts = {"sentinel": collections.Counter(), "finite": collections.Counter()}
    sec2 = {"sentinel": collections.Counter(), "finite": collections.Counter()}
    seen = {"sentinel": set(), "finite": set()}
    with open(CSV_PATH, newline="", encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            sku = (r.get("StockCode") or "").strip()
            grp = "sentinel" if sku in sent else ("finite" if sku in fin else None)
            if not grp: continue
            seen[grp].add(sku)
            gs[grp][(r.get("PrimaryCategory") or "").strip()] += 1
            sec2[grp][(r.get("SecondaryCategory") or "").strip()] += 1
            ts[grp][(r.get("Type") or "").strip()] += 1
    for grp in ("sentinel","finite"):
        print("\n--- %s --- matched CSV rows for %d SKUs (missing %d)"
              % (grp.upper(), len(seen[grp]),
                 (len(sent) if grp=="sentinel" else len(fin)) - len(seen[grp])))
        print(" Type:", dict(ts[grp]))
        print(" Top PrimaryCategory:")
        for k,v in gs[grp].most_common(20): print("   %-34s %s" % (k or "(blank)", v))
        print(" Top SecondaryCategory:")
        for k,v in sec2[grp].most_common(12): print("   %-34s %s" % (k or "(blank)", v))

def orphan_mechanism():
    orphans = list(Product.objects.filter(variants__isnull=True).order_by("id")
                   .values("id","name")[:60])
    print("orphan count:", len(orphans))
    if not CSV_PATH:
        print("CSV missing; cannot cross-reference"); return
    byname = {}
    with open(CSV_PATH, newline="", encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            byname.setdefault((r.get("StockDescription") or "").strip().upper(), []).append(
                (r.get("StockCode") or "").strip())
    for o in orphans:
        codes = byname.get(o["name"].strip().upper(), [])
        owners = []
        for code in codes:
            v = ProductVariant.objects.filter(sku=code).select_related("product").first()
            owners.append("%s->pid=%s(%s)" % (code, v.product_id if v else None,
                          (v.product.name[:28] if v else "NO VARIANT")))
        print("  pid=%-6s %-46s csv=%s owners=%s"
              % (o["id"], o["name"][:46], codes or "NOT-IN-CSV", owners or "-"))

def orphan_refs():
    ids = list(Product.objects.filter(variants__isnull=True).values_list("id", flat=True))
    if not ids: print("none"); return
    ph = ",".join(str(int(i)) for i in ids)
    checks = [("discount_voucher_products","product_id"),
              ("shipping_shippingmethod_excluded_products","product_id"),
              ("giftcard_giftcard","product_id"),
              ("attribute_attributevalue","reference_product_id"),
              ("product_productmedia","product_id"),
              ("product_producttranslation","product_id"),
              ("product_collectionproduct","product_id")]
    with connection.cursor() as c:
        for tbl,col in checks:
            c.execute("SELECT COUNT(*) FROM %s WHERE %s IN (%s)" % (tbl,col,ph))
            print("  %-46s %s" % (tbl, c.fetchone()[0]))

def command_flags():
    for app,label in (("saleor.core","update_search_indexes"),
                      ("saleor.product","update_all_products_discounted_prices")):
        print("\n--- %s ---" % label)
        cmd = load_command_class(app, label)
        cmd.create_parser("manage.py", label).print_help()

def beat_state():
    from django_celery_beat.models import PeriodicTask
    qs = PeriodicTask.objects.all()
    print("PeriodicTask rows:", qs.count())
    for t in qs[:30]:
        print("  %-44s enabled=%s runs=%s last=%s" % (t.name[:44], t.enabled, t.total_run_count, t.last_run_at))

for t, fn in [("J. WEIGHT STORAGE CONVENTION (raw vs descriptor)", weight_truth),
              ("K. SENTINEL vs FINITE SKU CLASSIFICATION FROM CSV", classify_sentinel),
              ("L. ORPHAN PRODUCT SKU OWNERSHIP TEST", orphan_mechanism),
              ("M. ORPHAN FK REFERENCES (deletion safety)", orphan_refs),
              ("N. EXISTING CORE COMMAND FLAGS (no execution)", command_flags),
              ("O. CELERY BEAT DB STATE", beat_state)]:
    run(t, fn)
sec("READ-ONLY PROBE COMPLETE — NOTHING MODIFIED")
