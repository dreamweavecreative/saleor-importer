import os, sys, csv, inspect, collections
sys.path.insert(0, "/app")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "saleor.settings")
import django; django.setup()
from django.db.models import Q, Count, F
from django.conf import settings
from saleor.channel.models import Channel
from saleor.product.models import (Product, ProductVariant, Category, Collection,
    ProductChannelListing, ProductVariantChannelListing)

CH = Channel.objects.filter(is_active=True).first()
def sec(t): print("\n"+"="*88+"\n"+t+"\n"+"="*88, flush=True)
def run(t, fn):
    sec(t)
    try: fn()
    except Exception as e: print("  ERROR %s: %s" % (type(e).__name__, e), flush=True)

def a_command_source():
    from django.core.management import load_command_class
    cmd = load_command_class("saleor.core", "update_search_indexes")
    print(inspect.getsource(type(cmd)))

def b_search_helpers():
    import importlib
    for mod in ("saleor.product.search", "saleor.core.search_tasks",
                "saleor.product.utils.search_helpers", "saleor.product.tasks"):
        try:
            m = importlib.import_module(mod)
        except Exception as e:
            print("%s -> %s" % (mod, e)); continue
        fns = [n for n, f in inspect.getmembers(m, inspect.isfunction)
               if "search" in n.lower()]
        print("\n%s functions: %s" % (mod, fns))
        for n in fns:
            if "vector" in n.lower() or "document" in n.lower():
                src = inspect.getsource(getattr(m, n))
                print("--- %s.%s ---" % (mod, n))
                print(src[:1800])

def c_celery():
    print("CELERY_TASK_ALWAYS_EAGER =", getattr(settings, "CELERY_TASK_ALWAYS_EAGER", "N/A"))
    b = str(getattr(settings, "CELERY_BROKER_URL", ""))
    print("broker scheme =", b.split("://")[0] if "://" in b else "unset")
    try:
        from saleor.celeryconf import app
        insp = app.control.inspect(timeout=4)
        print("worker stats:", insp.stats())
        print("active:", insp.active())
        print("reserved:", insp.reserved())
        print("registered search tasks:",
              [t for t in (insp.registered() or {}).get(list((insp.stats() or {}).keys())[0], [])
               if "search" in t] if insp.stats() else "no workers responded")
    except Exception as e:
        print("celery inspect failed:", e)
    try:
        from django_celery_beat.models import PeriodicTask
        for t in PeriodicTask.objects.filter(name__icontains="search"):
            print("beat: %s enabled=%s runs=%s last=%s" %
                  (t.name, t.enabled, t.total_run_count, t.last_run_at))
    except Exception as e:
        print("beat read failed:", e)

def d_null_price():
    nulls = ProductChannelListing.objects.filter(
        channel=CH, discounted_price_amount__isnull=True).values_list("product_id", flat=True)
    ids = list(nulls)
    print("products with NULL discounted price:", len(ids))
    all_unavail = 0
    for pid in ids:
        vs = ProductVariantChannelListing.objects.filter(
            channel=CH, variant__product_id=pid)
        if vs.exists() and not vs.filter(is_available_for_purchase=True).exists():
            all_unavail += 1
    print("of those, ALL variants unavailable:", all_unavail)
    print("=> expected/correct:", all_unavail, "| unexplained:", len(ids) - all_unavail)
    for pid in ids[:10]:
        p = Product.objects.get(id=pid)
        vs = ProductVariantChannelListing.objects.filter(channel=CH, variant__product_id=pid)
        print("  [%s] %-52s variants=%s available=%s" %
              (pid, p.name[:52], vs.count(), vs.filter(is_available_for_purchase=True).count()))

def e_vendors():
    path = "/tmp/imports/incoming/DickerData.csv"
    if not os.path.exists(path): print("CSV missing"); return
    c = collections.Counter()
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            v = (r.get("Vendor") or "").strip()
            if v: c[v] += 1
    print("distinct vendors:", len(c))
    print("\nAll vendors with >=25 rows (name | len | rows):")
    for v, n in c.most_common():
        if n >= 25: print("  %-14s len=%-3s rows=%s" % (v, len(v), n))

def f_taxonomy():
    print("ROOT categories holding products directly:")
    for c in Category.objects.filter(parent__isnull=True).annotate(
            n=Count("products")).filter(n__gt=0).order_by("-n"):
        kids = Category.objects.filter(parent=c).count()
        print("  [%s] %-26s products=%-7s children=%s" % (c.id, c.name, c.n, kids))
    print("\nLeaf categories with products whose parent also has products:")
    for c in Category.objects.exclude(parent__isnull=True).annotate(
            n=Count("products")).filter(n__gt=0):
        pn = Product.objects.filter(category_id=c.parent_id).count()
        if pn: print("  [%s] %-22s n=%-6s parent=%s n=%s" % (c.id, c.name, c.n, c.parent.name, pn))
    print("\nEmpty leaf categories (cleanup candidates):")
    for c in Category.objects.exclude(parent__isnull=True).annotate(
            n=Count("products")).filter(n=0):
        print("  [%s] %s (parent=%s)" % (c.id, c.name, c.parent.name))
    print("\nCollections with near-duplicate names:")
    names = list(Collection.objects.values_list("id", "name"))
    for i, (id1, n1) in enumerate(names):
        for id2, n2 in names[i+1:]:
            a1 = n1.lower().replace(" products", "").strip()
            a2 = n2.lower().replace(" products", "").strip()
            if a1 and a2 and (a1.startswith(a2[:6]) or a2.startswith(a1[:6])):
                print("  [%s] %-28s <-> [%s] %s" % (id1, n1, id2, n2))

for t, fn in [("A. update_search_indexes SOURCE", a_command_source),
              ("B. SEARCH HELPER FUNCTIONS", b_search_helpers),
              ("C. CELERY WORKER / BEAT STATE", c_celery),
              ("D. 111 NULL DISCOUNTED PRICES", d_null_price),
              ("E. VENDOR NAMES FROM FEED", e_vendors),
              ("F. TAXONOMY STRUCTURE", f_taxonomy)]:
    run(t, fn)
sec("READ-ONLY PROBE COMPLETE — NOTHING MODIFIED")
