"""Supplier record validation."""

from .normalizers import (
    normalise_record,
    normalise_decimal,
    normalise_integer,
)

from ..config.suppliers import get_supplier_profile


def validate_record(record, supplier_id="dickerdata"):
    errors = []

    try:
        profile = get_supplier_profile(supplier_id)
        required_fields = profile["required_fields"]
        mapping = profile["field_mapping"]
        
        # 1. Validate raw source fields exist
        for field in required_fields:
            if not record.get(field):
                errors.append(f"Missing required source field: {field}")
                
        if errors:
            return record, errors

        # 2. Clean the raw record
        row = normalise_record(record)

        # 3. Validate specific data types using profile mappings
        # Find source column names dynamically
        sku_source = next((k for k, v in mapping.items() if v == "supplier_item_code"), None)
        sku = str(row.get(sku_source) or "").strip() if sku_source else ""
        if not sku:
            errors.append("Missing mapped SKU (supplier_item_code)")

        # Validate pricing fields
        price_fields = [k for k, v in mapping.items() if v in ("supplier_rrp_ex_gst", "supplier_cost_ex_gst")]
        for field in price_fields:
            if row.get(field):
                try:
                    normalise_decimal(row.get(field), field)
                except ValueError as exc:
                    errors.append(str(exc))

        # Validate stock field
        stock_source = next((k for k, v in mapping.items() if v == "supplier_quantity"), None)
        if stock_source and row.get(stock_source):
            try:
                normalise_integer(row.get(stock_source), stock_source)
            except ValueError as exc:
                errors.append(str(exc))

        # Validate status
        status_source = next((k for k, v in mapping.items() if v == "supplier_status"), None)
        if status_source:
            status = row.get(status_source)
            active = profile.get("active_status_values", ())
            inactive = profile.get("inactive_status_values", ())
            if status and status not in active and status not in inactive:
                errors.append(f"Unknown product status: {status}")

        return row, errors

    except Exception as exc:
        return record, [str(exc)]


def validate_headers(headers, supplier_id="dickerdata"):
    profile = get_supplier_profile(supplier_id)
    required = profile["required_fields"]
    
    missing = [
        field
        for field in required
        if field not in headers
    ]

    if missing:
        raise ValueError(
            "Missing supplier columns: "
            + ", ".join(missing)
        )

