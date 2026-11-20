"""Saleor inventory integration for CAAG.

KEY PRINCIPLE: Supplier availability is NOT owned warehouse inventory.
- Supplier quantities go into variant.private_metadata.supplier_offers
- Owned Stock.quantity only from actual warehouse counts
- Supplier-available variants use track_inventory=False
- Sentinel (999999999) must NEVER be written to Stock.quantity
"""
import logging
from django.db import transaction

logger = logging.getLogger(__name__)
UNLIMITED_QUANTITY_THRESHOLD = 999999999


class InventoryIntegrationError(Exception):
    pass


def resolve_channel_warehouse(channel):
    from saleor.warehouse.models import ChannelWarehouse
    assignments = list(
        ChannelWarehouse.objects.filter(channel=channel)
        .select_related("warehouse").order_by("sort_order", "id")
    )
    if not assignments:
        raise InventoryIntegrationError(f"No warehouse assigned to channel: {channel.slug}")
    if len(assignments) != 1:
        raise InventoryIntegrationError(f"Expected one warehouse for {channel.slug}; found {len(assignments)}")
    return assignments[0].warehouse


def get_stock(variant, warehouse):
    from saleor.warehouse.models import Stock
    return Stock.objects.filter(product_variant=variant, warehouse=warehouse).first()


def get_stock_snapshot(variant, warehouse):
    stock = get_stock(variant, warehouse)
    if stock is None:
        return {"exists": False, "quantity": 0, "quantity_allocated": 0, "available_quantity": 0}
    return {
        "exists": True, "quantity": stock.quantity,
        "quantity_allocated": stock.quantity_allocated,
        "available_quantity": stock.quantity - stock.quantity_allocated,
    }


@transaction.atomic
def set_owned_stock(variant, warehouse, quantity):
    """Set OWNED warehouse inventory. NEVER call with supplier availability."""
    from saleor.warehouse.models import Stock

    if isinstance(quantity, bool) or not isinstance(quantity, int):
        raise ValueError("Inventory quantity must be an integer")
    if quantity < 0:
        raise ValueError("Inventory quantity cannot be negative")
    if quantity >= UNLIMITED_QUANTITY_THRESHOLD:
        raise InventoryIntegrationError(
            f"Refusing to write sentinel quantity {quantity} to owned stock. "
            "Use track_inventory=False for supplier-available items."
        )

    stock = Stock.objects.select_for_update().filter(
        product_variant=variant, warehouse=warehouse
    ).first()

    if stock is None:
        if quantity == 0:
            return None
        return Stock.objects.create(
            product_variant=variant, warehouse=warehouse,
            quantity=quantity, quantity_allocated=0,
        )

    if quantity < stock.quantity_allocated:
        raise InventoryIntegrationError(
            f"Cannot set stock below allocated. Allocated={stock.quantity_allocated}, requested={quantity}"
        )
    stock.quantity = quantity
    stock.save(update_fields=["quantity"])
    return stock


def update_supplier_availability(variant, supplier, supplier_sku, quantity, cost=None, currency="AUD"):
    """Record supplier availability in private metadata. Does NOT modify Stock."""
    from django.utils import timezone

    meta = dict(variant.private_metadata or {})
    offers = dict(meta.get("supplier_offers") or {})
    offers[supplier] = {
        "supplier_sku": supplier_sku,
        "quantity": int(quantity) if quantity else 0,
        "cost_ex_gst": str(cost) if cost else None,
        "currency": currency,
        "last_synced": timezone.now().isoformat(),
    }
    meta["supplier_offers"] = offers

    qty = int(quantity) if quantity else 0
    if qty >= UNLIMITED_QUANTITY_THRESHOLD or qty > 0:
        variant.track_inventory = False
        meta["availability_mode"] = "supplier_available_to_order"
        action = "AVAILABLE_TO_ORDER"
    else:
        variant.track_inventory = True
        meta["availability_mode"] = "owned_stock_only"
        action = "OWNED_STOCK_ONLY"

    variant.private_metadata = meta
    return action


def validate_inventory_environment(channel):
    from saleor.warehouse.models import Stock
    warehouse = resolve_channel_warehouse(channel)
    return {
        "warehouse": warehouse, "warehouse_id": str(warehouse.pk),
        "warehouse_slug": warehouse.slug, "channel_slug": channel.slug,
        "existing_stock_records": Stock.objects.filter(warehouse=warehouse).count(),
    }
