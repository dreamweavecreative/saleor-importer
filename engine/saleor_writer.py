"""Write canonical product + variant + price + supplier offers.

Important inventory contract:
- Supplier availability is NOT owned warehouse inventory.
- Supplier quantities are stored in variant.private_metadata.supplier_offers.
- Owned Stock.quantity is not implicitly written from supplier feeds.
- Supplier-available variants are track_inventory=False (available-to-order).
"""
import logging
from collections import defaultdict
from decimal import Decimal

from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify

from saleor.product.models import (
    Product, ProductType, ProductVariant, ProductChannelListing,
    ProductVariantChannelListing, Collection, CollectionProduct,
)
from saleor.channel.models import Channel
from saleor.warehouse.models import Stock
from saleor.tax.models import TaxClass
from saleor.attribute.models import (
    Attribute, AttributeProduct, AttributeValue, AssignedProductAttributeValue,
)

from imports.importer.engine.category_resolver import resolve_category
from imports.importer.engine.catalog_enrichment import display_brand, parse_specs
from imports.importer.engine.catalog_collections import ensure_collections
from imports.importer.engine.pricing import caag_check_pricing_safety
from imports.importer.engine.products import caag_enrich_product
from imports.importer.engine.variants import caag_adjust_variant

from imports.importer.config import taxonomy

logger = logging.getLogger(__name__)

NONPHYSICAL_PRIMARIES = {"SOFTWARE", "WARRANTY", "SERVICES"}
UNLIMITED_QUANTITY_THRESHOLD = 999999999
SENTINEL_PRICE = Decimal("9999999")
SENTINEL_COST = Decimal("9999")


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
    pt = ProductType.objects.filter(id=product_type_id).first()
    if pt and pt.tax_class_id:
        return pt.tax_class_id
    tc = TaxClass.objects.first()
    return tc.id if tc else None


def _product_type_id(primary_category, default_product_type_id, tax_class_id):
    p = (primary_category or "").strip().upper()
    if p not in NONPHYSICAL_PRIMARIES:
        return default_product_type_id

    pt, _ = ProductType.objects.get_or_create(
        slug="digital-services",
        defaults={
            "name": "Digital & Services",
            "has_variants": True,
            "is_shipping_required": False,
            "is_digital": False,
            "kind": "NORMAL",
            "weight": 0.0,
            "tax_class_id": tax_class_id,
        },
    )
    return pt.id


def _weight_grams(weight_kg):
    if weight_kg is None:
        return None
    try:
        return float(weight_kg) * 1000.0
    except (TypeError, ValueError):
        return None


def _metadata_from_payload(pd):
    brand = display_brand(pd.get("brand"))
    description = pd.get("description") or pd.get("name") or ""
    meta = {
        "brand": brand,
        "supplier_category": {
            "primary": pd.get("primary_category", ""),
            "secondary": pd.get("secondary_category", ""),
            "tertiary": pd.get("tertiary_category", ""),
        },
        "source_product_type": pd.get("source_product_type", ""),
    }
    specs = parse_specs(description)
    if specs:
        meta["specs"] = specs
    return meta


def _safe_listing(price, cost):
    try:
        if not caag_check_pricing_safety(cost, price):
            return False
    except Exception:
        # Pricing safety is a hard gate. An unavailable or broken safety
        # check must never make a listing eligible.
        logger.exception("pricing safety check failed; listing rejected")
        return False
    if price is None:
        return False
    if price <= 0:
        return False
    if price >= SENTINEL_PRICE:
        return False
    if cost is not None:
        if cost == SENTINEL_COST:
            return False
        if cost > price:
            return False
    return True


def _ensure_manufacturer_attribute(product_type_ids, brands):
    attr, _ = Attribute.objects.get_or_create(
        slug="manufacturer",
        defaults={
            "name": "Manufacturer",
            "type": "product-type",
            "input_type": "dropdown",
            "value_required": False,
            "visible_in_storefront": True,
            "filterable_in_storefront": True,
            "filterable_in_dashboard": True,
            "available_in_grid": True,
            "storefront_search_position": 1,
            "is_variant_only": False,
        },
    )

    for pt_id in product_type_ids:
        if pt_id:
            AttributeProduct.objects.get_or_create(
                attribute=attr,
                product_type_id=pt_id,
                defaults={"sort_order": 0},
            )

    value_map = {}
    for i, brand in enumerate(sorted(brands)):
        if not brand:
            continue
        slug = slugify(brand)[:255]
        if not slug:
            continue
        value, _ = AttributeValue.objects.get_or_create(
            attribute=attr,
            slug=slug,
            defaults={"name": brand[:250], "value": brand[:255], "sort_order": i},
        )
        value_map[brand] = value.id

    return value_map


def write_batch(payloads, default_product_type_id, default_channel_id, default_warehouse_id):
    if not payloads:
        return {}

    channel = Channel.objects.get(id=default_channel_id)
    currency = channel.currency_code
    tax_class_id = _tax_class_id(default_product_type_id)
    now = timezone.now()
    stats = defaultdict(int)

    with transaction.atomic():
        try:
            ensure_collections(channel=channel, dry_run=False)
        except Exception as e:
            logger.warning("collection ensure skipped: %s", e)

        # ---- 1. create missing products ----
        new_products, seen_slug = [], set()
        for p in payloads:
            if p["action"] != "CREATE_PRODUCT_VARIANT_AND_OFFER":
                continue
            pd = p["product_data"]
            if pd["slug"] in seen_slug:
                continue
            seen_slug.add(pd["slug"])

            desc, plain = editorjs(pd["description"])
            pt_id = _product_type_id(pd.get("primary_category"), default_product_type_id, tax_class_id)

            # CAAG Enrichment Hook
            feed_map = {"PrimaryCategory": pd.get("primary_category", ""), "SecondaryCategory": pd.get("secondary_category", ""), "TertiaryCategory": pd.get("tertiary_category", ""), "Vendor": pd.get("brand", "")}
            pd = caag_enrich_product(pd, feed_map)

            new_products.append(Product(
                name=pd["name"],
                slug=pd["slug"],
                description=desc,
                description_plaintext=plain,
                product_type_id=pt_id,
                category_id=resolve_category(
                    pd.get("primary_category"),
                    pd.get("secondary_category"),
                    pd.get("tertiary_category"),
                ),
                seo_title=seo_title(pd["name"], display_brand(pd.get("brand"))),
                seo_description=seo_desc(pd["name"], plain),
                tax_class_id=tax_class_id,
                weight=_weight_grams(p["variant_data"].get("weight")),
                metadata=_metadata_from_payload(pd),
                search_index_dirty=True,
            ))

        if new_products:
            Product.objects.bulk_create(new_products, ignore_conflicts=True)
            stats["products_create_attempted"] += len(new_products)

        slug_map = {
            p.slug: p.id
            for p in Product.objects.filter(
                slug__in=[p["product_data"]["slug"] for p in payloads]
            )
        }
        for p in payloads:
            if not p.get("product_id"):
                p["product_id"] = slug_map.get(p["product_data"]["slug"])

        # ---- 2. validate global SKU ownership before variant writes ----
        # Saleor variant SKUs are globally unique. A feed row must never
        # silently attach an existing SKU to a different product.
        sku_product_map = {}
        for payload in payloads:
            sku = payload.get("sku")
            if not sku:
                continue

            intended_product_id = payload.get("product_id")
            if not intended_product_id:
                raise ValueError(
                    "SKU ownership validation failed: "
                    f"SKU {sku!r} has no resolved product_id"
                )

            previous_product_id = sku_product_map.get(sku)
            if (
                previous_product_id is not None
                and previous_product_id != intended_product_id
            ):
                raise ValueError(
                    "SKU ownership conflict inside import batch: "
                    f"SKU {sku!r} maps to product IDs "
                    f"{previous_product_id!r} and {intended_product_id!r}"
                )

            sku_product_map[sku] = intended_product_id

        existing_sku_owners = dict(
            ProductVariant.objects.filter(
                sku__in=list(sku_product_map)
            ).values_list("sku", "product_id")
        )

        for sku, intended_product_id in sku_product_map.items():
            existing_product_id = existing_sku_owners.get(sku)
            if (
                existing_product_id is not None
                and existing_product_id != intended_product_id
            ):
                raise ValueError(
                    "Existing Saleor SKU belongs to a different product; "
                    "refusing to re-parent variant: "
                    f"SKU={sku!r}, existing_product_id={existing_product_id!r}, "
                    f"intended_product_id={intended_product_id!r}"
                )

        # ---- 3. upsert new variants ----
        to_upsert, upsert_slugs = [], set()
        for p in payloads:
            if p["action"] == "UPDATE_VARIANT_AND_OFFER" or not p.get("product_id"):
                continue
            sku = p["sku"]
            if not sku or sku in upsert_slugs:
                continue
            upsert_slugs.add(sku)

            # CAAG Variant Adjustment Hook
            v_data = caag_adjust_variant(dict(p["variant_data"]), p.get("supplier_quantity", 0))
            
            to_upsert.append(ProductVariant(
                product_id=p["product_id"],
                sku=sku,
                name=v_data.get("name", p["variant_data"]["name"]),
                weight=_weight_grams(v_data.get("weight")),
                track_inventory=v_data.get("track_inventory", True),
                sort_order=v_data.get("sort_order", 0),
                is_preorder=False,
            ))

        if to_upsert:
            ProductVariant.objects.bulk_create(
                to_upsert,
                update_conflicts=True,
                unique_fields=["sku"],
                # product_id is deliberately excluded: ownership was
                # validated above and must never be changed by conflict upsert.
                update_fields=["name", "weight", "track_inventory", "updated_at"],
            )
            stats["variants_upsert_attempted"] += len(to_upsert)

        # ---- 3. resolve variants by SKU ----
        skus = [p["sku"] for p in payloads if p.get("sku")]
        variants = {v.sku: v for v in ProductVariant.objects.filter(sku__in=skus)}

        # ---- 4. aggregate supplier offers per variant ----
        agg = {}
        for p in payloads:
            v = variants.get(p["sku"])
            if not v:
                stats["variant_unresolved"] += 1
                continue

            a = agg.setdefault(v.id, {
                "variant": v,
                "offers": {},
                "any_supplier_available": False,
                "unlimited": False,
                "price": None,
                "cost": None,
                "brand": p["product_data"].get("brand", ""),
            })

            qty = int(p.get("supplier_quantity") or 0)
            a["offers"][p["supplier"]] = {
                "supplier_sku": p["supplier_sku"],
                "cost_ex_gst": str(p["cost_ex_gst"]) if p["cost_ex_gst"] is not None else None,
                "quantity": qty,
                "last_synced": now.isoformat(),
            }
            if qty > 0:
                a["any_supplier_available"] = True
            if qty >= UNLIMITED_QUANTITY_THRESHOLD:
                a["unlimited"] = True

            pr = p.get("selling_price_inc_gst")
            if pr is not None:
                a["price"] = pr if a["price"] is None else max(a["price"], pr)

            c = p.get("cost_ex_gst")
            if c is not None:
                a["cost"] = c if a["cost"] is None else min(a["cost"], c)

        # ---- 5. variant supplier metadata + availability mode ----
        v_updates = []
        for a in agg.values():
            v = a["variant"]
            meta = dict(v.private_metadata or {})
            offers = dict(meta.get("supplier_offers") or {})
            offers.update(a["offers"])
            meta["supplier_offers"] = offers
            meta["availability_mode"] = (
                "supplier_available_to_order"
                if a["any_supplier_available"]
                else "owned_stock_only"
            )
            v.private_metadata = meta

            # Supplier availability is not owned stock.
            # If supplier can fulfil, make variant available-to-order.
            # If no supplier availability, owned stock controls availability.
            v.track_inventory = not a["any_supplier_available"]
            
            # CAAG Variant Adjustment Hook (Update Phase)
            v_data = caag_adjust_variant({"track_inventory": v.track_inventory, "sort_order": getattr(v, "sort_order", 0)}, 999999999 if a.get("unlimited") else 0)
            v.track_inventory = v_data.get("track_inventory", v.track_inventory)
            if "sort_order" in v_data: v.sort_order = v_data["sort_order"]
            
            v_updates.append(v)

        if v_updates:
            ProductVariant.objects.bulk_update(
                v_updates, ["private_metadata", "track_inventory", "sort_order", "updated_at"]
            )

        # ---- 6. backfill existing products ----
        pids = {a["variant"].product_id for a in agg.values()}
        sample_by_product = {}
        for p in payloads:
            if p.get("product_id"):
                sample_by_product.setdefault(p["product_id"], p)

        prod_updates = []
        for prod in Product.objects.filter(id__in=pids):
            changed = False
            sample = sample_by_product.get(prod.id)

            if sample:
                pd = sample["product_data"]
                desired_pt = _product_type_id(
                    pd.get("primary_category"), default_product_type_id, tax_class_id
                )

                if prod.category_id is None:
                    prod.category_id = resolve_category(
                        pd.get("primary_category"),
                        pd.get("secondary_category"),
                        pd.get("tertiary_category"),
                    )
                    changed = True

                if (pd.get("primary_category") or "").strip().upper() in NONPHYSICAL_PRIMARIES:
                    if prod.product_type_id != desired_pt:
                        prod.product_type_id = desired_pt
                        changed = True

                desired_meta = _metadata_from_payload(pd)
                current_meta = dict(prod.metadata or {})
                for k, v in desired_meta.items():
                    if k not in current_meta:
                        current_meta[k] = v
                        changed = True
                prod.metadata = current_meta

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
                prod_updates,
                ["category_id", "product_type_id", "seo_title", "seo_description",
                 "tax_class_id", "metadata", "search_index_dirty", "updated_at"],
            )
            stats["products_backfill_attempted"] += len(prod_updates)

        # ---- 7. manufacturer attribute assignments ----
        type_ids = set(Product.objects.filter(id__in=pids).values_list("product_type_id", flat=True))
        brands = {display_brand(a["brand"]) for a in agg.values() if a["brand"]}
        value_map = _ensure_manufacturer_attribute(type_ids, brands)

        assignments = []
        seen_assign = set()
        for a in agg.values():
            brand = display_brand(a["brand"])
            value_id = value_map.get(brand)
            pid = a["variant"].product_id
            if value_id and (value_id, pid) not in seen_assign:
                seen_assign.add((value_id, pid))
                assignments.append(AssignedProductAttributeValue(
                    product_id=pid, value_id=value_id, sort_order=0
                ))

        # Manufacturer is a single-valued product identity attribute in
        # this importer. Do not add a second or conflicting value silently.
        expected_manufacturer_by_product = {}
        for assignment in assignments:
            pid = assignment.product_id
            value_id = assignment.value_id
            previous = expected_manufacturer_by_product.get(pid)
            if previous is not None and previous != value_id:
                raise ValueError(
                    "Conflicting manufacturer values in import batch for "
                    f"product_id={pid!r}: {previous!r} vs {value_id!r}"
                )
            expected_manufacturer_by_product[pid] = value_id

        if expected_manufacturer_by_product:
            manufacturer_attr = Attribute.objects.get(slug="manufacturer")
            existing_manufacturers = (
                AssignedProductAttributeValue.objects.filter(
                    product_id__in=list(expected_manufacturer_by_product),
                    value__attribute=manufacturer_attr,
                ).values_list("product_id", "value_id")
            )

            existing_by_product = defaultdict(set)
            for pid, value_id in existing_manufacturers:
                existing_by_product[pid].add(value_id)

            for pid, expected_value_id in expected_manufacturer_by_product.items():
                existing_values = existing_by_product.get(pid, set())
                if existing_values and existing_values != {expected_value_id}:
                    raise ValueError(
                        "Existing manufacturer assignment conflicts with "
                        "incoming manufacturer; refusing additive assignment: "
                        f"product_id={pid!r}, existing_values={sorted(existing_values)!r}, "
                        f"incoming_value_id={expected_value_id!r}"
                    )

        if assignments:
            AssignedProductAttributeValue.objects.bulk_create(assignments, ignore_conflicts=True)
            stats["manufacturer_assignments_attempted"] += len(assignments)

        # ---- 8. variant channel listings: price + cost + safety ----
        vcl = []
        for vid, a in agg.items():
            if a["price"] is None:
                continue
            safe = _safe_listing(a["price"], a["cost"])
            if not safe:
                stats["unsafe_variants"] += 1

            vcl.append(ProductVariantChannelListing(
                variant_id=vid,
                channel_id=default_channel_id,
                currency=currency,
                price_amount=a["price"],
                cost_price_amount=a["cost"],
                discounted_price_amount=a["price"],
                is_available_for_purchase=safe,
            ))

        if vcl:
            ProductVariantChannelListing.objects.bulk_create(
                vcl,
                update_conflicts=True,
                unique_fields=["variant_id", "channel_id"],
                update_fields=["price_amount", "cost_price_amount",
                               "discounted_price_amount", "currency",
                               "is_available_for_purchase"],
            )
            stats["variant_listings_upsert_attempted"] += len(vcl)

        # ---- 9. product channel listings ----
        priced_products = {
            a["variant"].product_id for a in agg.values() if a["price"] is not None
        }
        pcl = [
            ProductChannelListing(
                product_id=pid,
                channel_id=default_channel_id,
                currency=currency,
                is_published=True,
                published_at=now,
                visible_in_listings=True,
                available_for_purchase_at=now,
                discounted_price_dirty=True,
            )
            for pid in priced_products
        ]
        if pcl:
            ProductChannelListing.objects.bulk_create(
                pcl,
                update_conflicts=True,
                unique_fields=["product_id", "channel_id"],
                update_fields=["is_published", "published_at", "visible_in_listings",
                               "available_for_purchase_at", "currency",
                               "discounted_price_dirty"],
            )
            stats["products_publish_upsert_attempted"] += len(pcl)

        stats["products_unpriced"] += len(
            {a["variant"].product_id for a in agg.values()} - priced_products
        )

        # ---- 10. owned stock is NOT written from supplier availability ----
        # Supplier quantities live in private_metadata.supplier_offers.
        # Historic sentinel cleanup is handled by post_import.
        stats["owned_stock_writes"] += 0

        # ---- 11. default variant where missing ----
        first_by_product = {}
        for a in agg.values():
            first_by_product.setdefault(a["variant"].product_id, a["variant"].id)

        existing_default_ids = set(
            Product.objects.exclude(default_variant_id__isnull=True)
            .values_list("default_variant_id", flat=True)
        )
        dv = []
        for prod in Product.objects.filter(id__in=first_by_product, default_variant_id__isnull=True):
            vid = first_by_product[prod.id]
            if vid not in existing_default_ids:
                prod.default_variant_id = vid
                existing_default_ids.add(vid)
                dv.append(prod)

        if dv:
            Product.objects.bulk_update(dv, ["default_variant_id"])
            stats["default_variants_set_attempted"] += len(dv)

        # ---- 12. canonical collections ----
        wanted_slugs = {slug for slug, _ in taxonomy.all_collections()}
        cols = {c.slug: c.id for c in Collection.objects.filter(slug__in=wanted_slugs)}
        links, seen_links = [], set()

        for a in agg.values():
            pid = a["variant"].product_id
            sample = sample_by_product.get(pid)
            if not sample:
                continue
            pd = sample["product_data"]
            collection_items = taxonomy.collections_for(
                vendor=a["brand"],
                product_type=pd.get("source_product_type"),
                description=pd.get("description"),
                primary_category=pd.get("primary_category"),
                secondary_category=pd.get("secondary_category"),
                tertiary_category=pd.get("tertiary_category"),
            )
            for slug, _name in collection_items:
                cid = cols.get(slug)
                if cid and (cid, pid) not in seen_links:
                    seen_links.add((cid, pid))
                    links.append(CollectionProduct(collection_id=cid, product_id=pid))

        if links:
            CollectionProduct.objects.bulk_create(links, ignore_conflicts=True)
            stats["collection_links_attempted"] += len(links)

    logger.info("batch written: %s", dict(stats))
    return dict(stats)
