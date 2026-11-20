from typing import Optional, Tuple
from saleor.product.models import ProductVariant, Product, Category, Collection
from django.db.models import Q
import logging

logger = logging.getLogger(__name__)

class MatchResult:
    EXACT = "EXACT"
    FUZZY = "FUZZY"
    NONE = "NONE"

def resolve_identity(normalized_row) -> Tuple[str, Optional[ProductVariant], Optional[Product]]:
    """
    Attempts to find an existing ProductVariant by SKU.
    If not found, attempts to find an existing Product by name+brand to attach a new variant.
    Returns: (MatchResult, existing_variant, existing_product)
    """
    # 1. Exact match on canonical SKU
    if normalized_row.sku:
        variant = ProductVariant.objects.filter(sku__iexact=normalized_row.sku).first()
        if variant:
            return MatchResult.EXACT, variant, variant.product

    # 2. Fuzzy match on Product Name + Brand (to avoid duplicate canonical products)
    # We use icontains for basic fuzzy matching. Production systems may use pg_trgm.
    product_qs = Product.objects.filter(name__icontains=normalized_row.product_name)
    if normalized_row.brand:
        # Assuming brand is stored in product metadata or name; checking name for simplicity
        product_qs = product_qs.filter(name__icontains=normalized_row.brand)
    
    product = product_qs.first()
    if product:
        return MatchResult.FUZZY, None, product

    return MatchResult.NONE, None, None
