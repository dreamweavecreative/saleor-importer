"""Map supplier categories onto the EXISTING canonical tree. No per-supplier category spam."""
import logging
from django.utils.text import slugify
from saleor.product.models import Category

logger = logging.getLogger(__name__)

# DickerData PrimaryCategory -> canonical slug (extend as discovery reveals more)
PRIMARY_MAP = {
    "NOTEBOOKS": "laptops", "LAPTOPS": "laptops", "DESKTOPS": "computers",
    "WORKSTATIONS": "computers", "TABLETS": "computers",
    "ACCESSORIES": "accessories", "BAGS & CASES": "bags-and-cases",
    "BAGS AND CASES": "bags-and-cases",
}
FALLBACK_SLUGS = ("general", "other", "uncategorised", "accessories")
_cache = {}
_fallback_id = None


def _fallback():
    global _fallback_id
    if _fallback_id is not None:
        return _fallback_id
    for slug in FALLBACK_SLUGS:
        c = Category.objects.filter(slug=slug).first()
        if c:
            _fallback_id = c.id
            return _fallback_id
    c = Category.objects.create(name="Other", slug="other",
                                description_plaintext="Other products")
    logger.info("Created fallback category 'Other' id=%s", c.id)
    _fallback_id = c.id
    return _fallback_id


def resolve_category(primary_cat="", secondary_cat="", tertiary_cat=""):
    key = (primary_cat or "").strip().upper()
    if key in _cache:
        return _cache[key]

    slug = PRIMARY_MAP.get(key)
    if not slug and key:
        # try an existing category whose slug matches the supplier term
        cand = slugify(key)[:50]
        if Category.objects.filter(slug=cand).exists():
            slug = cand

    cat_id = None
    if slug:
        c = Category.objects.filter(slug=slug).first()
        cat_id = c.id if c else None
    if cat_id is None:
        cat_id = _fallback()

    _cache[key] = cat_id
    return cat_id
