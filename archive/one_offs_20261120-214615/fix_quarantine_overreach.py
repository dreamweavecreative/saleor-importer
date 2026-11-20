#!/usr/bin/env python3
"""Re-enable listings quarantined by the over-broad cost>=9999 rule.
Keeps quarantined: price<=0, price>=9999999, cost==9999 (exact sentinel), cost>price.
Importer-only. Dry-run default; --apply writes with JSON backup."""
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

vl = ProductVariantChannelListing.objects.filter(channel=CH)
keep = vl.filter(Q(price_amount__lte=0) | Q(price_amount__gte=SENT_P) |
                 Q(cost_price_amount=SENT_C) |
                 Q(cost_price_amount__gt=F("price_amount")))
reenable = list(vl.filter(is_available_for_purchase=False)
                  .exclude(id__in=keep.values_list("id", flat=True))
                  .values_list("id", "variant__sku", "price_amount", "cost_price_amount"))

print("currently unavailable :", vl.filter(is_available_for_purchase=False).count())
print("legitimately quarantined (keep):", keep.count())
print("over-quarantined (re-enable)  :", len(reenable))
for row in reenable[:10]:
    print("   ", row)

if not A.apply:
    print("\nDRY-RUN — re-run with --apply"); raise SystemExit(0)

bk = "/tmp/caag-quarantine-overreach-%s.json" % datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
json.dump([{"id": r[0], "sku": r[1], "price": str(r[2]), "cost": str(r[3])} for r in reenable],
          open(bk, "w"), indent=2)
print("backup:", bk)
ids = [r[0] for r in reenable]
for i in range(0, len(ids), 2000):
    ProductVariantChannelListing.objects.filter(id__in=ids[i:i+2000]).update(is_available_for_purchase=True)
print("re-enabled:", len(ids))
print("post-check unavailable:", vl.filter(is_available_for_purchase=False).count(),
      "| keep-set:", keep.count())
