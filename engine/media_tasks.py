"""Media enrichment: accuracy-first sourcing, two-tier card fallback."""
import logging

logger = logging.getLogger(__name__)

DIGITAL_TYPE_SLUG = "digital-services"
PRIMARY_KIND = {"SOFTWARE": "licence", "WARRANTY": "warranty", "SERVICES": "service"}


def _is_digital(product):
    try:
        return (product.product_type.slug or "") == DIGITAL_TYPE_SLUG
    except Exception:
        return False


def card_kind(product):
    if not _is_digital(product):
        return "pending"
    primary = ((product.metadata or {}).get("supplier_category") or {})
    return PRIMARY_KIND.get(str(primary.get("primary", "")).strip().upper(), "licence")


def _has_genuine_media(product):
    from .media import is_placeholder_media
    return any(not is_placeholder_media(media) for media in product.media.all())


def process_product_media_sync(product_id, force=False, allow_search=True):
    from saleor.product.models import Product
    from . import image_identity as ident
    from . import image_sources as sources
    from . import image_lookup as lookup
    from .media import attach_image
    from .product_cards import generate_card

    try:
        product = Product.objects.get(id=product_id)
    except Product.DoesNotExist:
        return "missing"
    if not force and _has_genuine_media(product):
        return "skipped_has_media"
    variant = product.variants.first()
    if variant is None:
        return "no_variant"

    identity = ident.product_identity(product, variant)

    svc, svc_reason = ident.is_service_sku(
        product.name, identity["mpn"], identity.get("primary_category"))
    if svc:
        kind = "warranty" if "WARRANTY" in svc_reason.upper() or True else "service"
        kind = "service"
        data, ext, sha = generate_card(identity["brand"], product.name,
                                       variant.sku or str(product.id), kind)
        prov = {"source_tier": "card:" + kind, "source_url": None,
                "brand": identity["brand"], "sku": variant.sku,
                "mpn": identity["mpn"], "confidence": 0.0,
                "evidence": svc_reason, "license": "generated",
                "card_kind": kind}
        if attach_image(product, data, ext, sha, prov, status="branded_card"):
            return "card:" + kind
        return "failed"

    if not _is_digital(product):
        cands, _ = sources.candidates(identity, allow_search=allow_search,
                                      keys=sources.provider_keys())
        for c in cands[:3]:
            try:
                raw, _final = lookup.fetch(c["fetch_url"])
                data, ext, w, h, sha = lookup.validate_normalize(raw)
            except Exception as exc:
                logger.info("candidate rejected pid=%s: %s", product_id, exc)
                continue
            prov = {"source_tier": c["tier"], "source_url": c["source_url"],
                    "source_page": c.get("source_page"),
                    "brand": identity["brand"], "sku": variant.sku,
                    "mpn": identity["mpn"], "matched_mpn": c.get("matched_mpn"),
                    "mpn_derivation": c.get("mpn_derivation"),
                    "confidence": c["confidence"], "evidence": c.get("reason"),
                    "source_title": (c.get("title") or "")[:300],
                    "license": ("icecat_partner" if c["tier"] == "icecat"
                                else "third_party_unverified"),
                    "width": w, "height": h}
            if attach_image(product, data, ext, sha, prov):
                return "resolved:" + c["tier"]

    kind = card_kind(product)
    data, ext, sha = generate_card(identity["brand"], product.name,
                                   variant.sku or str(product.id), kind)
    prov = {"source_tier": "card:" + kind, "source_url": None,
            "brand": identity["brand"], "sku": variant.sku,
            "mpn": identity["mpn"], "confidence": 0.0,
            "evidence": "no_verified_source" if kind == "pending" else "non_physical",
            "license": "generated", "card_kind": kind}
    status = "branded_card" if kind != "pending" else "placeholder"
    if attach_image(product, data, ext, sha, prov, status=status):
        return "card:" + kind
    return "failed"
