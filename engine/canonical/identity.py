"""Deterministic identity resolution.

No substring/fuzzy product matching.
Priority:
1. Existing variant SKU.
2. Existing canonical product slug using the importer's stable slug scheme.
3. New product.
"""
from typing import Optional, Tuple
import logging
from saleor.product.models import ProductVariant, Product

logger = logging.getLogger(__name__)


class MatchResult:
    EXACT = "EXACT"
    FUZZY = "FUZZY"
    NONE = "NONE"


def _slug(name, brand):
    base = f"{brand}-{name}" if brand else name
    return base.lower().replace(" ", "-")[:250]


def resolve_identity(normalized_row) -> Tuple[str, Optional[ProductVariant], Optional[Product]]:
    sku = (normalized_row.sku or "").strip()
    if sku:
        variant = ProductVariant.objects.filter(sku__iexact=sku).select_related("product").first()
        if variant:
            return MatchResult.EXACT, variant, variant.product

    slug = _slug(normalized_row.product_name, normalized_row.brand)
    product = Product.objects.filter(slug=slug).first()
    if product:
        # Deterministic product-level match; new variant will be attached.
        return MatchResult.FUZZY, None, product

    return MatchResult.NONE, None, None
