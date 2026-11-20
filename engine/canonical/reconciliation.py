"""Build a complete, uniform payload for every row."""
import logging
from imports.importer.engine.canonical.identity import MatchResult

logger = logging.getLogger(__name__)


def _slug(name, brand):
    # Keep existing scheme so re-runs reconcile instead of creating duplicates.
    base = f"{brand}-{name}" if brand else name
    return base.lower().replace(" ", "-")[:250]


def reconcile(normalized_row, match_result, existing_variant, existing_product,
              default_channel_id, default_warehouse_id):
    n = normalized_row
    p = {
        "supplier": n.supplier,
        "supplier_sku": n.supplier_sku,
        "cost_ex_gst": n.cost_ex_gst,
        "selling_price_inc_gst": n.selling_price_inc_gst,
        "supplier_quantity": n.supplier_quantity,
        "channel_id": default_channel_id,
        "warehouse_id": default_warehouse_id,
        "sku": n.sku,
        "variant_data": {
            "sku": n.sku,
            "name": n.product_name[:255],
            "weight": n.weight_kg,
        },
        "product_data": {
            "name": n.product_name[:250],
            "slug": _slug(n.product_name, n.brand),
            "description": n.description or n.product_name,
            "brand": n.brand,
            "primary_category": getattr(n, "category_level_1", "") or "",
            "secondary_category": getattr(n, "category_level_2", "") or "",
            "tertiary_category": getattr(n, "category_level_3", "") or "",
            "source_product_type": getattr(n, "source_product_type", "") or "",
        },
        "errors": [],
    }

    if match_result == MatchResult.EXACT:
        p["action"] = "UPDATE_VARIANT_AND_OFFER"
        p["variant_id"] = existing_variant.id
        p["product_id"] = existing_product.id
    elif match_result == MatchResult.FUZZY:
        p["action"] = "CREATE_VARIANT_AND_OFFER"
        p["product_id"] = existing_product.id
    else:
        p["action"] = "CREATE_PRODUCT_VARIANT_AND_OFFER"

    return p
