#!/usr/bin/env python3
"""Complete CAAG catalogue remediation: collections, orphans, sort_order,
tax/SEO gap, search + discounted-price rebuild. Importer-only. Dry-run default."""
import os, sys, csv, json, argparse, collections
from datetime import datetime, timezone
sys.path.insert(0, "/app")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "saleor.settings")
import django; django.setup()

from django.db import connection
from django.db.models import Q, Count
from django.utils.text import slugify
from django.core.management import call_command
from saleor.channel.models import Channel
from saleor.product.models import (Product, ProductVariant, Category, Collection,
    CollectionProduct, CollectionChannelListing, ProductChannelListing)
from saleor.attribute.models import AssignedProductAttributeValue
from saleor.tax.models import TaxClass

ap = argparse.ArgumentParser()
ap.add_argument("--apply", action="store_true")
ap.add_argument("--run-commands", action="store_true")
ap.add_argument("--min-brand-products", type=int, default=25)
ap.add_argument("--csv", default="/tmp/imports/incoming/DickerData.csv")
A = ap.parse_args()
APPLY = A.apply
TS = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
BK = "/tmp/catalogue-complete-%s" % TS
CH = Channel.objects.filter(is_active=True).first()
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
def sec(t): print("\n"+"="*88+"\n"+t+"\n"+"="*88, flush=True)
def mode(): return "APPLY" if APPLY else "DRY-RUN"
def save(name, obj):
    os.makedirs(BK, exist_ok=True); os.chmod(BK, 0o700)
    p = os.path.join(BK, name+".json")
    with open(p,"w") as fh: json.dump(obj, fh, default=str)
    os.chmod(p, 0o600); print("  backup:", p, flush=True)
def ed(text):
    return {"blocks":[{"type":"paragraph","data":{"text":text}}]}
def canon(p): return CANON.get(p, ("Other", (p or "Uncategorised").title()))

sec("0. LOAD FEED")
if not os.path.exists(A.csv): sys.exit("CSV missing: "+A.csv)
FEED = {}
with open(A.csv, newline="", encoding="utf-8-sig") as fh:
    for r in csv.DictReader(fh):
        s=(r.get("StockCode") or "").strip()
        if s: FEED[s]={"vendor":(r.get("Vendor") or "").strip(),
                       "p":(r.get("PrimaryCategory") or "").strip().upper()}
P2SKU={}
for pid,sku in ProductVariant.objects.values_list("product_id","sku").order_by("product_id","id"):
    P2SKU.setdefault(pid,sku)
ALLP=list(Product.objects.values_list("id",flat=True))
def finfo(pid): return FEED.get(P2SKU.get(pid) or "", {})
print("feed=%d products=%d mapped=%d" % (len(FEED),len(ALLP),len(P2SKU)), flush=True)

sec("7b. CLOSE TAX / SEO GAP")
tc = TaxClass.objects.filter(name="Australian GST").first() or TaxClass.objects.first()
no_tax = Product.objects.filter(tax_class__isnull=True)
no_seo = Product.objects.filter(Q(seo_title__isnull=True)|Q(seo_title=""))
print("[%s] missing tax_class=%d missing seo_title=%d (tax class id=%s)"
      % (mode(), no_tax.count(), no_seo.count(), tc.id if tc else None), flush=True)
if APPLY:
    if tc and no_tax.exists():
        save("tax_gap", list(no_tax.values_list("id",flat=True)))
        print("  tax set:", no_tax.update(tax_class=tc, search_index_dirty=True))
    n=0
    for p in Product.objects.filter(Q(seo_title__isnull=True)|Q(seo_title="")
                                   ).only("id","name","metadata")[:5000]:
        brand=(p.metadata or {}).get("brand","")
        t=(f"{brand} {p.name}".strip())[:70]
        d=(f"Buy {p.name} at CAA Global. Genuine Australian stock, fast delivery.")[:300]
        Product.objects.filter(id=p.id).update(seo_title=t, seo_description=d,
                                               search_index_dirty=True)
        n+=1
    print("  seo filled:", n)

sec("8. COLLECTIONS (brand >= %d + root categories)" % A.min_brand_products)
vcount=collections.Counter(finfo(pid).get("vendor","") for pid in ALLP)
plans={}
for pid in ALLP:
    f=finfo(pid)
    if not f: continue
    v=f.get("vendor","")
    if v and vcount[v]>=A.min_brand_products: plans.setdefault("brand:"+v,[]).append(pid)
    plans.setdefault("cat:"+canon(f.get("p",""))[0],[]).append(pid)
print("[%s] planned=%d skipped_vendors=%d existing_collections=%d"
      % (mode(),len(plans),sum(1 for v,c in vcount.items() if v and c<A.min_brand_products),
         Collection.objects.count()), flush=True)
if APPLY:
    made=0; linked=0
    for key,ids in plans.items():
        kind,label=key.split(":",1)
        name=label if kind=="cat" else "%s Products" % label
        desc=("Browse %s at CAA Global. Genuine Australian stock, competitive pricing, "
              "fast nationwide delivery." % name)
        coll=Collection.objects.filter(name__iexact=name).first()
        if coll is None:
            coll=Collection.objects.filter(slug=slugify(name)[:255]).first()
        if coll is None:
            coll=Collection.objects.create(
                name=name[:250], slug=slugify(name)[:255],
                seo_title=name[:70], seo_description=desc[:300],
                description=ed(desc))
            made+=1
        u={}
        if not coll.seo_title: u["seo_title"]=name[:70]
        if not coll.seo_description: u["seo_description"]=desc[:300]
        if not coll.description: u["description"]=ed(desc)
        if u: Collection.objects.filter(id=coll.id).update(**u)
        CollectionChannelListing.objects.update_or_create(
            collection=coll, channel=CH,
            defaults={"is_published":True,"published_at":datetime.now(timezone.utc)})
        objs=[CollectionProduct(collection_id=coll.id, product_id=i) for i in ids]
        for i in range(0,len(objs),5000):
            CollectionProduct.objects.bulk_create(objs[i:i+5000], ignore_conflicts=True)
        linked+=len(ids)
    print("  new collections=%d link_attempts=%d total_collections=%d total_links=%d"
          % (made,linked,Collection.objects.count(),CollectionProduct.objects.count()), flush=True)

sec("9. DELETE ORPHAN SHELLS")
orph=list(Product.objects.filter(variants__isnull=True).values_list("id",flat=True))
print("orphans:",len(orph), flush=True); refs={}
if orph:
    ph=",".join(str(int(i)) for i in orph)
    with connection.cursor() as c:
        for t,col in [("discount_voucher_products","product_id"),
                      ("shipping_shippingmethod_excluded_products","product_id"),
                      ("giftcard_giftcard","product_id"),
                      ("attribute_attributevalue","reference_product_id"),
                      ("attribute_assignedproductattributevalue","product_id"),
                      ("product_productmedia","product_id"),
                      ("product_producttranslation","product_id"),
                      ("product_collectionproduct","product_id"),
                      ("product_productchannellisting","product_id")]:
            c.execute("SELECT COUNT(*) FROM %s WHERE %s IN (%s)" % (t,col,ph))
            refs[t]=c.fetchone()[0]
    print("  FK refs:",refs, flush=True)
if APPLY and orph and sum(refs.values())==0:
    save("orphans",orph)
    print("  deleted:",Product.objects.filter(id__in=orph).delete(), flush=True)
elif orph and sum(refs.values())>0:
    print("  SKIPPED - references exist")

sec("10. VARIANT sort_order")
with connection.cursor() as c:
    c.execute("SELECT COUNT(*) FROM product_productvariant WHERE sort_order IS NULL")
    n=c.fetchone()[0]
print("[%s] NULL sort_order=%d" % (mode(),n), flush=True)
if APPLY and n:
    with connection.cursor() as c:
        c.execute("""UPDATE product_productvariant v SET sort_order=s.rn
                     FROM (SELECT id, ROW_NUMBER() OVER (PARTITION BY product_id ORDER BY id)-1 AS rn
                           FROM product_productvariant WHERE sort_order IS NULL) s
                     WHERE v.id=s.id""")
    print("  ordered", flush=True)

sec("11. MARK DIRTY + CORE REBUILD")
print("[%s] commands run only with --run-commands" % mode(), flush=True)
if APPLY:
    Product.objects.update(search_index_dirty=True)
    ProductChannelListing.objects.filter(channel=CH).update(discounted_price_dirty=True)
    if A.run_commands:
        for cmd in ("update_all_products_discounted_prices","update_search_indexes"):
            print("  running:",cmd, flush=True)
            t0=datetime.now(timezone.utc)
            call_command(cmd, verbosity=1)
            print("  %s done %.1fs" % (cmd,(datetime.now(timezone.utc)-t0).total_seconds()), flush=True)
    else:
        print("  marked dirty; re-run with --run-commands to rebuild")

sec("12. VERIFICATION")
tp=Product.objects.count()
print("products=%d variants=%d" % (tp,ProductVariant.objects.count()))
print("orphans=%d" % Product.objects.filter(variants__isnull=True).count())
print("missing category=%d tax=%d seo=%d"
      % (Product.objects.filter(category__isnull=True).count(),
         Product.objects.filter(tax_class__isnull=True).count(),
         Product.objects.filter(Q(seo_title__isnull=True)|Q(seo_title="")).count()))
print("metadata non-empty=%d manufacturer assignments=%d"
      % (Product.objects.exclude(metadata={}).count(),
         AssignedProductAttributeValue.objects.filter(value__attribute__slug="manufacturer").count()))
print("NULL sort_order=%d" % ProductVariant.objects.filter(sort_order__isnull=True).count())
print("collections=%d links=%d products_in_collections=%d"
      % (Collection.objects.count(),CollectionProduct.objects.count(),
         CollectionProduct.objects.values("product_id").distinct().count()))
print("product listings w/ discounted price=%d / %d"
      % (ProductChannelListing.objects.filter(channel=CH).exclude(discounted_price_amount=None).count(),
         ProductChannelListing.objects.filter(channel=CH).count()))
print("search_vector populated=%d dirty=%d"
      % (Product.objects.exclude(search_vector=None).count(),
         Product.objects.filter(search_index_dirty=True).count()))
print("\nCATEGORY DISTRIBUTION (all):")
for c in Category.objects.annotate(n=Count("products")).order_by("-n","name"):
    par=c.parent.name if c.parent else "-"
    print("  [%s] %-28s parent=%-24s products=%d" % (c.id,c.name[:28],par[:24],c.n))
print("\nCOLLECTION DISTRIBUTION (top 30):")
for c in Collection.objects.annotate(n=Count("products")).order_by("-n")[:30]:
    print("  [%s] %-34s products=%d" % (c.id,c.name[:34],c.n))
with connection.cursor() as c:
    c.execute("SELECT COUNT(*) FROM product_category WHERE lft>=rght OR tree_id=0")
    print("\nMPTT anomalies:",c.fetchone()[0])
print("\nMODE:",mode(),"| backups:",BK if APPLY else "(none)")
