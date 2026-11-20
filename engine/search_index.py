"""Search indexing via Saleor's proven search_vector pipeline.

search_document is vestigial for products in Saleor 3.23 (declaration + GIN
index only; no application writer or reader). We deliberately do NOT populate it.
"""
import logging

logger = logging.getLogger(__name__)
BATCH = 500


def reindex_dirty_products(batch=BATCH, dry_run=False):
    from saleor.product.models import Product

    ids = list(Product.objects.filter(search_index_dirty=True)
               .order_by("id").values_list("id", flat=True))
    if dry_run or not ids:
        return {"dirty": len(ids), "reindexed": 0}

    from saleor.product.search import update_products_search_vector
    done = 0
    for i in range(0, len(ids), batch):
        update_products_search_vector(ids[i:i + batch])   # clears dirty itself
        done += len(ids[i:i + batch])
    remaining = Product.objects.filter(search_index_dirty=True).count()
    logger.info("[search] reindexed %s/%s, remaining dirty %s", done, len(ids), remaining)
    return {"dirty": len(ids), "reindexed": done, "remaining": remaining}


def get_search_stats():
    from saleor.product.models import Product
    total = Product.objects.count()
    populated = Product.objects.exclude(search_vector=None).count()
    dirty = Product.objects.filter(search_index_dirty=True).count()
    return {"total": total, "vector_populated": populated,
            "missing": total - populated, "dirty": dirty}
