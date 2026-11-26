"""Build a complete, uniform payload for every row."""
import logging
from django.db import connection
from .identity import MatchResult, normalize_gtin, normalize_mpn, normalize_manufacturer

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



# ------------------------------------------------------------
# Canonical identifier ownership and reconciliation
# ------------------------------------------------------------

class IdentityConflict(ValueError):
    """Identifiers supplied for a product resolve to different owners."""


def resolve_identity(gtin=None, mpn=None, manufacturer=None):
    """
    Resolve a canonical product by exact GTIN or manufacturer-scoped MPN.

    GTINs use the empty namespace. MPNs are scoped by the normalised
    manufacturer name. No fuzzy or substring matching is performed.
    """
    gtin = normalize_gtin(gtin) if gtin else None
    mpn = normalize_mpn(mpn)
    manufacturer = normalize_manufacturer(manufacturer)

    if mpn and not manufacturer:
        raise ValueError("Manufacturer is required with MPN")

    matches = []

    if gtin:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT canonical_product_id
                FROM catalogue.product_identifiers
                WHERE identifier_type = 'GTIN'
                  AND namespace = ''
                  AND normalized_value = %s
                """,
                [gtin],
            )
            row = cursor.fetchone()
            if row:
                matches.append((row[0], "GTIN"))

    if mpn:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT canonical_product_id
                FROM catalogue.product_identifiers
                WHERE identifier_type = 'MPN'
                  AND namespace = %s
                  AND normalized_value = %s
                """,
                [manufacturer, mpn],
            )
            row = cursor.fetchone()
            if row:
                matches.append((row[0], "MPN"))

    if not matches:
        return None, "UNMATCHED"

    owners = {str(product_id) for product_id, _ in matches}

    if len(owners) > 1:
        raise IdentityConflict(
            "GTIN and MPN resolve to different canonical products"
        )

    # Prefer GTIN as the stronger identifier when both agree.
    for product_id, method in matches:
        if method == "GTIN":
            return product_id, "GTIN"

    return matches[0]


def register_identity(product_id, gtin=None, mpn=None, manufacturer=None):
    """
    Register every supplied identifier against one canonical product.

    CanonicalRepository enforces unique ownership in the database.
    Ownership conflicts are exposed consistently as IdentityConflict.
    """
    from .repository import CanonicalRepository

    gtin = normalize_gtin(gtin) if gtin else None
    mpn = normalize_mpn(mpn)
    manufacturer = normalize_manufacturer(manufacturer)

    if not gtin and not mpn:
        raise ValueError("At least one GTIN or MPN is required")

    if mpn and not manufacturer:
        raise ValueError("Manufacturer is required with MPN")

    try:
        if gtin:
            CanonicalRepository.add_identifier(
                product_id=product_id,
                identifier_type="GTIN",
                value=gtin,
                namespace="",
                source="importer",
            )

        if mpn:
            CanonicalRepository.add_identifier(
                product_id=product_id,
                identifier_type="MPN",
                value=mpn,
                namespace=manufacturer,
                source="importer",
            )

    except ValueError as exc:
        raise IdentityConflict(str(exc)) from exc

    return True
