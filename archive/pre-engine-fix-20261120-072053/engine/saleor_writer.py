"""Write canonical product + variant + price + stock + supplier offers. No schema changes."""
import logging
from collections import defaultdict
from decimal import Decimal

from django.db import transaction
from django.utils import timezone
from saleor.product.models import (
    Product, ProductVariant, ProductChannelListing,
    ProductVariantChannelListing, Collection, CollectionProduct,
)
from saleor.channel.models import Channel
from saleor.warehouse.models import Stock
from saleor.tax.models import TaxClass
from imports.importer.engine.category_resolver import resolve_category

logger = logging.getLogger(__name__)


def editorjs(desc):
    if isinstance(desc, dict):
        return desc, str(desc.get("blocks", ""))[:5000]
    text = (desc or "").strip()
    if not text:
        return None, ""
    return {"blocks": [{"type": "paragraph", "data": {"text": text}}]}, text


def seo_title(name, brand):
    t = f"{brand} {name}" if brand and brand.lower() not in name.lower() else name
    return t[:70]


def seo_desc(name, plain):
    base = plain if len(plain or "") > 40 else f"Buy {name} online at CAA Global."
    return (base or "")[:300]


def _tax_class_id(product_type_id):
    # Reuse existing config; never invent tax rates.
    from saleor.product.models import ProductType
    pt = ProductType.objects.filter(id=product_type_id).first()
    if pt and pt.tax_class_id:
        return pt.tax_class_id
    tc = TaxClass.objects.first()
    return tc.id if tc else None


def write_batch(payloads, default_product_type_id, default_channel_id, default_warehouse_id):
    if not payloads:
        return {}

    channel = Channel.objects.get(id=default_channel_id)
    currency = channel.currency_code
    tax_class_id = _tax_class_id(default_product_type_id)
    now = timezone.now()
    stats = defaultdict(int)

    with transaction.atomic():
        # ---- 1. create missing products (full fields) ----
        new_products, seen_slug = [], set()
        for p in payloads:
            if p["action"] != "CREATE_PRODUCT_VARIANT_AND_OFFER":
                continue
            pd = p["product_data"]
            if pd["slug"] in seen_slug:
                continue
            seen_slug.add(pd["slug"])
            desc, plain = editorjs(pd["description"])
            new_products.append(Product(
                name=pd["name"], slug=pd["slug"],
                description=desc, description_plaintext=plain,
                product_type_id=default_product_type_id,
                category_id=resolve_category(pd["primary_category"], pd["secondary_category"], pd["tertiary_category"]),
                seo_title=seo_title(pd["name"], pd["brand"]),
                seo_description=seo_desc(pd["name"], plain),
                tax_class_id=tax_class_id,
                weight=p["variant_data"].get("weight"),
                metadata={"brand": pd["brand"]},
                search_index_dirty=True,
            ))
        if new_products:
            Product.objects.bulk_create(new_products, ignore_conflicts=True)
            stats["products_created"] += len(new_products)

        slug_map = {p.slug: p.id for p in Product.objects.filter(
            slug__in=[p["product_data"]["slug"] for p in payloads])}
        for p in payloads:
            if not p.get("product_id"):
                p["product_id"] = slug_map.get(p["product_data"]["slug"])

        # ---- 2. upsert new variants ----
        to_upsert, upsert_slugs = [], set()
        for p in payloads:
            if p["action"] == "UPDATE_VARIANT_AND_OFFER" or not p.get("product_id"):
                continue
            sku = p["sku"]
            if not sku or sku in upsert_slugs:
                continue
            upsert_slugs.add(sku)
            to_upsert.append(ProductVariant(
                product_id=p["product_id"], sku=sku,
                name=p["variant_data"]["name"],
                weight=p["variant_data"].get("weight"),
                track_inventory=True, sort_order=0, is_preorder=False,
            ))
        if to_upsert:
            ProductVariant.objects.bulk_create(
                to_upsert, update_conflicts=True, unique_fields=["sku"],
                update_fields=["name", "weight", "track_inventory", "product_id", "updated_at"])
            stats["variants_upserted"] += len(to_upsert)

        # ---- 3. resolve every variant by SKU (uniform key) ----
        skus = [p["sku"] for p in payloads if p.get("sku")]
        variants = {v.sku: v for v in ProductVariant.objects.filter(sku__in=skus)}

        # ---- 4. aggregate per variant: offers, stock, price ----
        agg = {}
        for p in payloads:
            v = variants.get(p["sku"])
            if not v:
                stats["variant_unresolved"] += 1
                continue
            a = agg.setdefault(v.id, {"variant": v, "offers": {}, "qty": 0,
                                      "price": None, "cost": None, "brand": p["product_data"]["brand"]})
            a["offers"][p["supplier"]] = {
                "supplier_sku": p["supplier_sku"],
                "cost_ex_gst": str(p["cost_ex_gst"]) if p["cost_ex_gst"] is not None else None,
                "quantity": p["supplier_quantity"],
                "last_synced": now.isoformat(),
            }
            a["qty"] += int(p["supplier_quantity"] or 0)
            pr = p.get("selling_price_inc_gst")
            if pr is not None:
                a["price"] = pr if a["price"] is None else max(a["price"], pr)
            c = p.get("cost_ex_gst")
            if c is not None:
                a["cost"] = c if a["cost"] is None else min(a["cost"], c)

        # ---- 5. variant metadata (supplier offers) ----
        v_updates = []
        for a in agg.values():
            v = a["variant"]
            meta = dict(v.private_metadata or {})
            offers = dict(meta.get("supplier_offers") or {})
            offers.update(a["offers"])
            meta["supplier_offers"] = offers
            v.private_metadata = meta
            v_updates.append(v)
        if v_updates:
            ProductVariant.objects.bulk_update(v_updates, ["private_metadata", "updated_at"])

        # ---- 6. backfill existing products missing category / SEO / tax ----
        pids = {a["variant"].product_id for a in agg.values()}
        prod_updates = []
        for prod in Product.objects.filter(id__in=pids):
            changed = False
            sample = next((p for p in payloads if p.get("product_id") == prod.id), None)
            if prod.category_id is None and sample:
                pd = sample["product_data"]
                prod.category_id = resolve_category(pd["primary_category"], pd["secondary_category"], pd["tertiary_category"])
                changed = True
            if not prod.seo_title:
                prod.seo_title = seo_title(prod.name, (prod.metadata or {}).get("brand", ""))
                changed = True
            if not prod.seo_description:
                prod.seo_description = seo_desc(prod.name, prod.description_plaintext)
                changed = True
            if prod.tax_class_id is None and tax_class_id:
                prod.tax_class_id = tax_class_id
                changed = True
            if changed:
                prod.search_index_dirty = True
                prod_updates.append(prod)
        if prod_updates:
            Product.objects.bulk_update(
                prod_updates, ["category_id", "seo_title", "seo_description",
                               "tax_class_id", "search_index_dirty", "updated_at"])
            stats["products_backfilled"] += len(prod_updates)

        # ---- 7. variant channel listings (PRICE + COST) ----
        vcl = [ProductVariantChannelListing(
            variant_id=vid, channel_id=default_channel_id, currency=currency,
            price_amount=a["price"], cost_price_amount=a["cost"],
            discounted_price_amount=a["price"], is_available_for_purchase=True,
        ) for vid, a in agg.items() if a["price"] is not None]
        if vcl:
            ProductVariantChannelListing.objects.bulk_create(
                vcl, update_conflicts=True, unique_fields=["variant_id", "channel_id"],
                update_fields=["price_amount", "cost_price_amount",
                               "discounted_price_amount", "currency", "is_available_for_purchase"])
            stats["variant_listings"] += len(vcl)

        # ---- 8. product channel listings (publish only when priced) ----
        priced_products = {a["variant"].product_id for a in agg.values() if a["price"] is not None}
        pcl = [ProductChannelListing(
            product_id=pid, channel_id=default_channel_id, currency=currency,
            is_published=True, published_at=now, visible_in_listings=True,
            available_for_purchase_at=now, discounted_price_dirty=True,
        ) for pid in priced_products]
        if pcl:
            ProductChannelListing.objects.bulk_create(
                pcl, update_conflicts=True, unique_fields=["product_id", "channel_id"],
                update_fields=["is_published", "published_at", "visible_in_listings",
                               "available_for_purchase_at", "currency", "discounted_price_dirty"])
            stats["products_published"] += len(pcl)
        stats["products_unpriced"] += len({a["variant"].product_id for a in agg.values()} - priced_products)

        # ---- 9. stock (aggregated across suppliers) ----
        stocks = [Stock(product_variant_id=vid, warehouse_id=default_warehouse_id,
                        quantity=a["qty"], quantity_allocated=0)
                  for vid, a in agg.items()]
        if stocks:
            Stock.objects.bulk_create(
                stocks, update_conflicts=True,
                unique_fields=["product_variant_id", "warehouse_id"],
                update_fields=["quantity"])
            stats["stocks"] += len(stocks)

        # ---- 10. default variant where missing ----
        first_by_product = {}
        for a in agg.values():
            first_by_product.setdefault(a["variant"].product_id, a["variant"].id)
        dv = []
        for prod in Product.objects.filter(id__in=first_by_product, default_variant_id__isnull=True):
            vid = first_by_product[prod.id]
            if not Product.objects.filter(default_variant_id=vid).exists():
                prod.default_variant_id = vid
                dv.append(prod)
        if dv:
            Product.objects.bulk_update(dv, ["default_variant_id"])
            stats["default_variants_set"] += len(dv)

        # ---- 11. brand collections (existing only) ----
        try:
            from imports.importer.config.taxonomy import BRAND_COLLECTIONS
            wanted = {slug for (slug, _n) in BRAND_COLLECTIONS.values()}
            cols = {c.slug: c.id for c in Collection.objects.filter(slug__in=wanted)}
            links, seen = [], set()
            for a in agg.values():
                brand = (a["brand"] or "").upper()
                if brand in BRAND_COLLECTIONS:
                    slug = BRAND_COLLECTIONS[brand][0]
                    cid = cols.get(slug)
                    pid = a["variant"].product_id
                    if cid and (cid, pid) not in seen:
                        seen.add((cid, pid))
                        links.append(CollectionProduct(collection_id=cid, product_id=pid))
            if links:
                CollectionProduct.objects.bulk_create(links, ignore_conflicts=True)
                stats["collection_links"] += len(links)
        except Exception as e:
            logger.warning("collection assignment skipped: %s", e)

    logger.info("batch written: %s", dict(stats))
    return dict(stats)
