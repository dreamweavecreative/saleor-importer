"""Canonical collections (V5 Phase 2). Idempotent."""
from django.utils import timezone
from saleor.product.models import Collection, CollectionChannelListing
from saleor.channel.models import Channel
from importer.config import taxonomy

def ensure_collections(dry_run=True):
    channel = Channel.objects.get(slug="default-channel")
    wanted = (list(taxonomy.BRAND_COLLECTIONS.values())
              + [taxonomy.BUNDLE_COLLECTION, taxonomy.GENERAL_COLLECTION,
                 taxonomy.ACCESSORY_COLLECTION])
    report = {"created": [], "existing": [], "listings": []}
    for slug, name in wanted:
        col = Collection.objects.filter(slug=slug).first()
        if col is None:
            report["created"].append(slug)
            if dry_run: continue
            col = Collection.objects.create(name=name, slug=slug)
        else:
            report["existing"].append(slug)
        if not dry_run:
            _, created = CollectionChannelListing.objects.get_or_create(
                collection=col, channel=channel,
                defaults={"is_published": True, "published_at": timezone.now()})
            if created: report["listings"].append(slug)
    return report

def assign_product_collections(product, row):
    slugs = [s for s, _ in taxonomy.collections_for(
        row.get("Vendor"), row.get("Type"), row.get("StockDescription"))]
    product.collections.add(*Collection.objects.filter(slug__in=slugs))
