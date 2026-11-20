#!/usr/bin/env python3
"""CAAG catalogue remediation v2. TRUE dry-run (zero writes without --apply).
Importer-only: no Saleor backend file is created, edited or migrated."""
import os, sys, csv, json, argparse, collections
from datetime import datetime, timezone
sys.path.insert(0, "/app")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "saleor.settings")
import django; django.setup()

from django.db import transaction, connection
from django.db.models import Q, F, Count
from django.utils.text import slugify
from django.core.management import call_command
from saleor.channel.models import Channel
from saleor.product.models import (Product, ProductVariant, ProductType, Category,
    Collection, CollectionProduct, CollectionChannelListing,
    ProductChannelListing, ProductVariantChannelListing)
from saleor.attribute.models import (Attribute, AttributeProduct, AttributeValue,
    AssignedProductAttributeValue)
from saleor.warehouse.models import Stock
from decimal import Decimal

ap = argparse.ArgumentParser()
ap.add_argument("--apply", action="store_true")
ap.add_argument("--run-commands", action="store_true")
ap.add_argument("--min-brand-products", type=int, default=25)
ap.add_argument("--csv", default="/tmp/imports/incoming/DickerData.csv")
A = ap.parse_args()
APPLY = A.apply
TS = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
BK = "/tmp/catalogue-fix-%s" % TS
CH = Channel.objects.filter(is_active=True).first()
SENT = 999999999
NONPHYS = {"SOFTWARE", "WARRANTY", "SERVICES"}
CANON = {
 "NOTEBOOKS": ("Computers","Laptops"), "DESKTOPS": ("Computers","Desktops"),
 "WORKSTATIONS": ("Computers","Workstations"), "SERVERS": ("Computers","Servers"),
 "TABLETS": ("Computers","Tablets"), "COMPONENTS": ("Computers","Components"),
 "STORAGE": ("Storage","Storage Devices"), "STORAGE MEDIA": ("Storage","Storage Media"),
 "DISPLAYS": ("Displays","Monitors"), "PRINTERS": ("Printers","Printers"),
 "CONSUMABLES": ("Printers","Consumables"), "NETWORKING": ("Networking","Networking"),
 "COMMUNICATIONS": ("Communications","Communications"),
 "VIDEO CONFERENCING": ("Communications","Video Conferencing"),
 "SURVEILLANCE": ("Security & Surveillance","Surveillance"),
 "ACCESS CONTROL": ("Security & Surveillance","Access Control"),
 "INTRUSION": ("Security & Surveillance","Intrusion Detection"),
 "SOFTWARE": ("Software","Software"),
 "WARRANTY": ("Services & Warranty","Warranty"),
 "SERVICES": ("Services & Warranty","Services"),
 "UPS": ("Power & UPS","UPS"), "RACKS": ("Power & UPS","Racks"),
 "PERIPHERALS": ("Peripherals","Peripherals"),
 "SMART HOME": ("Smart Home","Smart Home"),
 "BAGS AND CASES": ("Accessories","Bags & Cases"),
}
def sec(t): print("\n" + "="*88 + "\n" + t + "\n" + "="*88)
def mode(): return "APPLY" if APPLY else "DRY-RUN"
def save(name, obj):
    os.makedirs(BK, exist_ok=True); os.chmod(BK, 0o700)
    p = os.path.join(BK, name + ".json")
    with open(p, "w") as fh: json.dump(obj, fh, default=str)
    os.chmod(p, 0o600); print("  backup:", p)
def plan_or_create(model, lookup, defaults, label):
    obj = model.objects.filter(**lookup).first()
    if obj: return obj
    if not APPLY:
        print("  [PLAN] would create %s: %s" % (label, lookup)); return None
    obj = model.objects.create(**{**lookup, **defaults})
    print("  created %s id=%s" % (label, obj.pk)); return obj

sec("0. LOAD FEED")
if not os.path.exists(A.csv): sys.exit("CSV missing: " + A.csv)
FEED = {}
with open(A.csv, newline="", encoding="utf-8-sig") as fh:
    for r in csv.DictReader(fh):
        s = (r.get("StockCode") or "").strip()
        if s: FEED[s] = {"vendor": (r.get("Vendor") or "").strip(),
                         "p": (r.get("PrimaryCategory") or "").strip().upper(),
                         "s": (r.get("SecondaryCategory") or "").strip(),
                         "t": (r.get("TertiaryCategory") or "").strip()}
P2SKU = {}
for pid, sku in ProductVariant.objects.values_list("product_id","sku").order_by("product_id","id"):
    P2SKU.setdefault(pid, sku)
ALLP = list(Product.objects.values_list("id", flat=True))
def finfo(pid): return FEED.get(P2SKU.get(pid) or "", {})
print("feed=%d products=%d variants=%d mapped=%d" %
      (len(FEED), len(ALLP), ProductVariant.objects.count(), len(P2SKU)))

sec("1. QUARANTINE UNSAFE PRICING")
risky = list(ProductVariantChannelListing.objects.filter(channel=CH,
    is_available_for_purchase=True).filter(
    Q(price_amount=0) | Q(cost_price_amount__gt=F("price_amount")) |
    Q(price_amount__gte=Decimal("9999999"))).values("id","variant_id","is_available_for_purchase"))
print("[%s] to quarantine: %d" % (mode(), len(risky)))
if APPLY and risky:
    save("quarantine", risky)
    ProductVariantChannelListing.objects.filter(id__in=[r["id"] for r in risky]).update(
        is_available_for_purchase=False)
    print("  done")

sec("2. NULL FABRICATED WEIGHTS (column = grams)")
fab = Product.objects.filter(weight=0.5)
print("[%s] fabricated 0.5 g: %d" % (mode(), fab.count()))
if APPLY and fab.exists():
    save("weights", list(fab.values_list("id","weight")))
    print("  nulled:", fab.update(weight=None))

sec("3. UNLIMITED STOCK -> untracked")
sq = Stock.objects.filter(quantity=SENT, quantity_allocated=0)
sids = list(sq.values_list("id", flat=True)); svids = list(sq.values_list("product_variant_id", flat=True))
print("[%s] sentinel rows: %d" % (mode(), len(sids)))
if APPLY and sids:
    save("sentinel", sids)
    with transaction.atomic():
        ProductVariant.objects.filter(id__in=svids).update(track_inventory=False)
        Stock.objects.filter(id__in=sids).update(quantity=0)
    print("  done")

sec("4. PRODUCT TYPE SPLIT (non-shipping for SOFTWARE/WARRANTY/SERVICES)")
phys = plan_or_create(ProductType, {"slug":"physical-goods"},
    {"name":"Physical goods","has_variants":True,"is_shipping_required":True,
     "is_digital":False,"kind":"NORMAL","weight":0.0}, "ProductType")
dig = plan_or_create(ProductType, {"slug":"digital-services"},
    {"name":"Digital & Services","has_variants":True,"is_shipping_required":False,
     "is_digital":False,"kind":"NORMAL","weight":0.0}, "ProductType")
want = [pid for pid in ALLP if finfo(pid).get("p") in NONPHYS]
cur = set(Product.objects.filter(product_type__slug="digital-services").values_list("id", flat=True))
move = [p for p in want if p not in cur]
print("[%s] -> digital/services: %d (already there: %d)" % (mode(), len(move), len(want)-len(move)))
if APPLY and dig and move:
    save("to_digital", move)
    for i in range(0, len(move), 5000):
        Product.objects.filter(id__in=move[i:i+5000]).update(product_type=dig, search_index_dirty=True)
    print("  moved:", len(move))

sec("5. CATEGORY TAXONOMY (reuse existing by NAME, avoid slug duplicates)")
def canon(p): return CANON.get(p, ("Other", (p or "Uncategorised").title()))
groups = collections.defaultdict(list)
for pid in ALLP:
    f = finfo(pid)
    if f: groups[f.get("p","")].append(pid)
leafcache = {}
def resolve(p):
    if p in leafcache: return leafcache[p]
    rn, cn = canon(p)
    root = Category.objects.filter(name__iexact=rn, parent__isnull=True).first() \
        or Category.objects.filter(slug=slugify(rn)[:255], parent__isnull=True).first()
    if root is None:
        root = plan_or_create(Category, {"slug": slugify(rn)[:255]},
            {"name": rn[:250], "description_plaintext": rn, "seo_title": rn[:70],
             "seo_description": ("Shop %s at CAA Global." % rn)[:300]}, "root category")
    node = None
    if root is not None:
        node = Category.objects.filter(name__iexact=cn, parent=root).first()
        if node is None:
            node = plan_or_create(Category, {"slug": slugify(cn)[:255]},
                {"name": cn[:250], "parent": root, "description_plaintext": cn,
                 "seo_title": cn[:70],
                 "seo_description": ("Buy %s online at CAA Global." % cn)[:300]}, "leaf category")
    leafcache[p] = node; return node
print("[%s] supplier primary categories: %d" % (mode(), len(groups)))
for p, ids in sorted(groups.items(), key=lambda x: -len(x[1])):
    print("   %-22s -> %-38s %d" % (p or "(blank)", "/".join(canon(p)), len(ids)))
if APPLY:
    for p, ids in groups.items():
        node = resolve(p)
        if node is None: continue
        for i in range(0, len(ids), 5000):
            Product.objects.filter(id__in=ids[i:i+5000]).exclude(category=node).update(
                category=node, search_index_dirty=True)
    for c in Category.objects.all():
        u = {}
        if not c.seo_title: u["seo_title"] = c.name[:70]
        if not c.seo_description: u["seo_description"] = ("Browse %s at CAA Global." % c.name)[:300]
        if not c.description_plaintext: u["description_plaintext"] = c.name
        if u: Category.objects.filter(id=c.id).update(**u)
    print("  categories now:", Category.objects.count())
    empty = Category.objects.annotate(n=Count("products")).filter(n=0)
    print("  EMPTY categories (left in place, review manually):",
          [(c.id, c.name) for c in empty])
else:
    for p in groups: resolve(p)

sec("6. MANUFACTURER ATTRIBUTE (dropdown)")
attr = plan_or_create(Attribute, {"slug":"manufacturer"},
    {"name":"Manufacturer","type":"product-type","input_type":"dropdown",
     "value_required":False,"visible_in_storefront":True,"filterable_in_storefront":True,
     "filterable_in_dashboard":True,"available_in_grid":True,
     "storefront_search_position":1,"is_variant_only":False}, "Attribute")
vendors = sorted({v["vendor"] for v in FEED.values() if v["vendor"]})
print("[%s] vendors in feed: %d" % (mode(), len(vendors)))
if APPLY and attr:
    for i, pt in enumerate(ProductType.objects.all()):
        AttributeProduct.objects.get_or_create(attribute=attr, product_type=pt,
            defaults={"sort_order": i})
    vmap = {}
    for i, v in enumerate(vendors):
        av = AttributeValue.objects.filter(attribute=attr, slug=slugify(v)[:255]).first()
        if av is None:
            av = AttributeValue.objects.create(attribute=attr, slug=slugify(v)[:255],
                name=v[:250], value=v[:255], sort_order=i)
        vmap[v] = av.id
    rows, seen = [], set()
    for pid in ALLP:
        v = finfo(pid).get("vendor")
        if v and v in vmap and (vmap[v], pid) not in seen:
            seen.add((vmap[v], pid))
            rows.append(AssignedProductAttributeValue(product_id=pid, value_id=vmap[v], sort_order=0))
    for i in range(0, len(rows), 5000):
        AssignedProductAttributeValue.objects.bulk_create(rows[i:i+5000], ignore_conflicts=True)
    print("  assignments:", AssignedProductAttributeValue.objects.filter(value__attribute=attr).count())

sec("7. BACKFILL PRODUCT METADATA")
need = [pid for pid in ALLP if finfo(pid)]
print("[%s] products to backfill: %d" % (mode(), len(need)))
if APPLY and need:
    n = 0
    for i in range(0, len(need), 2000):
        batch = []
        for p in Product.objects.filter(id__in=need[i:i+2000]).only("id","metadata"):
            f = finfo(p.id)
            p.metadata = {"brand": f.get("vendor",""), "supplier_category": {
                "primary": f.get("p",""), "secondary": f.get("s",""), "tertiary": f.get("t","")}}
            batch.append(p)
        Product.objects.bulk_update(batch, ["metadata"], batch_size=2000)
        n += len(batch)
        if n % 10000 == 0: print("   ...", n)
    print("  written:", n)

sec("8. COLLECTIONS (brand >= %d products + root categories)" % A.min_brand_products)
vcount = collections.Counter(finfo(pid).get("vendor","") for pid in ALLP)
plans = {}
for pid in ALLP:
    f = finfo(pid)
    if not f: continue
    v = f.get("vendor","")
    if v and vcount[v] >= A.min_brand_products: plans.setdefault("brand:"+v, []).append(pid)
    plans.setdefault("cat:"+canon(f.get("p",""))[0], []).append(pid)
skipped = sorted(v for v,c in vcount.items() if v and c < A.min_brand_products)
print("[%s] planned collections: %d | vendors below threshold skipped: %d"
      % (mode(), len(plans), len(skipped)))
for k, ids in sorted(plans.items(), key=lambda x: -len(x[1]))[:20]:
    print("   %-46s %d" % (k, len(ids)))
if APPLY:
    for key, ids in plans.items():
        kind, label = key.split(":", 1)
        name = label if kind == "cat" else "%s Products" % label
        coll = Collection.objects.filter(name__iexact=name).first()
        if coll is None:
            coll = plan_or_create(Collection, {"slug": slugify(name)[:255]},
                {"name": name[:250], "seo_title": name[:70],
                 "seo_description": ("Shop %s at CAA Global. Genuine stock, Australia-wide." % name)[:300],
                 "description_plaintext": name}, "Collection")
        if coll is None: continue
        u = {}
        if not coll.seo_title: u["seo_title"] = name[:70]
        if not coll.seo_description: u["seo_description"] = ("Shop %s at CAA Global." % name)[:300]
        if u: Collection.objects.filter(id=coll.id).update(**u)
        CollectionChannelListing.objects.update_or_create(collection=coll, channel=CH,
            defaults={"is_published": True, "published_at": datetime.now(timezone.utc)})
        objs = [CollectionProduct(collection_id=coll.id, product_id=i) for i in ids]
        for i in range(0, len(objs), 5000):
            CollectionProduct.objects.bulk_create(objs[i:i+5000], ignore_conflicts=True)
    print("  collections:", Collection.objects.count(), "links:", CollectionProduct.objects.count())

sec("9. DELETE ORPHAN SHELLS (extended FK guard)")
orph = list(Product.objects.filter(variants__isnull=True).values_list("id", flat=True))
print("orphans:", len(orph)); refs = {}
if orph:
    ph = ",".join(str(int(i)) for i in orph)
    with connection.cursor() as c:
        for t, col in [("discount_voucher_products","product_id"),
                       ("shipping_shippingmethod_excluded_products","product_id"),
                       ("giftcard_giftcard","product_id"),
                       ("attribute_attributevalue","reference_product_id"),
                       ("attribute_assignedproductattributevalue","product_id"),
                       ("product_productmedia","product_id"),
                       ("product_producttranslation","product_id"),
                       ("product_collectionproduct","product_id"),
                       ("product_productchannellisting","product_id")]:
            c.execute("SELECT COUNT(*) FROM %s WHERE %s IN (%s)" % (t, col, ph))
            refs[t] = c.fetchone()[0]
    print("  FK refs:", refs)
if APPLY and orph and sum(refs.values()) == 0:
    save("orphans", orph)
    print("  deleted:", Product.objects.filter(id__in=orph).delete())
elif orph and sum(refs.values()) > 0:
    print("  SKIPPED - references exist")

sec("10. VARIANT sort_order")
with connection.cursor() as c:
    c.execute("SELECT COUNT(*) FROM product_productvariant WHERE sort_order IS NULL")
    n = c.fetchone()[0]
print("[%s] NULL sort_order: %d" % (mode(), n))
if APPLY and n:
    with connection.cursor() as c:
        c.execute("""UPDATE product_productvariant v SET sort_order = s.rn
                     FROM (SELECT id, ROW_NUMBER() OVER (PARTITION BY product_id ORDER BY id)-1 AS rn
                           FROM product_productvariant WHERE sort_order IS NULL) s
                     WHERE v.id = s.id""")
    print("  ordered")

sec("11. SEARCH DIRTY + CORE COMMANDS")
print("[%s] will mark all products dirty; commands run only with --run-commands" % mode())
if APPLY:
    Product.objects.update(search_index_dirty=True)
    ProductChannelListing.objects.filter(channel=CH).update(discounted_price_dirty=True)
    if A.run_commands:
        for cmd in ("update_all_products_discounted_prices", "update_search_indexes"):
            print("  running:", cmd, flush=True)
            t0 = datetime.now(timezone.utc)
            call_command(cmd, verbosity=1)
            print("  %s done in %.1fs" % (cmd, (datetime.now(timezone.utc)-t0).total_seconds()), flush=True)
    else:
        print("  marked dirty; run again with --run-commands (or nohup) to rebuild")

sec("12. VERIFICATION")
print("products:", Product.objects.count(), "variants:", ProductVariant.objects.count())
print("fabricated 0.5g:", Product.objects.filter(weight=0.5).count())
print("sentinel stock:", Stock.objects.filter(quantity=SENT).count())
print("untracked variants:", ProductVariant.objects.filter(track_inventory=False).count())
for pt in ProductType.objects.all():
    print("  type %-20s ship=%-5s products=%d" % (pt.name, pt.is_shipping_required,
          Product.objects.filter(product_type=pt).count()))
print("categories:", Category.objects.count(),
      "with category:", Product.objects.exclude(category_id=None).count())
print("collections:", Collection.objects.count(),
      "products in collections:", CollectionProduct.objects.values("product_id").distinct().count())
print("manufacturer assignments:", AssignedProductAttributeValue.objects.filter(
      value__attribute__slug="manufacturer").count())
print("products with metadata:", Product.objects.exclude(metadata={}).count())
print("orphans:", Product.objects.filter(variants__isnull=True).count())
print("NULL sort_order:", ProductVariant.objects.filter(sort_order__isnull=True).count())
print("risky available:", ProductVariantChannelListing.objects.filter(channel=CH,
      is_available_for_purchase=True).filter(Q(price_amount=0) |
      Q(cost_price_amount__gt=F("price_amount")) |
      Q(price_amount__gte=Decimal("9999999"))).count())
print("product listings w/ discounted price:", ProductChannelListing.objects.filter(
      channel=CH).exclude(discounted_price_amount=None).count())
print("search_vector populated:", Product.objects.exclude(search_vector=None).count())
print("dirty search:", Product.objects.filter(search_index_dirty=True).count())
with connection.cursor() as c:
    c.execute("SELECT COUNT(*) FROM product_category WHERE lft>=rght OR tree_id=0")
    print("MPTT anomalies:", c.fetchone()[0])
print("\nMODE:", mode(), "| backups:", BK if APPLY else "(none written)")
