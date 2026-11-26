"""
Conservative DickerData CSV adapter.

Initial import policy:
- Include only Status=A and Type=StockedItem.
- Exclude bundles, special items, terminated and unknown-status rows.
- Treat StockAvailable=999999999 as unknown/unlimited, not literal stock.
- Map DealerEx to AUD supplier cost, excluding GST.
- Preserve every original source column in each adapted record.

This module performs no database, Saleor, inventory or media operations.
"""

from collections import Counter


PROFILE_ID = "dickerdata"
CURRENCY = "AUD"
UNLIMITED_STOCK_SENTINEL = 999999999

EXPECTED_COLUMNS = (
    "StockCode",
    "Vendor",
    "VendorStockCode",
    "StockDescription",
    "PrimaryCategory",
    "SecondaryCategory",
    "TertiaryCategory",
    "RRPEx",
    "DealerEx",
    "StockAvailable",
    "ETA",
    "Status",
    "Type",
    "BundledItem1",
    "BundledItem2",
    "BundledItem3",
    "BundledItem4",
    "BundledItem5",
)


def _text(value):
    if value is None:
        return ""
    return str(value).strip()


def adapt_records(source_records):
    """
    Convert parsed native DickerData rows into canonical importer records.

    Returns:
        (records, summary)

    Raises ValueError for schema drift, missing mandatory commercial values,
    or a feed with no eligible active stocked items.
    """
    if not isinstance(source_records, (list, tuple)) or not source_records:
        raise ValueError("DickerData feed contains no source records")

    if not all(isinstance(row, dict) for row in source_records):
        raise ValueError("Every DickerData source row must be an object")

    actual_columns = set(source_records[0])
    expected_columns = set(EXPECTED_COLUMNS)

    missing_columns = sorted(expected_columns - actual_columns)
    unexpected_columns = sorted(actual_columns - expected_columns)

    if missing_columns or unexpected_columns:
        details = []
        if missing_columns:
            details.append("missing columns: " + ", ".join(missing_columns))
        if unexpected_columns:
            details.append(
                "unexpected columns: " + ", ".join(unexpected_columns)
            )
        raise ValueError(
            "DickerData CSV schema mismatch (" + "; ".join(details) + ")"
        )

    status_counts = Counter()
    type_counts = Counter()
    excluded_status_counts = Counter()
    excluded_type_counts = Counter()
    excluded_reason_counts = Counter()

    adapted = []
    sentinel_count = 0
    eligible_count = 0

    for row_number, source in enumerate(source_records, 1):
        status = _text(source.get("Status")).upper()
        item_type = _text(source.get("Type"))
        item_type_key = item_type.casefold()

        status_counts[status or "<blank>"] += 1
        type_counts[item_type or "<blank>"] += 1

        # Deliberately narrow first release: active stocked items only.
        if status != "A" or item_type_key != "stockeditem":
            if status != "A":
                excluded_status_counts[status or "<blank>"] += 1
            if item_type_key != "stockeditem":
                excluded_type_counts[item_type or "<blank>"] += 1

            if status != "A" and item_type_key != "stockeditem":
                reason = "non_active_status_and_non_stocked_type"
            elif status != "A":
                reason = "non_active_status"
            else:
                reason = "non_stocked_type"

            excluded_reason_counts[reason] += 1
            continue

        sku = _text(source.get("StockCode"))
        name = _text(source.get("StockDescription"))
        manufacturer = _text(source.get("Vendor"))
        mpn = _text(source.get("VendorStockCode"))
        dealer_cost = _text(source.get("DealerEx"))
        stock_text = _text(source.get("StockAvailable"))

        # Do not let missing commercial fields turn into apparently valid
        # supplier offers. These are feed-level adapter errors.
        required_values = {
            "StockCode": sku,
            "StockDescription": name,
            "Vendor": manufacturer,
            "VendorStockCode": mpn,
            "DealerEx": dealer_cost,
            "StockAvailable": stock_text,
        }
        missing_values = [
            field for field, value in required_values.items() if not value
        ]
        if missing_values:
            raise ValueError(
                f"DickerData source row {row_number}: missing required "
                + ", ".join(missing_values)
            )

        try:
            stock_integer = int(stock_text)
        except (TypeError, ValueError):
            raise ValueError(
                f"DickerData source row {row_number}: "
                f"StockAvailable is not an integer: {stock_text!r}"
            ) from None

        if stock_integer == UNLIMITED_STOCK_SENTINEL:
            canonical_stock = None
            availability_state = "UNLIMITED"
            sentinel_count += 1
        elif stock_integer < 0:
            raise ValueError(
                f"DickerData source row {row_number}: "
                "StockAvailable cannot be negative"
            )
        elif stock_integer == 0:
            canonical_stock = 0
            availability_state = "UNAVAILABLE"
        else:
            canonical_stock = stock_integer
            availability_state = "FINITE"

        # Keep original DickerData fields and add canonical aliases.
        # normalize_record() ignores the original fields, while raw_record
        # retains them for auditability and offer traceability.
        adapted_row = dict(source)
        adapted_row.update({
            "sku": sku,
            "name": name,
            "manufacturer": manufacturer,
            "brand": None,
            "gtin": None,
            "mpn": mpn,
            "price": dealer_cost,
            "currency": CURRENCY,
            "stock": canonical_stock,
            "availability_state": availability_state,
            "moq": None,
        })

        adapted.append(adapted_row)
        eligible_count += 1

    if not adapted:
        raise ValueError(
            "DickerData feed contains no eligible A / StockedItem records"
        )

    summary = {
        "profile": PROFILE_ID,
        "source_rows": len(source_records),
        "eligible_rows": eligible_count,
        "excluded_rows": len(source_records) - eligible_count,
        "source_status_counts": dict(sorted(status_counts.items())),
        "source_type_counts": dict(sorted(type_counts.items())),
        "excluded_by_status": dict(sorted(excluded_status_counts.items())),
        "excluded_by_type": dict(sorted(excluded_type_counts.items())),
        "excluded_by_reason": dict(sorted(excluded_reason_counts.items())),
        "unlimited_stock_sentinel_rows": sentinel_count,
        "sentinel_availability_state": "UNLIMITED",
        "currency": CURRENCY,
        "supplier_cost_field": "DealerEx",
        "supplier_cost_tax_inclusive": False,
        "source_file_modified": False,
    }

    return adapted, summary
