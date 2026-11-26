"""Attach sourced or placeholder media. Provenance is PUBLIC-safe.

Icecat fetch URLs carry ?content_token=...; product.metadata is readable via
the storefront GraphQL API, so only sanitised URLs are ever persisted.
"""
import logging
from django.core.files.base import ContentFile

logger = logging.getLogger(__name__)

TIER_BY_SOURCE = {
    "index": "index",
    "cache": "cache",
    "icecat": "icecat",
    "serpapi": "search",
    "exa": "search",
    "scrappa": "scrappa",
    "google_cse": "search",
    "bing": "search",
}



def _identity(product):
    v = product.variants.first()
    sku = (v.sku if v else "") or str(product.id)
    md = product.metadata or {}
    return sku, md.get("brand", ""), md.get("mpn", "")


def is_placeholder_media(media) -> bool:
    """Recognise generated placeholders/cards using provenance or explicit filenames."""
    metadata = getattr(media, "metadata", None)
    provenance = metadata.get("image_provenance", {}) if isinstance(metadata, dict) else {}
    status = provenance.get("status") if isinstance(provenance, dict) else None

    if status in ("placeholder", "branded_card"):
        return True

    image = getattr(media, "image", None)
    filename = str(getattr(image, "name", "") or "")
    filename = filename.replace("\\\\", "/").rsplit("/", 1)[-1].lower()

    legacy_prefixes = (
        "placeholder-", "placeholder_",
        "branded_card-", "branded_card_",
        "branded-card-", "branded-card_",
    )
    return filename.startswith(legacy_prefixes)


def _drop_placeholders(product):
    for media in product.media.all():
        if is_placeholder_media(media):
            media.delete()


def attach_image(product, data, ext, sha, prov, status="resolved") -> bool:
    """Attach verified bytes. Never removes genuine media."""
    from saleor.product.models import ProductMedia
    try:
        for existing in product.media.all():
            ep = (existing.metadata or {}).get("image_provenance", {})
            if ep.get("sha256") == sha and ep.get("status") == status:
                return True
        _drop_placeholders(product)
        prov = dict(prov)
        prov.update({"status": status, "sha256": sha,
                     "pending_real_image": status == "placeholder"})
        m = ProductMedia(product=product, type="IMAGE",
                         alt=(product.name or prov.get("sku", ""))[:250],
                         sort_order=product.media.count())
        m.image.save(f"product_{product.id}.{ext}", ContentFile(data), save=False)
        m.metadata = {"image_provenance": prov}
        m.save()
        md = dict(product.metadata or {})
        md["image_provenance"] = prov
        product.metadata = md
        product.save(update_fields=["metadata"])
        return True
    except Exception:
        logger.exception("attach_image failed pid=%s", product.pk)
        return False


def attach_placeholder(product) -> bool:
    from saleor.product.models import ProductMedia
    from .placeholder_image import generate_placeholder
    try:
        sku, brand, mpn = _identity(product)
        data, ext, sha = generate_placeholder(brand, sku, mpn)
        prov = {"status": "placeholder", "source_tier": "fallback",
                "source_url": None, "brand": brand, "sku": sku, "mpn": mpn,
                "confidence": 0.0, "license": "placeholder",
                "sha256": sha, "pending_real_image": True}
        _drop_placeholders(product)
        m = ProductMedia(product=product, type="IMAGE",
                         alt=(product.name or sku)[:250],
                         sort_order=product.media.count())
        m.image.save(f"placeholder_{product.id}.{ext}", ContentFile(data), save=False)
        m.metadata = {"image_provenance": prov}
        m.save()
        md = dict(product.metadata or {})
        md["image_provenance"] = prov
        product.metadata = md
        product.save(update_fields=["metadata"])
        return True
    except Exception:
        logger.exception("attach_placeholder failed pid=%s", product.pk)
        return False
