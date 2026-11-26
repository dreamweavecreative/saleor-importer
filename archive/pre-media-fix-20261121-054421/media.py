from django.core.files.base import ContentFile
from saleor.product.models import ProductMedia
from .image_provenance import ImageProvenance
from .placeholder_image import generate_placeholder
from .image_verification import verify_image

def attach_verified_image(product, image_url: str, provenance: dict) -> bool:
    try:
        is_valid, tier, img_data, sha256 = verify_image(image_url, provenance.get("brand", ""), provenance.get("mpn", ""))
        if not is_valid: return False
        ext = image_url.split('.')[-1].split('?')[0] or 'jpg'
        ProductMedia.objects.filter(product=product).delete()
        media = ProductMedia.objects.create(product=product)
        media.image.save(f"product_{product.variants.first().sku or product.id}.{ext}", ContentFile(img_data), save=True)
        meta = product.metadata or {}
        provenance["sha256"] = sha256
        meta["image_provenance"] = provenance
        product.metadata = meta
        product.save(update_fields=["metadata"])
        return True
    except Exception:
        return False

def attach_placeholder(product) -> bool:
    try:
        sku = product.variants.first().sku if product.variants.exists() else str(product.id)
        brand = (product.metadata or {}).get("brand", "Unknown")
        mpn = (product.metadata or {}).get("mpn", "")
        img_data, fmt, sha256 = generate_placeholder(brand, sku, mpn)
        ProductMedia.objects.filter(product=product).delete()
        media = ProductMedia.objects.create(product=product)
        media.image.save(f"placeholder_{sku}.{fmt}", ContentFile(img_data), save=True)
        meta = product.metadata or {}
        meta["image_provenance"] = {"status": "placeholder", "source_tier": "fallback", "brand": brand, "sku": sku, "mpn": mpn, "confidence": 0.1, "license": "placeholder", "sha256": sha256}
        product.metadata = meta
        product.save(update_fields=["metadata"])
        return True
    except Exception:
        return False
