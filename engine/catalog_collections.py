"""Canonical collection ensure/assign helpers. Idempotent."""
from django.utils import timezone
from saleor.product.models import Collection, CollectionChannelListing, CollectionProduct
from saleor.channel.models import Channel
from imports.importer.config import taxonomy


def _editorjs(text):
    return {"blocks": [{"type": "paragraph", "data": {"text": text}}]}


def ensure_collections(channel=None, dry_run=True):
    if channel is None:
        channel = Channel.objects.filter(slug="default-channel").first()
    if channel is None:
        channel = Channel.objects.filter(is_active=True).first()
    if channel is None:
        raise RuntimeError("No active channel found for collections")

    report = {"created": [], "existing": [], "listings": []}

    for slug, name in taxonomy.all_collections():
        col = Collection.objects.filter(slug=slug).first()
        if col is None:
            report["created"].append(slug)
            if dry_run:
                continue
            col = Collection.objects.create(
                name=name,
                slug=slug,
                seo_title=name[:70],
                seo_description=f"Shop {name} at CAA Global. Genuine Australian stock."[:300],
                description=_editorjs(f"Shop {name} at CAA Global."),
            )
        else:
            report["existing"].append(slug)

        if not dry_run:
            _, created = CollectionChannelListing.objects.get_or_create(
                collection=col,
                channel=channel,
                defaults={"is_published": True, "published_at": timezone.now()},
            )
            if created:
                report["listings"].append(slug)

    return report


def assign_product_collections(product, row):
    slugs = [s for s, _ in taxonomy.collections_for(
        row.get("Vendor"),
        row.get("Type"),
        row.get("StockDescription"),
        primary_category=row.get("PrimaryCategory"),
        secondary_category=row.get("SecondaryCategory"),
        tertiary_category=row.get("TertiaryCategory"),
    )]
    product.collections.add(*Collection.objects.filter(slug__in=slugs))
