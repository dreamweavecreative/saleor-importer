
"""
DickerData Version 4.0 production safety utilities.

This module performs read-only production preflight checks.
It does not create, update, delete or publish Saleor records.
"""

import json
import hashlib

from pathlib import Path
from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone
from collections import Counter

from django.db import connection

from saleor.product.models import ProductVariant


def decimal_value(value):
    try:
        result = Decimal(str(value))
        if not result.is_finite():
            return None
        return result
    except (InvalidOperation, TypeError, ValueError):
        return None


def fingerprint(record):
    payload = json.dumps(
        record,
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(
        payload.encode("utf-8")
    ).hexdigest()


def analyse_pricing(row):

    rrp = decimal_value(row.get("RRPEx"))
    dealer = decimal_value(row.get("DealerEx"))

    result = {
        "rrp_ex": str(rrp) if rrp is not None else None,
        "dealer_ex": str(dealer) if dealer is not None else None,
        "margin_amount": None,
        "margin_percent": None,
        "warning": None,
    }

    if rrp is None or dealer is None:
        result["warning"] = "INVALID_PRICE"
        return result

    if rrp == 0:
        result["warning"] = "ZERO_RRP"
        return result

    margin = rrp - dealer
    margin_percent = (margin / rrp) * Decimal("100")

    result["margin_amount"] = str(
        margin.quantize(Decimal("0.01"))
    )

    result["margin_percent"] = str(
        margin_percent.quantize(Decimal("0.01"))
    )

    if margin < 0:
        result["warning"] = "NEGATIVE_MARGIN"

    elif margin == 0:
        result["warning"] = "ZERO_MARGIN"

    elif margin_percent < 10:
        result["warning"] = "LOW_MARGIN"

    return result


def compare_supplier_record(variant, row):

    metadata = dict(variant.private_metadata or {})

    suppliers = metadata.get("supplier_data", {})

    # Supplier identity is the distributor, not the manufacturer.
    supplier_name = "DickerData"

    existing = suppliers.get(supplier_name, {})

    comparisons = {
        "manufacturer": row.get("Vendor"),
        "manufacturer_part_number": row.get("VendorStockCode"),
        "supplier_item_code": row.get("StockCode"),
        "dealer_cost_ex_gst": row.get("DealerEx"),
        "rrp_ex_gst": row.get("RRPEx"),
        "supplier_quantity": row.get("StockAvailable"),
        "supplier_status": row.get("Status"),
        "supplier_type": row.get("Type"),
    }

    changes = {}

    for field, incoming in comparisons.items():

        previous = existing.get(field)

        incoming = (
            str(incoming)
            if incoming is not None
            else None
        )

        if previous is not None and str(previous) != incoming:
            changes[field] = {
                "previous": previous,
                "incoming": incoming,
            }

    return changes


def analyse_existing_sku(sku):

    matches = list(
        ProductVariant.objects.filter(
            sku__iexact=sku
        ).select_related("product")[:2]
    )

    if len(matches) > 1:
        return {
            "action": "AMBIGUOUS",
            "variant": None,
        }

    if not matches:
        return {
            "action": "CREATE",
            "variant": None,
        }

    return {
        "action": "UPDATE",
        "variant": matches[0],
    }


def run_preflight(filename):

    from importer.engine.readers import read_supplier_file
    from importer.engine.validators import validate_record
    from importer.engine.idempotency import file_hash

    path = Path(filename)

    if not path.is_file():
        raise FileNotFoundError(path)

    report = {
        "version": "4.0.0",
        "timestamp": datetime.now(
            timezone.utc
        ).isoformat(),
        "filename": str(path),
        "sha256": file_hash(path),
        "database": connection.vendor,
        "summary": {},
        "pricing_warnings": [],
        "supplier_changes": [],
        "inactive_products": [],
        "new_products": [],
        "ambiguous_skus": [],
        "invalid_records": [],
        "stock_changes": [],
    }

    counts = Counter()
    seen = set()

    for row_number, record in read_supplier_file(path):

        counts["total"] += 1

        row, errors = validate_record(record)

        sku = row.get("StockCode", "")

        if sku in seen:
            counts["duplicates"] += 1
            errors.append("Duplicate SKU in source file")

        else:
            seen.add(sku)

        if errors:
            counts["invalid"] += 1

            report["invalid_records"].append({
                "row": row_number,
                "sku": sku,
                "errors": errors,
            })

            continue

        counts["valid"] += 1

        # ---------------------------------------------
        # PRICING ANALYSIS
        # ---------------------------------------------

        pricing = analyse_pricing(row)

        if pricing["warning"]:

            counts[pricing["warning"]] += 1

            report["pricing_warnings"].append({
                "row": row_number,
                "sku": sku,
                **pricing,
            })

        # ---------------------------------------------
        # INACTIVE SUPPLIER PRODUCTS
        # ---------------------------------------------

        if row.get("Status") != "A":

            counts["inactive"] += 1

            report["inactive_products"].append({
                "row": row_number,
                "sku": sku,
                "description": row.get("StockDescription"),
                "status": row.get("Status"),
                "action": "MANUAL_REVIEW_REQUIRED",
            })

            continue

        # ---------------------------------------------
        # EXISTING SALEOR SKU
        # ---------------------------------------------

        match = analyse_existing_sku(sku)

        action = match["action"]

        counts[action.lower()] += 1

        if action == "AMBIGUOUS":

            report["ambiguous_skus"].append({
                "row": row_number,
                "sku": sku,
            })

            continue

        if action == "CREATE":

            report["new_products"].append({
                "row": row_number,
                "sku": sku,
                "description": row.get("StockDescription"),
            })

            continue

        # ---------------------------------------------
        # EXISTING SUPPLIER DATA COMPARISON
        # ---------------------------------------------

        variant = match["variant"]

        changes = compare_supplier_record(
            variant,
            row,
        )

        if changes:

            counts["changed"] += 1

            report["supplier_changes"].append({
                "row": row_number,
                "sku": sku,
                "variant_id": variant.pk,
                "changes": changes,
            })

            if "supplier_quantity" in changes:

                counts["stock_changed"] += 1

                report["stock_changes"].append({
                    "sku": sku,
                    **changes["supplier_quantity"],
                    "action": "REPORT_ONLY",
                })

        else:
            counts["unchanged"] += 1

    report["summary"] = dict(counts)

    report["safety"] = {
        "database_writes": False,
        "products_published": False,
        "warehouse_inventory_modified": False,
        "purchase_availability_modified": False,
        "inactive_products_deleted": False,
        "existing_products_deleted": False,
        "pricing_modified": False,
    }

    report["production_gate"] = {
        "validation_passed": (
            counts["invalid"] == 0
            and counts["duplicates"] == 0
            and counts["ambiguous"] == 0
        ),
        "manual_pricing_review_required": (
            counts["NEGATIVE_MARGIN"] > 0
            or counts["ZERO_MARGIN"] > 0
        ),
        "manual_inactive_review_required": (
            counts["inactive"] > 0
        ),
        "live_import_authorised": False,
    }

    return report


def save_report(report, destination):

    destination = Path(destination)

    destination.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    temporary = destination.with_suffix(".tmp")

    temporary.write_text(
        json.dumps(
            report,
            indent=2,
            ensure_ascii=False,
            default=str,
        ),
        encoding="utf-8",
    )

    temporary.replace(destination)

    return destination
