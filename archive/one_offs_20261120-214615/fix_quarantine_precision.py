#!/usr/bin/env python3
"""Restore availability for listings quarantined by the over-broad cost>=9999 rule.
Keep quarantine ONLY for: price<=0, price>=9999999, cost>price, cost==9999 (true sentinel)."""
import os, sys, json, argparse
from decimal import Decimal
from datetime import datetime, timezone
sys.path.insert(0, "/app"); sys.path.insert(0, "/tmp")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "saleor.settings")
import django; django.setup()
from django.db.models import Q, F
from saleor.channel.models import Channel
from saleor.product.models import ProductVariantChannelListing

ap = argparse.ArgumentParser(); ap.add_argument("--apply", action="store_true")
A = ap.parse_args()
CH = Channel.objects.filter(is_active=True).first()
SENT_P, SENT_C = Decimal("9999999"), Decimal("9999")

unsafe = (Q(price_amount__lte=0) | Q(price_amount__gte=SENT_P) |
          Q(cost_price_amount__gt=F("price_amount")) | Q(cost_price_amount=SENT_C))

currently_off = ProductVariantChannelListing.objects.filter(channel=CH, is_available_for_purchase=False)
restore = list(currently_off.filter(~unsafe).values_list("id", "variant__sku", "price_amount", "cost_price_amount"))
keep    = currently_off.filter(unsafe).count()
still_on_unsafe = ProductVariantChannelListing.objects.filter(channel=CH, is_available_for_purchase=True).filter(unsafe).count()

print("channel:", CH.slug)
print("  currently unavailable :", currently_off.count())
print("  FALSE POSITIVES to restore:", len(restore))
print("  genuinely unsafe (stay off):", keep)
print("  unsafe but still available :", still_on_unsafe)
for r in restore[:8]: print("   restore:", r)

if not A.apply:
    print("DRY-RUN — re-run with --apply"); raise SystemExit(0)

bk = "/tmp/caag-restore-%s.json" % datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
json.dump([{"id": r[0], "sku": r[1]} for r in restore], open(bk, "w"), indent=2)
print("backup:", bk)

ids = [r[0] for r in restore]
for i in range(0, len(ids), 2000):
    ProductVariantChannelListing.objects.filter(id__in=ids[i:i+2000]).update(is_available_for_purchase=True)
if still_on_unsafe:
    bad = list(ProductVariantChannelListing.objects.filter(channel=CH, is_available_for_purchase=True).filter(unsafe).values_list("id", flat=True))
    ProductVariantChannelListing.objects.filter(id__in=bad).update(is_available_for_purchase=False)
    print("newly quarantined:", len(bad))

from django.core.management import call_command
call_command("update_all_products_discounted_prices", verbosity=0)
print("restored:", len(ids))
print("now unavailable:", ProductVariantChannelListing.objects.filter(channel=CH, is_available_for_purchase=False).count())
print("product discounted NULL:", __import__("saleor.product.models", fromlist=["ProductChannelListing"]).ProductChannelListing.objects.filter(channel=CH, discounted_price_amount__isnull=True).count())
