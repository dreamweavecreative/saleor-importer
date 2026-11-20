"""
Universal supplier profile registry.

Supplier identity is independent of manufacturer/brand.
Profiles describe source-feed fields and map them to canonical
importer field names.

This module is additive: existing importer behaviour is unchanged
until readers, validators and the orchestrator are explicitly wired
to use the registry.
"""

from copy import deepcopy


DICKERDATA = {
    "supplier_id": "dickerdata",
    "supplier_name": "DickerData",
    "description": "DickerData distributor product feed",
    "default_currency": "AUD",
    "default_tax_included": False,

    # Supported source formats.
    "supported_formats": (".csv", ".xlsx", ".ods"),

    # Source column -> canonical field.
    "field_mapping": {
        "StockCode": "supplier_item_code",
        "Vendor": "manufacturer",
        "VendorStockCode": "manufacturer_part_number",
        "StockDescription": "product_name",

        "PrimaryCategory": "category_level_1",
        "SecondaryCategory": "category_level_2",
        "TertiaryCategory": "category_level_3",

        "RRPEx": "supplier_rrp_ex_gst",
        "DealerEx": "supplier_cost_ex_gst",

        "StockAvailable": "supplier_quantity",
        "ETA": "supplier_eta",
        "Status": "supplier_status",
        "Type": "supplier_product_type",

        "BundledItem1": "bundle_component_1",
        "BundledItem2": "bundle_component_2",
        "BundledItem3": "bundle_component_3",
        "BundledItem4": "bundle_component_4",
        "BundledItem5": "bundle_component_5",
    },

    # Required source columns for a usable supplier offer.
    "required_fields": (
        "StockCode",
        "StockDescription",
        "RRPEx",
        "DealerEx",
        "StockAvailable",
        "Status",
        "Type",
    ),

    # Optional source columns.
    "optional_fields": (
        "Vendor",
        "VendorStockCode",
        "PrimaryCategory",
        "SecondaryCategory",
        "TertiaryCategory",
        "ETA",
        "BundledItem1",
        "BundledItem2",
        "BundledItem3",
        "BundledItem4",
        "BundledItem5",
    ),

    "active_status_values": ("A",),
    "inactive_status_values": ("T",),

    # These are source values, not Saleor product types.
    "source_product_types": (
        "StockedItem",
        "BundledItem",
    ),

    # DickerData feed convention; interpretation will be handled
    # by the inventory normalisation stage.
    "unlimited_quantity_threshold": 999999999,
}


_PROFILES = {
    DICKERDATA["supplier_id"]: DICKERDATA,
}


def get_supplier_profile(supplier_id):
    """Return a defensive copy of a registered supplier profile."""
    key = str(supplier_id or "").strip().lower()

    if not key:
        raise ValueError("A supplier_id is required")

    try:
        return deepcopy(_PROFILES[key])
    except KeyError:
        raise ValueError(
            f"Unknown supplier profile: {supplier_id}"
        ) from None


def list_supplier_profiles():
    """Return registered supplier identifiers."""
    return tuple(sorted(_PROFILES))


def register_supplier_profile(profile):
    """
    Register a new supplier profile.

    Existing profiles cannot be silently overwritten.
    """
    if not isinstance(profile, dict):
        raise TypeError("Supplier profile must be a dictionary")

    required = {
        "supplier_id",
        "supplier_name",
        "field_mapping",
        "required_fields",
        "supported_formats",
    }

    missing = required - set(profile)
    if missing:
        raise ValueError(
            "Supplier profile missing keys: "
            + ", ".join(sorted(missing))
        )

    supplier_id = str(profile["supplier_id"]).strip().lower()

    if not supplier_id:
        raise ValueError("supplier_id cannot be empty")

    if supplier_id in _PROFILES:
        raise ValueError(
            f"Supplier profile already registered: {supplier_id}"
        )

    mapping = profile["field_mapping"]

    if not isinstance(mapping, dict) or not mapping:
        raise ValueError("field_mapping must be a non-empty dictionary")

    if len(set(mapping.values())) != len(mapping):
        raise ValueError(
            "Multiple source columns map to the same canonical field"
        )

    _PROFILES[supplier_id] = deepcopy(profile)
    _PROFILES[supplier_id]["supplier_id"] = supplier_id

    return supplier_id


def map_source_record(record, supplier_id):
    """Map source column names to canonical importer field names."""
    profile = get_supplier_profile(supplier_id)
    mapping = profile["field_mapping"]

    return {
        canonical: record.get(source, "")
        for source, canonical in mapping.items()
    }
