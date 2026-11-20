"""Saleor product integration."""

# after the existing ProductType and Product model
# requirements have been audited.

def find_product_by_sku(sku):
    from saleor.product.models import ProductVariant

    return ProductVariant.objects.filter(
        sku=sku
    ).select_related("product").first()
