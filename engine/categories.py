"""Canonical category management (V5 Phase 2). Global slug uniqueness."""
from django.db import IntegrityError, transaction
from saleor.product.models import Category
from importer.config import taxonomy

def _create_unique(name, slug, parent, seo_title=None, seo_desc=None):
    candidate = slug[:255]; counter = 0
    while counter < 10000:
        if not Category.objects.filter(slug=candidate).exists():
            try:
                with transaction.atomic():
                    return Category.objects.create(
                        name=name, slug=candidate, parent=parent,
                        seo_title=seo_title or "", seo_description=seo_desc or "")
            except IntegrityError:
                existing = Category.objects.filter(slug=candidate).first()
                if existing: return existing
        counter += 1
        suffix = f"-{counter}"
        candidate = slug[:255 - len(suffix)] + suffix
    raise RuntimeError("Cannot generate unique slug for " + name)

def ensure_canonical_tree(dry_run=True):
    report = {"created": [], "existing": [], "updated": []}
    by_slug = {}
    for slug, name, parent_slug, seo_t, seo_d in taxonomy.CANONICAL_TREE:
        parent = by_slug.get(parent_slug) if parent_slug else None
        if parent_slug and parent is None:
            parent = Category.objects.filter(slug=parent_slug).first()
        cat = Category.objects.filter(slug=slug).first()
        if cat is None:
            report["created"].append(slug)
            if not dry_run:
                cat = _create_unique(name, slug, parent, seo_t, seo_d)
        else:
            report["existing"].append(slug)
            if not dry_run:
                changed = []
                if cat.name != name: cat.name = name; changed.append("name")
                if parent is not None and cat.parent_id != parent.id:
                    cat.parent = parent; changed.append("parent")
                if changed: cat.save(); report["updated"].append(slug)
        by_slug[slug] = cat
    return report

def resolve_canonical_category(row):
    """Crosswalk only. Never creates supplier categories."""
    path, reason = taxonomy.map_category(
        row.get("PrimaryCategory"), row.get("SecondaryCategory"),
        row.get("TertiaryCategory"), row.get("Vendor"),
        row.get("StockDescription"))
    if not path: return None, reason
    cat = None
    for slug in path:
        cat = Category.objects.filter(slug=slug).first()
        if cat is None: return None, f"missing-canonical:{slug}"
    return cat, reason
