"""Saleor variant integration with CAAG adjustments."""

UNLIMITED_QUANTITY_THRESHOLD = 999999999


def caag_adjust_variant(variant_data, stock_qty):
    """Apply CAAG variant adjustments. Supplier availability != owned stock."""
    if variant_data.get("sort_order") is None:
        variant_data["sort_order"] = 0

    try:
        qty = int(stock_qty) if stock_qty is not None else 0
    except (TypeError, ValueError):
        qty = 0

    if qty >= UNLIMITED_QUANTITY_THRESHOLD:
        variant_data["track_inventory"] = False
        variant_data["availability_mode"] = "supplier_available_to_order"
        variant_data["owned_stock_qty"] = 0
    elif qty > 0:
        variant_data["track_inventory"] = False
        variant_data["availability_mode"] = "supplier_available_to_order"
        variant_data["owned_stock_qty"] = 0
    else:
        variant_data["track_inventory"] = True
        variant_data["availability_mode"] = "owned_stock_only"
        variant_data["owned_stock_qty"] = 0

    return variant_data


def is_unlimited_quantity(qty):
    try:
        return int(qty) >= UNLIMITED_QUANTITY_THRESHOLD
    except (TypeError, ValueError):
        return False


def find_variant(sku):
    from .products import find_product_by_sku
    return find_product_by_sku(sku)
