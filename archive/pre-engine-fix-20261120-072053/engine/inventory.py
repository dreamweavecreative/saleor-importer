"""Saleor 3.23 warehouse inventory integration.

Supplier availability and owned warehouse inventory are separate.
Supplier feeds must never modify owned inventory implicitly.
"""

from django.db import transaction

from saleor.warehouse.models import (
    Warehouse,
    ChannelWarehouse,
    Stock,
)


class InventoryIntegrationError(Exception):
    pass


def resolve_channel_warehouse(channel):
    """Resolve the warehouse explicitly assigned to a Saleor channel."""

    assignments = list(
        ChannelWarehouse.objects.filter(
            channel=channel
        ).select_related("warehouse").order_by("sort_order", "id")
    )

    if not assignments:
        raise InventoryIntegrationError(
            f"No warehouse assigned to channel: {channel.slug}"
        )

    # This importer requires one unambiguous primary warehouse.
    if len(assignments) != 1:
        raise InventoryIntegrationError(
            f"Expected one warehouse for {channel.slug}; "
            f"found {len(assignments)}"
        )

    return assignments[0].warehouse


def get_stock(variant, warehouse):
    """Return existing stock without creating or modifying anything."""

    return Stock.objects.filter(
        product_variant=variant,
        warehouse=warehouse,
    ).first()


def get_stock_snapshot(variant, warehouse):
    """Return a safe inventory snapshot."""

    stock = get_stock(variant, warehouse)

    if stock is None:
        return {
            "exists": False,
            "quantity": 0,
            "quantity_allocated": 0,
            "available_quantity": 0,
        }

    return {
        "exists": True,
        "quantity": stock.quantity,
        "quantity_allocated": stock.quantity_allocated,
        "available_quantity": (
            stock.quantity - stock.quantity_allocated
        ),
    }


@transaction.atomic
def set_owned_stock(variant, warehouse, quantity):
    """Set explicitly authorised physical warehouse inventory.

    This function must only be called with an owned-inventory
    quantity, never a supplier availability quantity.
    """

    if isinstance(quantity, bool) or not isinstance(quantity, int):
        raise ValueError("Inventory quantity must be an integer")

    if quantity < 0:
        raise ValueError("Inventory quantity cannot be negative")

    stock = (
        Stock.objects.select_for_update()
        .filter(
            product_variant=variant,
            warehouse=warehouse,
        )
        .first()
    )

    if stock is None:
        if quantity == 0:
            return None

        return Stock.objects.create(
            product_variant=variant,
            warehouse=warehouse,
            quantity=quantity,
            quantity_allocated=0,
        )

    if quantity < stock.quantity_allocated:
        raise InventoryIntegrationError(
            "Cannot set stock below its allocated quantity. "
            f"Allocated={stock.quantity_allocated}, "
            f"requested={quantity}"
        )

    stock.quantity = quantity

    stock.save(update_fields=["quantity"])

    return stock


def validate_inventory_environment(channel):
    """Validate the production channel/warehouse relationship."""

    warehouse = resolve_channel_warehouse(channel)

    return {
        "warehouse": warehouse,
        "warehouse_id": str(warehouse.pk),
        "warehouse_slug": warehouse.slug,
        "channel_slug": channel.slug,
        "existing_stock_records": Stock.objects.filter(
            warehouse=warehouse
        ).count(),
    }
