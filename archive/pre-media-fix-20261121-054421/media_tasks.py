import os
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "saleor.settings")

from saleor.product.models import Product
from .media import attach_placeholder

def process_product_media_sync(product_id: int):
    try:
        product = Product.objects.get(id=product_id)
    except Product.DoesNotExist:
        return "Product not found"

    existing_media = product.media.first()
    if existing_media:
        prov = (existing_media.metadata or {}).get("image_provenance", {})
        if prov.get("status") == "resolved" and prov.get("confidence", 0) >= 0.8:
            return "Already has verified image"

    # TODO: Tier 1 (sku_images.csv) / Tier 2 (ICECAT) lookup goes here before placeholder
    success = attach_placeholder(product)
    return "Placeholder attached" if success else "Failed"
