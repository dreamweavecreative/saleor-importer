"""Resolve supplier category fields to canonical Saleor leaf category IDs."""
import logging
from saleor.product.models import Category
from imports.importer.config import taxonomy

logger = logging.getLogger(__name__)


def _ensure_category(slug):
    existing = Category.objects.filter(slug=slug).first()
    if existing:
        return existing

    entry = taxonomy.TREE_BY_SLUG.get(slug)
    if not entry:
        logger.warning("Unknown canonical category slug: %s", slug)
        return None

    name, parent_slug, seo_title, seo_desc = entry
    parent = _ensure_category(parent_slug) if parent_slug else None

    return Category.objects.create(
        name=name,
        slug=slug,
        parent=parent,
        seo_title=seo_title[:70],
        seo_description=seo_desc[:300],
        description_plaintext=name,
    )


def resolve_category(primary, secondary=None, tertiary=None,
                     vendor=None, description=None):
    """Return canonical leaf category ID, or None."""
    leaf = taxonomy.leaf_slug_for_primary(primary)

    # Accessory override for notebook bags/cases.
    if (primary or "").strip().upper() == "NOTEBOOKS" and taxonomy.is_accessory(description):
        leaf = "bags-and-cases"

    node = _ensure_category(leaf)
    return node.id if node else None
