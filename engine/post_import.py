"""Post-import reconciliation for the CAAG daily pipeline. Idempotent."""
import logging
from decimal import Decimal
from django.db.models import Q, F

logger = logging.getLogger(__name__)

UNLIMITED_QUANTITY_THRESHOLD = 999999999
SENTINEL_PRICE = Decimal("9999999")
SENTINEL_COST = Decimal("9999")
NAVBAR_SLUG = "navbar"


def fix_sentinel_stock():
    from saleor.warehouse.models import Stock
    from saleor.product.models import ProductVariant
    qs = Stock.objects.filter(quantity__gte=UNLIMITED_QUANTITY_THRESHOLD,
                              quantity_allocated=0)
    found = qs.count()
    if not found:
        return {"found": 0, "fixed": 0}
    vids = list(qs.values_list("product_variant_id", flat=True))
    ProductVariant.objects.filter(id__in=vids).update(track_inventory=False)
    return {"found": found, "fixed": qs.update(quantity=0)}


def fix_variant_sort_order():
    from django.db import connection
    with connection.cursor() as c:
        c.execute("SELECT COUNT(*) FROM product_productvariant WHERE sort_order IS NULL")
        found = c.fetchone()[0]
        if found:
            c.execute("""UPDATE product_productvariant v SET sort_order = s.rn
                         FROM (SELECT id, ROW_NUMBER() OVER
                               (PARTITION BY product_id ORDER BY id) - 1 AS rn
                               FROM product_productvariant WHERE sort_order IS NULL) s
                         WHERE v.id = s.id""")
    return {"found": found, "fixed": found}


def fix_fabricated_weights():
    from saleor.product.models import Product
    found = Product.objects.filter(weight=0.5).count()
    return {"found": found,
            "fixed": Product.objects.filter(weight=0.5).update(weight=None) if found else 0}


def cleanup_orphan_products():
    from django.db import connection
    from saleor.product.models import Product
    orphans = list(Product.objects.filter(variants__isnull=True).values_list("id", flat=True))
    if not orphans:
        return {"found": 0, "deleted": 0}
    tables = [("discount_voucher_products", "product_id"),
              ("shipping_shippingmethod_excluded_products", "product_id"),
              ("giftcard_giftcard", "product_id"),
              ("attribute_attributevalue", "reference_product_id"),
              ("attribute_assignedproductattributevalue", "product_id"),
              ("product_productmedia", "product_id"),
              ("product_producttranslation", "product_id"),
              ("product_collectionproduct", "product_id"),
              ("product_productchannellisting", "product_id")]
    safe = []
    with connection.cursor() as c:
        for pid in orphans:
            hit = False
            for t, col in tables:
                c.execute("SELECT 1 FROM %s WHERE %s = %%s LIMIT 1" % (t, col), [pid])
                if c.fetchone():
                    hit = True
                    break
            if not hit:
                safe.append(pid)
    deleted = Product.objects.filter(id__in=safe).delete()[0] if safe else 0
    return {"found": len(orphans), "deleted": deleted}


def enforce_listing_policy():
    from django.utils import timezone
    from saleor.channel.models import Channel
    from saleor.product.models import ProductChannelListing, ProductVariantChannelListing
    from saleor.warehouse.models import Stock
    ch = Channel.objects.filter(is_active=True).first()
    if ch is None:
        return {"channel": None}
    stocked = Stock.objects.filter(quantity__gt=0).values_list("product_variant_id", flat=True)
    safe = (Q(price_amount__gt=0) & Q(price_amount__lt=SENTINEL_PRICE) &
            (Q(cost_price_amount__isnull=True) |
             (~Q(cost_price_amount=SENTINEL_COST) &
              ~Q(cost_price_amount__gt=F("price_amount")))))
    buyable = safe & (Q(variant__track_inventory=False) | Q(variant_id__in=stocked))
    vl = ProductVariantChannelListing.objects.filter(channel=ch)
    t = vl.filter(buyable, is_available_for_purchase=False); n_t = t.count()
    if n_t: t.update(is_available_for_purchase=True)
    f = vl.filter(~buyable, is_available_for_purchase=True); n_f = f.count()
    if n_f: f.update(is_available_for_purchase=False)
    pids = vl.filter(safe).values_list("variant__product_id", flat=True)
    pub = ProductChannelListing.objects.filter(channel=ch, product_id__in=pids).filter(
        Q(is_published=False) | Q(visible_in_listings=False))
    n_p = pub.count()
    if n_p:
        pub.update(is_published=True, visible_in_listings=True,
                   available_for_purchase_at=timezone.now())
    return {"avail_enabled": n_t, "avail_disabled": n_f, "published": n_p}


def sync_navbar_menu(menu_slug=NAVBAR_SLUG):
    from saleor.channel.models import Channel
    from saleor.menu.models import Menu, MenuItem
    from saleor.product.models import Category, Product
    ch = Channel.objects.filter(is_active=True).first()
    menu = Menu.objects.filter(slug=menu_slug).first()
    if ch is None or menu is None:
        return {"menu": menu_slug, "present": bool(menu)}
    linked = set(MenuItem.objects.filter(menu=menu, category__isnull=False)
                 .values_list("category_id", flat=True))
    roots = added = 0
    for r in Category.objects.filter(parent__isnull=True).order_by("name"):
        sub = Category.objects.filter(Q(id=r.id) | Q(parent=r) | Q(parent__parent=r))
        if not Product.objects.filter(category__in=sub,
                                      channel_listings__channel=ch,
                                      channel_listings__is_published=True).exists():
            continue
        roots += 1
        if r.id in linked:
            continue
        MenuItem.objects.create(menu=menu, name=r.name[:128], category=r,
                                sort_order=100 + added)
        added += 1
    return {"menu": menu_slug, "roots_with_published": roots, "added": added}


def dedupe_variant_names():
    from django.db.models import Count
    from saleor.product.models import ProductVariant
    dups = (ProductVariant.objects.values("product_id", "name")
            .annotate(n=Count("id")).filter(n__gt=1))
    groups = dups.count(); renamed = 0
    for d in dups:
        for v in ProductVariant.objects.filter(product_id=d["product_id"],
                                               name=d["name"]).order_by("id"):
            new = ("%s — SKU %s" % (v.name.split(" — SKU ")[0], v.sku))[:255]
            ProductVariant.objects.filter(id=v.id).update(name=new)
            renamed += 1
    return {"groups": groups, "renamed": renamed}


def rebuild_search_index():
    from .search_index import reindex_dirty_products
    return reindex_dirty_products()


def recalc_discounted_prices():
    from django.core.management import call_command
    call_command("update_all_products_discounted_prices", verbosity=0)
    return {"status": "completed"}


def dispatch_media_tasks_for_products(product_ids):
    from saleor.product.models import Product
    from .media_tasks import process_product_media_sync
    stats = {"considered": 0, "skipped_has_media": 0, "attempted": 0,
             "placeholder": 0, "verified": 0, "missing": 0, "failed": 0}
    seen = set()
    for pid in product_ids or []:
        if pid in seen:
            continue
        seen.add(pid)
        stats["considered"] += 1
        try:
            product = Product.objects.get(id=pid)
        except Product.DoesNotExist:
            stats["missing"] += 1
            continue
        media = list(product.media.all())
        if media:
            statuses = [(m.metadata or {}).get("image_provenance", {}).get("status")
                        for m in media]
            if any(s != "placeholder" for s in statuses):
                stats["skipped_has_media"] += 1
                continue
        stats["attempted"] += 1
        try:
            r = process_product_media_sync(pid)
        except Exception:
            logger.exception("media dispatch failed pid=%s", pid)
            stats["failed"] += 1
            continue
        if r == "Placeholder attached":
            stats["placeholder"] += 1
        elif r == "Already has verified image":
            stats["verified"] += 1
    return stats


def run(channel_id=None, warehouse_id=None, product_ids=None, reindex=True, discount=True):
    results = {
        "sentinel_stock": fix_sentinel_stock(),
        "sort_order": fix_variant_sort_order(),
        "weights": fix_fabricated_weights(),
        "orphans": cleanup_orphan_products(),
        "listing_policy": enforce_listing_policy(),
        "menu": sync_navbar_menu(),
        "variant_names": dedupe_variant_names(),
    }
    if product_ids:
        results["media"] = dispatch_media_tasks_for_products(product_ids)
    if reindex:
        results["search_index"] = rebuild_search_index()
    if discount:
        results["discounted_prices"] = recalc_discounted_prices()
    logger.info("post-import: %s", results)
    return results


def caag_run_post_import_fixes(*args, **kwargs):
    return run(*args, **kwargs)
