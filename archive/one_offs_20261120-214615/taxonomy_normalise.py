#!/usr/bin/env python3
"""Normalise category depth + canonicalise brand collections. Importer-only.
Dry-run prints full plan; --apply executes with backups + FK guards."""
import os, sys, csv, json, argparse, collections
from datetime import datetime, timezone
sys.path.insert(0, "/app")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "saleor.settings")
import django; django.setup()
from django.db import connection, transaction
from django.db.models import Count, Q
from django.utils.text import slugify
from saleor.channel.models import Channel
from saleor.product.models import (Product, ProductVariant, Category, Collection,
    CollectionProduct, CollectionChannelListing)

ap = argparse.ArgumentParser()
ap.add_argument("--apply", action="store_true")
ap.add_argument("--csv", default="/tmp/imports/incoming/DickerData.csv")
A = ap.parse_args(); APPLY = A.apply
TS = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
BK = "/tmp/taxonomy-%s" % TS
CH = Channel.objects.filter(is_active=True).first()

BRAND = {"MICROSOFT":"Microsoft","CISCO":"Cisco","HPE":"HPE","HP":"HP","LENOVO":"Lenovo",
 "DELL":"Dell","DYNABOOK":"Dynabook","APC":"APC","LG":"LG","HID":"HID",
 "TREND MICR":"Trend Micro","POWERSHIEL":"PowerShield","COOLERMAST":"Cooler Master",
 "CONNECTWIS":"ConnectWise","CHECKPOINT":"Check Point","TPLINK":"TP-Link","DLINK":"D-Link",
 "STARTECH":"StarTech.com","EXTREME":"Extreme Networks","TUNGSTEN":"Tungsten Automation",
 "AUDIOCODES":"AudioCodes","WATCHGUARD":"WatchGuard","KENSINGTON":"Kensington",
 "YEALINK":"Yealink","SONICWALL":"SonicWall","JABRA":"Jabra","EPSON":"Epson","ADOBE":"Adobe",
 "EATON":"Eaton","SHURE":"Shure","SAMSUNG":"Samsung","COMMBOX":"CommBox","LOGITECH":"Logitech",
 "HIKVISION":"Hikvision","NEAT":"Neat","NVIDIA":"NVIDIA","UBIQUITI":"Ubiquiti","KYOCERA":"Kyocera",
 "CERTECH":"Certech","RAYTEC":"Raytec","QNAP":"QNAP","AVEPOINT":"AvePoint","ASUS":"ASUS",
 "BAMBU":"Bambu Lab","TARGUS":"Targus","ATDEC":"ATDEC","INSTA360":"Insta360","SHARP":"Sharp",
 "REYEE":"Reyee","RUCKUS":"Ruckus","BRIEFCAM":"BriefCam","BELKIN":"Belkin","BARCO":"Barco",
 "H3C":"H3C","ATEN":"ATEN","IDEMIA":"IDEMIA","BTICINO":"BTicino","ACCELTEX":"Acceltex",
 "NETGEAR":"NETGEAR","HUNTRESS":"Huntress","HUDDLY":"Huddly","ADATA":"ADATA","SEAGATE":"Seagate",
 "ASUSTOR":"ASUSTOR","PHILIPS":"Philips","NEDAP":"Nedap","TRUSTWAVE":"Trustwave",
 "COMSOL":"COMSOL","LUMIFY":"Lumify","POLY":"Poly"}
REVIEW = {"INTERACTIV","OPENTEXTCS","SMART","DD","PSS","STM","SWITCH","THIRDPARTY","JACKFUSE"}
MERGE = {"Microsoft Surface":"Microsoft","Lenovo Laptops":"Lenovo","Dell Laptops":"Dell",
         "Dynabook Laptops":"Dynabook","HP Laptops":"HP"}
KEEP_AS_IS = {"Laptops","Laptop Bundles","Bags & Cases"}
LEAF_OVERRIDE = {"HARDWARE":"Hardware","SECURITY":"Security","SERVICES":"Services",
 "WARRANTY":"Warranty","OPTION":"Options","OPTIONS":"Options","ACCESSORIES":"Accessories",
 "MANAGED SWITCH":"Managed Switches","CABLES":"Cables","NCE CORPORATE":"Microsoft NCE Corporate",
 "NCE ACADEMIC":"Microsoft NCE Academic","OPEN VALUE":"Microsoft Open Value",
 "OPEN VALUE GOVT":"Microsoft Open Value Government","OV ACADEMIC":"Microsoft Open Value Academic",
 "OV CHARITY":"Microsoft Open Value Charity","RESERVED INSTANCES":"Reserved Instances",
 "SPARES & ACCESSORIES":"Spares & Accessories"}
INCH = ["13-Inch Laptops","14-Inch Laptops","15-Inch Laptops","16-Inch Laptops","17-Inch Laptops"]

def sec(t): print("\n"+"="*84+"\n"+t+"\n"+"="*84, flush=True)
def save(n,o):
    os.makedirs(BK,exist_ok=True); os.chmod(BK,0o700)
    p=os.path.join(BK,n+".json"); open(p,"w").write(json.dumps(o,default=str,indent=2))
    os.chmod(p,0o600); print("  backup:",p,flush=True)
def ed(t): return {"blocks":[{"type":"paragraph","data":{"text":t}}]}

sec("0. LOAD FEED")
FEED={}
with open(A.csv,newline="",encoding="utf-8-sig") as fh:
    for r in csv.DictReader(fh):
        s=(r.get("StockCode") or "").strip()
        if s: FEED[s]={"v":(r.get("Vendor") or "").strip(),
                       "s":(r.get("SecondaryCategory") or "").strip().upper()}
P2SKU={}
for pid,sku in ProductVariant.objects.values_list("product_id","sku").order_by("product_id","id"):
    P2SKU.setdefault(pid,sku)
def fi(pid): return FEED.get(P2SKU.get(pid) or "",{})
print("feed rows:",len(FEED),"| mapped products:",len(P2SKU))

sec("1. PLAN — move root-held products to data-derived leaves")
plan=[]
for root in Category.objects.filter(parent__isnull=True).annotate(n=Count("products")).filter(n__gt=0):
    pids=[p.id for p in Product.objects.filter(category=root).only("id")]
    groups=collections.defaultdict(list)
    for pid in pids:
        s=fi(pid).get("s","")
        groups[LEAF_OVERRIDE.get(s, s.title() if s else "General")].append(pid)
    print(f"\nROOT [{root.id}] {root.name}  products={len(pids)}")
    for leaf,cnt in sorted(groups.items(), key=lambda x:-len(x[1])):
        print(f"   -> {root.name} / {leaf:<34} {len(cnt)}")
        plan.append((root,leaf,cnt))
if APPLY:
    save("category_plan",[{"root":r.id,"leaf":l,"ids":i} for r,l,i in plan])
    for root,leaf,pids in plan:
        node=Category.objects.filter(name__iexact=leaf,parent=root).first()
        if node is None:
            node=Category.objects.create(name=leaf[:250],parent=root,
                slug=slugify(f"{root.name}-{leaf}")[:255],description_plaintext=leaf,
                seo_title=f"{leaf} | {root.name}"[:70],
                seo_description=f"Buy {leaf.lower()} in {root.name.lower()} at CAA Global. Genuine Australian stock, fast nationwide delivery."[:300])
        for i in range(0,len(pids),5000):
            Product.objects.filter(id__in=pids[i:i+5000]).update(category=node,search_index_dirty=True)
    print("  moved:",sum(len(i) for _,_,i in plan))

sec("2. PLAN — collapse screen-size categories into Laptops (facet, not category)")
laptops=Category.objects.filter(name__iexact="Laptops").first()
inch=[c for c in Category.objects.filter(name__in=INCH)]
for c in inch:
    n=Product.objects.filter(category=c).count(); print(f"  [{c.id}] {c.name}: {n} -> Laptops")
if APPLY and laptops:
    ids=list(Product.objects.filter(category__in=inch).values_list("id",flat=True))
    if ids:
        save("inch_move",ids)
        Product.objects.filter(id__in=ids).update(category=laptops,search_index_dirty=True)
    print("  moved:",len(ids))

sec("3. Empty categories — FK-guarded removal")
empties=list(Category.objects.annotate(n=Count("products")).filter(n=0))
refs={}
for c in empties:
    with connection.cursor() as cur:
        tot=0
        for t,col in [("menu_menuitem","category_id"),("discount_voucher_categories","category_id"),
                      ("attribute_attributevalue","reference_category_id"),
                      ("thumbnail_thumbnail","category_id"),("product_category","parent_id")]:
            cur.execute(f"SELECT COUNT(*) FROM {t} WHERE {col}=%s",[c.id]); tot+=cur.fetchone()[0]
    refs[c.id]=tot
    print(f"  [{c.id}] {c.name:<26} refs={tot} {'DELETE' if tot==0 else 'KEEP (referenced)'}")
if APPLY:
    save("empty_category_refs",refs)
    for c in empties:
        if refs[c.id]==0:
            Category.objects.filter(id=c.id).delete()
    print("  removed:",sum(1 for v in refs.values() if v==0))

sec("4. Category SEO backfill")
if APPLY:
    n=0
    for c in Category.objects.all():
        u={}
        if not c.seo_title: u["seo_title"]=c.name[:70]
        if not c.seo_description:
            u["seo_description"]=f"Shop {c.name} at CAA Global. Genuine Australian stock, competitive pricing and fast nationwide delivery."[:300]
        if not c.description_plaintext: u["description_plaintext"]=c.name
        if u: Category.objects.filter(id=c.id).update(**u); n+=1
    print("  updated:",n)
else:
    print("  missing seo_title:",Category.objects.filter(Q(seo_title__isnull=True)|Q(seo_title="")).count())

sec("5. PLAN — canonical brand collections")
vend=collections.Counter(fi(pid).get("v","") for pid in P2SKU)
targets={}
for v,n in vend.items():
    if not v or n<25: continue
    if v in REVIEW: targets[v]=v; print(f"  REVIEW (no guess): {v:<12} rows={n}")
    else: targets[v]=BRAND.get(v,v.title())
print("\n  brand collections planned:",len(targets))
existing={c.name:c for c in Collection.objects.all()}
merges=[]
for legacy,canon in MERGE.items():
    if legacy in existing: merges.append((existing[legacy],canon))
for c in Collection.objects.filter(name__endswith=" Products"):
    raw=c.name[:-9]
    canon=targets.get(raw, BRAND.get(raw, raw.title()))
    if canon!=c.name: merges.append((c,canon))
print("\n  MERGES / RENAMES:")
for src,canon in merges: print(f"    [{src.id}] {src.name:<28} -> {canon}")
print("\n  KEPT unchanged:",sorted(KEEP_AS_IS))
print("  NEVER merged (distinct entities): HP/HPE, ASUS/ASUSTOR, Smart Home/SMART")
if APPLY:
    save("collection_merges",[{"src":s.id,"src_name":s.name,"target":t} for s,t in merges])
    for src,canon in merges:
        tgt=Collection.objects.filter(name__iexact=canon).first()
        if tgt is None:
            tgt=Collection.objects.create(name=canon[:250],slug=slugify(canon)[:255])
        if tgt.id==src.id: continue
        links=list(CollectionProduct.objects.filter(collection=src).values_list("product_id",flat=True))
        CollectionProduct.objects.bulk_create(
            [CollectionProduct(collection_id=tgt.id,product_id=p) for p in links],
            batch_size=5000,ignore_conflicts=True)
        with connection.cursor() as cur:
            tot=0
            for t,col in [("menu_menuitem","collection_id"),("discount_voucher_collections","collection_id"),
                          ("attribute_attributevalue","reference_collection_id"),
                          ("thumbnail_thumbnail","collection_id")]:
                cur.execute(f"SELECT COUNT(*) FROM {t} WHERE {col}=%s",[src.id]); tot+=cur.fetchone()[0]
        if tot==0: Collection.objects.filter(id=src.id).delete()
        else: print(f"    kept [{src.id}] {src.name} (refs={tot})")
    for raw,canon in targets.items():
        c=Collection.objects.filter(name__iexact=canon).first()
        if c is None:
            pids=[pid for pid in P2SKU if fi(pid).get("v")==raw]
            c=Collection.objects.create(name=canon[:250],slug=slugify(canon)[:255])
            CollectionProduct.objects.bulk_create(
                [CollectionProduct(collection_id=c.id,product_id=p) for p in pids],
                batch_size=5000,ignore_conflicts=True)
    n=0
    for c in Collection.objects.all():
        d=f"Shop {c.name} at CAA Global. Genuine Australian stock, competitive pricing and fast nationwide delivery."
        u={}
        if not c.seo_title: u["seo_title"]=c.name[:70]
        if not c.seo_description: u["seo_description"]=d[:300]
        if not c.description: u["description"]=ed(d)
        if u: Collection.objects.filter(id=c.id).update(**u); n+=1
        CollectionChannelListing.objects.update_or_create(collection=c,channel=CH,
            defaults={"is_published":True,"published_at":datetime.now(timezone.utc)})
    print("  SEO updated:",n)

sec("6. VERIFICATION")
print("categories:",Category.objects.count())
print("roots holding products:",Category.objects.filter(parent__isnull=True).annotate(n=Count("products")).filter(n__gt=0).count())
print("empty categories:",Category.objects.annotate(n=Count("products")).filter(n=0).count())
print("collections:",Collection.objects.count(),"| links:",CollectionProduct.objects.count())
print("products in collections:",CollectionProduct.objects.values("product_id").distinct().count())
print("collections missing SEO:",Collection.objects.filter(Q(seo_title__isnull=True)|Q(seo_title="")).count())
with connection.cursor() as cur:
    cur.execute("SELECT COUNT(*) FROM product_category WHERE lft>=rght OR tree_id=0")
    print("MPTT anomalies:",cur.fetchone()[0])
print("\nMODE:", "APPLY" if APPLY else "DRY-RUN", "| backups:", BK if APPLY else "(none)")
