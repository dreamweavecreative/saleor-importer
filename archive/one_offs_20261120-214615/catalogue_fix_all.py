#!/usr/bin/env python3
"""CAAG catalogue: review + amend. Importer-only. No Saleor backend files touched.
Dry-run by default. --apply writes. --skip-commands skips the two core rebuilds."""
import os, sys, csv, json, argparse, collections
from datetime import datetime, timezone
from decimal import Decimal
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

ap = argparse.ArgumentParser()
ap.add_argument("--apply", action="store_true")
ap.add_argument("--skip-commands", action="store_true")
ap.add_argument("--csv", default="/tmp/imports/incoming/DickerData.csv")
A = ap.parse_args()
APPLY = A.apply
TS = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
BK = "/tmp/catalogue-fix-%s" % TS
os.makedirs(BK, exist_ok=True); os.chmod(BK, 0o700)
CH = Channel.objects.filter(is_active=True).first()
SENT = 999999999
NONPHYS = {"SOFTWARE", "WARRANTY", "SERVICES"}
PRIMARY_CANON = {
 "NOTEBOOKS": ("Computers","Laptops"), "DESKTOPS": ("Computers","Desktops"),
 "WORKSTATIONS": ("Computers","Workstations"), "SERVERS": ("Computers","Servers"),
 "TABLETS": ("Computers","Tablets"), "COMPONENTS": ("Computers","Components"),
 "STORAGE": ("Storage","Storage Devices"), "DISPLAYS": ("Displays","Monitors"),
 "PRINTERS": ("Printers","Printers"), "CONSUMABLES": ("Printers","Consumables"),
 "NETWORKING": ("Networking","Networking"),
 "COMMUNICATIONS": ("Communications","Communications"),
 "VIDEO CONFERENCING": ("Communications","Video Conferencing"),
 "SURVEILLANCE": ("Security & Surveillance","Surveillance"),
 "ACCESS CONTROL": ("Security & Surveillance","Access Control"),
 "SOFTWARE": ("Software","Software"),
 "WARRANTY": ("Services & Warranty","Warranty"),
 "SERVICES": ("Services & Warranty","Services"),
 "UPS": ("Power & UPS","UPS"), "RACKS": ("Power & UPS","Racks"),
 "PERIPHERALS": ("Peripherals","Peripherals"),
 "BAGS AND CASES": ("Accessories","Bags & Cases"),
}
def sec(t): print("\n" + "="*88 + "\n" + t + "\n" + "="*88)
def save(name, obj):
    p = os.path.join(BK, name + ".json")
    with open(p, "w") as fh: json.dump(obj, fh, default=str)
    os.chmod(p, 0o600); print("  backup:", p)
def mode(): return "APPLY" if APPLY else "DRY-RUN"

sec("0. LOAD SUPPLIER FEED (authoritative source)")
if not os.path.exists(A.csv): sys.exit("CSV not found: %s" % A.csv)
FEED = {}
with open(A.csv, newline="", encoding="utf-8-sig") as fh:
    for r in csv.DictReader(fh):
        s = (r.get("StockCode") or "").strip()
        if not s: continue
        FEED[s] = {"vendor": (r.get("Vendor") or "").strip(),
                   "p": (r.get("PrimaryCategory") or "").strip().upper(),
                   "s": (r.get("SecondaryCategory") or "").strip(),
                   "t": (r.get("TertiaryCategory") or "").strip()}
print("feed rows:", len(FEED))
P2SKU = {}
for pid, sku in ProductVariant.objects.values_list("product_id", "sku").order_by("product_id", "id"):
    P2SKU.setdefault(pid, sku)
def finfo(pid): return FEED.get(P2SKU.get(pid) or "", {})
ALLP = list(Product.objects.values_list("id", flat=True))
print("products:", len(ALLP), "| variants:", ProductVariant.objects.count(),
      "| mapped product->sku:", len(P2SKU))

sec("1. QUARANTINE COMMERCIALLY UNSAFE VARIANT LISTINGS")
risky = list(ProductVariantChannelListing.objects.filter(channel=CH).filter(
    Q(price_amount=0) | Q(cost_price_amount__gt=F("price_amount")) |
    Q(price_amount__gte=Decimal("9999999"))).values("id","variant_id","price_amount",
    "cost_price_amount","is_available_for_purchase"))
tgt = [r for r in risky if r["is_available_for_purchase"]]
print("[%s] candidates=%d to_quarantine=%d" % (mode(), len(risky), len(tgt)))
if APPLY and tgt:
    save("quarantine", tgt)
    ProductVariantChannelListing.objects.filter(
        id__in=[r["id"] for r in tgt]).update(is_available_for_purchase=False)
    print("  quarantined:", len(tgt))

sec("2. NULL FABRICATED WEIGHTS (column stores GRAMS)")
fab = Product.objects.filter(weight=0.5)
print("[%s] products with raw weight 0.5 (=0.5 g, fabricated): %d" % (mode(), fab.count()))
print("  genuine weights preserved, e.g.:",
      list(Product.objects.exclude(weight__isnull=True).exclude(weight=0.5)
           .values_list("id","weight")[:5]))
if APPLY and fab.exists():
    save("weights", list(fab.values_list("id","weight")))
    print("  nulled:", fab.update(weight=None))

sec("3. UNLIMITED STOCK -> track_inventory=False, quantity=0")
sq = Stock.objects.filter(quantity=SENT, quantity_allocated=0)
sids = list(sq.values_list("id", flat=True))
svids = list(sq.values_list("product_variant_id", flat=True))
print("[%s] sentinel stock rows=%d variants=%d" % (mode(), len(sids), len(svids)))
if APPLY and sids:
    save("sentinel_stock", [{"id": i, "quantity": SENT} for i in sids])
    with transaction.atomic():
        n1 = ProductVariant.objects.filter(id__in=svids).update(track_inventory=False)
        n2 = Stock.objects.filter(id__in=sids).update(quantity=0)
    print("  variants untracked:", n1, "| stock zeroed:", n2)

sec("4. PRODUCT TYPE SPLIT BY CATEGORY (non-shipping for SOFTWARE/WARRANTY/SERVICES)")
phys, _ = ProductType.objects.get_or_create(slug="physical-goods", defaults={
    "name":"Physical goods","has_variants":True,"is_shipping_required":True,
    "is_digital":False,"kind":"NORMAL","weight":0.0})
dig, created = ProductType.objects.get_or_create(slug="digital-services", defaults={
    "name":"Digital & Services","has_variants":True,"is_shipping_required":False,
    "is_digital":False,"kind":"NORMAL","weight":0.0})
print("  digital type created:", created, "| id:", dig.id)
want_dig = [pid for pid in ALLP if finfo(pid).get("p") in NONPHYS]
cur_dig = set(Product.objects.filter(product_type=dig).values_list("id", flat=True))
move_dig = [p for p in want_dig if p not in cur_dig]
move_phys = [pid for pid in ALLP if finfo(pid).get("p") not in NONPHYS
             and Product.objects.filter(id=pid, product_type=dig).exists()]
print("[%s] -> digital/services: %d | back to physical: %d" % (mode(), len(move_dig), len(move_phys)))
if APPLY and (move_dig or move_phys):
    save("product_types", {"to_digital": move_dig, "to_physical": move_phys})
    Product.objects.filter(id__in=move_dig).update(product_type=dig, search_index_dirty=True)
    if move_phys:
        Product.objects.filter(id__in=move_phys).update(product_type=phys, search_index_dirty=True)

sec("5. CATEGORY TAXONOMY FROM SUPPLIER PRIMARY CATEGORY")
def canon(p):
    return PRIMARY_CANON.get(p, ("Other", (p or "Uncategorised").title()))
leafcache = {}
def leaf(p):
    if p in leafcache: return leafcache[p]
    root_n, child_n = canon(p)
    root, _ = Category.objects.get_or_create(slug=slugify(root_n)[:255],
        defaults={"name": root_n[:250], "description_plaintext": root_n,
                  "seo_title": root_n[:70], "seo_description": ("Shop %s at CAA Global." % root_n)[:300]})
    node = Category.objects.filter(slug=slugify(child_n)[:255], parent=root).first()
    if node is None:
        node = Category.objects.create(name=child_n[:250], parent=root,
            slug=slugify(child_n)[:255], description_plaintext=child_n,
            seo_title=child_n[:70], seo_description=("Buy %s online at CAA Global." % child_n)[:300])
    leafcache[p] = node; return node
targets = {}
for pid in ALLP:
    p = finfo(pid).get("p")
    if p is None: continue
    targets.setdefault(p, []).append(pid)
print("[%s] distinct supplier primary categories: %d" % (mode(), len(targets)))
for p, ids in sorted(targets.items(), key=lambda x: -len(x[1]))[:25]:
    print("   %-24s -> %-22s %d products" % (p or "(blank)", "/".join(canon(p)), len(ids)))
if APPLY:
    save("category_plan", {p: len(i) for p, i in targets.items()})
    for p, ids in targets.items():
        node = leaf(p)
        Product.objects.filter(id__in=ids).exclude(category=node).update(
            category=node, search_index_dirty=True)
    for c in Category.objects.all():
        upd = {}
        if not c.seo_title: upd["seo_title"] = c.name[:70]
        if not c.seo_description: upd["seo_description"] = ("Browse %s at CAA Global." % c.name)[:300]
        if not c.description_plaintext: upd["description_plaintext"] = c.name
        if upd: Category.objects.filter(id=c.id).update(**upd)
    print("  categories now:", Category.objects.count())

sec("6. MANUFACTURER ATTRIBUTE (dropdown; Brand is Page-reference, unusable)")
attr, ac = Attribute.objects.get_or_create(slug="manufacturer", defaults={
    "name":"Manufacturer","type":"product-type","input_type":"dropdown",
    "value_required":False,"visible_in_storefront":True,"filterable_in_storefront":True,
    "filterable_in_dashboard":True,"available_in_grid":True,
    "storefront_search_position":1,"is_variant_only":False})
print("  attribute created:", ac, "| id:", attr.id)
if APPLY:
    for i, pt in enumerate(ProductType.objects.all()):
        AttributeProduct.objects.get_or_create(attribute=attr, product_type=pt,
            defaults={"sort_order": i})
vendors = sorted({v["vendor"] for v in FEED.values() if v["vendor"]})
print("  distinct vendors in feed:", len(vendors))
if APPLY:
    vmap = {}
    for i, v in enumerate(vendors):
        av, _ = AttributeValue.objects.get_or_create(attribute=attr, slug=slugify(v)[:255],
            defaults={"name": v[:250], "value": v[:255], "sort_order": i})
        vmap[v] = av.id
    rows, seen = [], set()
    for pid in ALLP:
        v = finfo(pid).get("vendor")
        if v and v in vmap and (vmap[v], pid) not in seen:
            seen.add((vmap[v], pid))
            rows.append(AssignedProductAttributeValue(product_id=pid, value_id=vmap[v], sort_order=0))
    AssignedProductAttributeValue.objects.bulk_create(rows, batch_size=2000, ignore_conflicts=True)
    print("  manufacturer assignments:", AssignedProductAttributeValue.objects.filter(
        value__attribute=attr).count())

sec("7. BACKFILL PRODUCT METADATA (brand + supplier category)")
need = [pid for pid in ALLP if finfo(pid)]
print("[%s] products with feed data: %d" % (mode(), len(need)))
if APPLY:
    n = 0
    for pid in need:
        f = finfo(pid)
        md = {"brand": f.get("vendor",""), "supplier_category": {
              "primary": f.get("p",""), "secondary": f.get("s",""), "tertiary": f.get("t","")}}
        Product.objects.filter(id=pid).update(metadata=md)
        n += 1
        if n % 10000 == 0: print("   ...", n)
    print("  metadata written:", n)

sec("8. COLLECTIONS: brand + category, links, SEO, channel listings")
plans = {}
for pid in ALLP:
    f = finfo(pid)
    if not f: continue
    if f.get("vendor"): plans.setdefault("brand:" + f["vendor"], []).append(pid)
    plans.setdefault("cat:" + canon(f.get("p"))[0], []).append(pid)
print("[%s] planned collections: %d" % (mode(), len(plans)))
if APPLY:
    made = 0
    for key, ids in plans.items():
        kind, label = key.split(":", 1)
        name = label if kind == "cat" else "%s Products" % label
        coll, c = Collection.objects.get_or_create(slug=slugify(name)[:255], defaults={
            "name": name[:250],
            "seo_title": name[:70],
            "seo_description": ("Shop %s at CAA Global. Genuine stock, Australia-wide." % name)[:300],
            "description_plaintext": name})
        made += 1 if c else 0
        upd = {}
        if not coll.seo_title: upd["seo_title"] = name[:70]
        if not coll.seo_description: upd["seo_description"] = ("Shop %s at CAA Global." % name)[:300]
        if upd: Collection.objects.filter(id=coll.id).update(**upd)
        CollectionChannelListing.objects.update_or_create(collection=coll, channel=CH,
            defaults={"is_published": True, "published_at": datetime.now(timezone.utc)})
        CollectionProduct.objects.bulk_create(
            [CollectionProduct(collection_id=coll.id, product_id=i) for i in ids],
            batch_size=2000, ignore_conflicts=True)
    print("  new collections:", made, "| total:", Collection.objects.count(),
          "| links:", CollectionProduct.objects.count())

sec("9. DELETE ORPHAN PRODUCT SHELLS (duplicate StockCode; FK refs must be 0)")
orph = list(Product.objects.filter(variants__isnull=True).values_list("id", flat=True))
print("orphans:", len(orph))
refs = {}
if orph:
    ph = ",".join(str(int(i)) for i in orph)
    with connection.cursor() as c:
        for t, col in [("discount_voucher_products","product_id"),
                       ("shipping_shippingmethod_excluded_products","product_id"),
                       ("giftcard_giftcard","product_id"),
                       ("attribute_attributevalue","reference_product_id"),
                       ("product_productmedia","product_id"),
                       ("product_producttranslation","product_id"),
                       ("product_collectionproduct","product_id")]:
            c.execute("SELECT COUNT(*) FROM %s WHERE %s IN (%s)" % (t, col, ph))
            refs[t] = c.fetchone()[0]
    print("  FK refs:", refs)
if APPLY and orph and sum(refs.values()) == 0:
    save("orphans", orph)
    print("  deleted:", Product.objects.filter(id__in=orph).delete())
elif orph and sum(refs.values()) > 0:
    print("  SKIPPED — references exist, manual review required")

sec("10. VARIANT sort_order (NULL -> sequential per product)")
with connection.cursor() as c:
    c.execute("SELECT COUNT(*) FROM product_productvariant WHERE sort_order IS NULL")
    nnull = c.fetchone()[0]
print("[%s] variants with NULL sort_order: %d" % (mode(), nnull))
if APPLY and nnull:
    with connection.cursor() as c:
        c.execute("""UPDATE product_productvariant v SET sort_order = s.rn
                     FROM (SELECT id, ROW_NUMBER() OVER (PARTITION BY product_id ORDER BY id)-1 AS rn
                           FROM product_productvariant WHERE sort_order IS NULL) s
                     WHERE v.id = s.id""")
    print("  ordered.")

sec("11. MARK SEARCH DIRTY, THEN RUN EXISTING CORE COMMANDS")
if APPLY:
    Product.objects.update(search_index_dirty=True)
    ProductChannelListing.objects.filter(channel=CH).update(discounted_price_dirty=True)
    if A.skip_commands:
        print("  skipped (--skip-commands)")
    else:
        for cmd in ("update_all_products_discounted_prices", "update_search_indexes"):
            print("  running:", cmd, flush=True)
            t0 = datetime.now(timezone.utc)
            call_command(cmd, verbosity=1)
            print("  done in", (datetime.now(timezone.utc)-t0).total_seconds(), "s")

sec("12. VERIFICATION")
print("products:", Product.objects.count(), "| variants:", ProductVariant.objects.count())
print("weight fabricated 0.5 remaining:", Product.objects.filter(weight=0.5).count())
print("sentinel stock remaining:", Stock.objects.filter(quantity=SENT).count())
print("untracked variants:", ProductVariant.objects.filter(track_inventory=False).count())
print("categories:", Category.objects.count(), "| products w/ category:",
      Product.objects.exclude(category_id=None).count())
print("collections:", Collection.objects.count(), "| products in collections:",
      CollectionProduct.objects.values("product_id").distinct().count())
print("manufacturer assignments:", AssignedProductAttributeValue.objects.filter(
    value__attribute__slug="manufacturer").count())
print("orphans:", Product.objects.filter(variants__isnull=True).count())
print("NULL sort_order:", ProductVariant.objects.filter(sort_order__isnull=True).count())
print("risky still available:", ProductVariantChannelListing.objects.filter(channel=CH,
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
print("\nbackups:", BK)
print("MODE:", mode(), "— re-run with --apply to write" if not APPLY else "— changes applied")
