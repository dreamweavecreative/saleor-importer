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



# ------------------------------------------------------------
# Canonical supplier identifier normalisation
# ------------------------------------------------------------

def normalize_gtin(value):
    """Validate a GTIN-8/12/13/14 and return its 14-digit form."""
    if value is None or isinstance(value, bool):
        return None

    text = str(value).strip()

    if not text:
        return None

    if not text.isascii() or not text.isdigit():
        raise ValueError("GTIN must contain digits only")

    if len(text) not in (8, 12, 13, 14):
        raise ValueError("GTIN must contain 8, 12, 13 or 14 digits")

    payload = text[:-1]
    supplied_check_digit = int(text[-1])

    weighted_sum = sum(
        int(digit) * (3 if position % 2 == 0 else 1)
        for position, digit in enumerate(reversed(payload))
    )
    expected_check_digit = (10 - weighted_sum % 10) % 10

    if supplied_check_digit != expected_check_digit:
        raise ValueError("Invalid GTIN check digit")

    return text.zfill(14)


def normalize_mpn(value):
    """Normalise an MPN without altering punctuation or leading zeroes."""
    if value is None:
        return None

    if isinstance(value, bool):
        raise ValueError("Invalid MPN")

    text = " ".join(str(value).strip().upper().split())
    return text or None


def normalize_manufacturer(value):
    """Normalise manufacturer names consistently for identifier namespaces."""
    if value is None:
        return None

    if isinstance(value, bool):
        raise ValueError("Invalid manufacturer")

    text = " ".join(str(value).strip().upper().split())
    return text or None
