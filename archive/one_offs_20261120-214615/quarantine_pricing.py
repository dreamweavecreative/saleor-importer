import os, sys, json, argparse
from decimal import Decimal
from datetime import datetime, timezone
sys.path.insert(0, "/app")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "saleor.settings")
import django; django.setup()
from django.db import transaction
from django.db.models import Q, F
from saleor.channel.models import Channel
from saleor.product.models import ProductVariantChannelListing

ap = argparse.ArgumentParser()
ap.add_argument("--apply", action="store_true")
a = ap.parse_args()
CH = Channel.objects.filter(is_active=True).first()

qs = (ProductVariantChannelListing.objects.filter(channel=CH)
      .filter(Q(price_amount=0) | Q(cost_price_amount__gt=F("price_amount"))
              | Q(price_amount__gte=Decimal("9999999")))
      .select_related("variant__product"))
print("channel:", CH.slug, "| candidates:", qs.count())
rows = list(qs)
for l in rows[:25]:
    why = []
    if l.price_amount == 0: why.append("ZERO_PRICE")
    if l.cost_price_amount and l.cost_price_amount > l.price_amount: why.append("BELOW_COST")
    if l.price_amount >= Decimal("9999999"): why.append("SENTINEL_PRICE")
    print("  %-26s price=%-14s cost=%-12s avail=%s %s"
          % (l.variant.sku, l.price_amount, l.cost_price_amount,
             l.is_available_for_purchase, ",".join(why)))
targets = [l for l in rows if l.is_available_for_purchase]
print("currently available (will be quarantined):", len(targets))
if not a.apply:
    print("DRY RUN — re-run with --apply"); raise SystemExit(0)
if not targets:
    print("nothing to do"); raise SystemExit(0)
ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
bak = "/tmp/quarantine-backup-%s.json" % ts
with open(bak,"w") as fh:
    json.dump([{"id": l.id, "sku": l.variant.sku,
                "is_available_for_purchase": l.is_available_for_purchase} for l in targets], fh, indent=2)
print("backup:", bak)
with transaction.atomic():
    for l in targets: l.is_available_for_purchase = False
    ProductVariantChannelListing.objects.bulk_update(targets, ["is_available_for_purchase"], batch_size=500)
print("quarantined:", len(targets))
