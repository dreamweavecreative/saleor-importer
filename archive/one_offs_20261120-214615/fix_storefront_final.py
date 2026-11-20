#!/usr/bin/env python3
"""Storefront finalisation: menu, availability policy, quarantine, variant names.
Importer-only. Dry-run default; --apply writes with JSON backup."""
import os, sys, json, argparse
from decimal import Decimal
from datetime import datetime, timezone
sys.path.insert(0, "/app"); sys.path.insert(0, "/tmp")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "saleor.settings")
import django; django.setup()
from django.db.models import Count, Q, F
from saleor.channel.models import Channel
from saleor.product.models import (Product, ProductVariant, Category,
    ProductVariantChannelListing)
from saleor.menu.models import Menu, MenuItem
from saleor.warehouse.models import Stock

ap = argparse.ArgumentParser(); ap.add_argument("--apply", action="store_true")
A = ap.parse_args(); APPLY = A.apply
CH = Channel.objects.filter(is_active=True).first()
SENT_P, SENT_C = Decimal("9999999"), Decimal("9999")
BK = "/tmp/caag-storefront-fix-%s.json" % datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
out = {}

def sec(t): print("\n" + "="*80 + "\n" + t + "\n" + "="*80)

sec("1. NAVBAR MENU <- CATEGORY TREE")
navbar = Menu.objects.filter(slug="navbar").first()
pub_roots = []
for r in Category.objects.filter(parent__isnull=True):
    desc = Category.objects.filter(Q(id=r.id) | Q(parent=r) | Q(parent__parent=r))
    n = Product.objects.filter(category__in=desc,
        channel_listings__channel=CH, channel_listings__is_published=True).distinct().count()
    if n: pub_roots.append((r, n))
pub_roots.sort(key=lambda x: -x[1])
existing = set(MenuItem.objects.filter(menu=navbar, category__isnull=False)
               .values_list("category_id", flat=True))
plan_menu = [(r.id, r.name, n) for r, n in pub_roots if r.id not in existing]
print("  navbar exists:", bool(navbar), "| publishable roots:", len(pub_roots),
      "| already linked:", len(existing), "| to add:", len(plan_menu))
for pid, name, n in plan_menu: print("    + %-26s %d products" % (name, n))
out["menu_add"] = plan_menu

sec("2. AVAILABILITY POLICY (supplier-backed -> available-to-order)")
stocked = Stock.objects.filter(quantity__gt=0).values_list("product_variant_id", flat=True)
flip = list(ProductVariant.objects.filter(track_inventory=True)
            .exclude(id__in=stocked).values_list("id", flat=True))
print("  tracked variants with ZERO stock -> track_inventory=False:", len(flip))
out["variants_untracked"] = flip

sec("3. PRICE QUARANTINE")
q = ProductVariantChannelListing.objects.filter(channel=CH).filter(
    Q(price_amount__lte=0) | Q(price_amount__gte=SENT_P) |
    Q(cost_price_amount__gte=SENT_C) | Q(cost_price_amount__gt=F("price_amount")))
qids = [(l.id, l.variant.sku, str(l.price_amount), str(l.cost_price_amount))
        for l in q.select_related("variant")]
print("  unsafe listings to quarantine:", len(qids))
for row in qids[:10]: print("    ", row)
out["quarantine"] = qids

sec("4. DUPLICATE VARIANT DISPLAY NAMES")
dups = (ProductVariant.objects.values("product_id", "name")
        .annotate(n=Count("id")).filter(n__gt=1))
dup_products = sorted({d["product_id"] for d in dups})
rename = []
for pid in dup_products:
    for v in ProductVariant.objects.filter(product_id=pid).order_by("id"):
        new = ("%s — SKU %s" % (v.name.split(" — SKU ")[0], v.sku))[:255]
        rename.append((v.id, v.sku, new))
print("  products affected:", len(dup_products), "| variants to rename:", len(rename))
for row in rename[:6]: print("    ", row)
out["variant_renames"] = rename

sec("APPLY" if APPLY else "DRY-RUN")
if not APPLY:
    print("  re-run with --apply to write. Backup will be:", BK)
    raise SystemExit(0)
with open(BK, "w") as fh: json.dump(out, fh, indent=2, default=str)
print("  backup:", BK)

if navbar and plan_menu:
    for i, (cid, name, n) in enumerate(plan_menu):
        MenuItem.objects.create(menu=navbar, name=name[:128],
                                category_id=cid, sort_order=100 + i)
    print("  menu items created:", len(plan_menu))
if flip:
    for i in range(0, len(flip), 2000):
        ProductVariant.objects.filter(id__in=flip[i:i+2000]).update(track_inventory=False)
    print("  variants untracked:", len(flip))
if qids:
    ProductVariantChannelListing.objects.filter(
        id__in=[r[0] for r in qids]).update(is_available_for_purchase=False)
    print("  quarantined:", len(qids))
if rename:
    for i in range(0, len(rename), 1000):
        for vid, sku, new in rename[i:i+1000]:
            ProductVariant.objects.filter(id=vid).update(name=new)
    print("  variants renamed:", len(rename))

sec("POST-CHECK")
print("  menu items (navbar):", MenuItem.objects.filter(menu=navbar).count(),
      "| category-linked:", MenuItem.objects.filter(menu=navbar, category__isnull=False).count())
print("  tracked w/ zero stock:", ProductVariant.objects.filter(track_inventory=True)
      .exclude(id__in=Stock.objects.filter(quantity__gt=0)
      .values_list("product_variant_id", flat=True)).count())
print("  quarantined listings:", ProductVariantChannelListing.objects.filter(
      channel=CH, is_available_for_purchase=False).count())
print("  duplicate-name variant groups:", ProductVariant.objects.values("product_id", "name")
      .annotate(n=Count("id")).filter(n__gt=1).count())
