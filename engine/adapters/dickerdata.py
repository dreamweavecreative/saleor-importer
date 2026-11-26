"""DickerData adapter — first implementation of the supplier contract.

All DickerData-specific knowledge is confined to this module:
  * native column names
  * Status / Type eligibility
  * the 999999999 stock sentinel and its declared meaning
  * DealerEx as AUD ex-GST cost
  * exact StockCode identity with no MPN fallback

Per the CAAG specification, StockAvailable = 999999999 means UNLIMITED
availability. That declaration lives here, not in the shared lifecycle.
"""
import csv
from decimal import Decimal, InvalidOperation

from .base import SupplierAdapter, FeedValidationError, register

EXPECTED_COLUMNS = (
    "StockCode", "Vendor", "VendorStockCode", "StockDescription",
    "PrimaryCategory", "SecondaryCategory", "TertiaryCategory",
    "RRPEx", "DealerEx", "StockAvailable", "ETA", "Status", "Type",
    "BundledItem1", "BundledItem2", "BundledItem3", "BundledItem4",
    "BundledItem5",
)

UNLIMITED_STOCK_SENTINEL = 999999999
ACTIVE_STATUS = "A"
STOCKED_TYPE = "stockeditem"
CURRENCY = "AUD"


def _text(value):
    return "" if value is None else str(value).strip()


class DickerDataAdapter(SupplierAdapter):

    @property
    def supplier_id(self):
        return "dickerdata"

    @property
    def display_name(self):
        return "DickerData"

    @property
    def supported_formats(self):
        return ("CSV",)

    @property
    def feed_kind(self):
        # DickerData publishes its complete catalogue on every export.
        return "FULL"

    @property
    def ordering_mode(self):
        return "sequence"

    @property
    def sentinel_policy(self):
        return {"value": UNLIMITED_STOCK_SENTINEL, "meaning": "UNLIMITED"}

    @property
    def tax_inclusive(self):
        return False

    @property
    def thresholds(self):
        # Calibrated against the verified 72,513-row baseline.
        return {
            "min_eligible_rows": 1000,
            "max_row_decrease_pct": 10.0,
            "max_row_increase_pct": 50.0,
            "max_invalid_pct": 0.0,
            "max_unmatched_pct": 0.0,
            "max_duplicate_skus": 0,
        }

    # ---- headers ---------------------------------------------------------
    def validate_headers(self, headers):
        clean = [_text(h) for h in (headers or [])]
        if not clean:
            raise FeedValidationError("CSV header row required")
        if any(not h for h in clean):
            raise FeedValidationError("CSV contains an empty column header")
        folded = [h.casefold() for h in clean]
        if len(folded) != len(set(folded)):
            raise FeedValidationError("CSV contains duplicate column headers")
        missing = sorted(set(EXPECTED_COLUMNS) - set(clean))
        unexpected = sorted(set(clean) - set(EXPECTED_COLUMNS))
        if missing or unexpected:
            raise FeedValidationError(
                "DickerData schema mismatch; missing=%s unexpected=%s"
                % (missing, unexpected))
        return clean

    # ---- streaming -------------------------------------------------------
    def stream_records(self, path):
        with open(path, "r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            clean = self.validate_headers(reader.fieldnames)
            reader.fieldnames = clean
            for row in reader:
                if None in row:
                    raise FeedValidationError("CSV row contains extra columns")
                if not any(v is not None and str(v).strip()
                           for v in row.values()):
                    continue
                yield row

    # ---- eligibility -----------------------------------------------------
    def is_eligible(self, record):
        status = _text(record.get("Status")).upper()
        kind = _text(record.get("Type")).casefold()
        if status != ACTIVE_STATUS and kind != STOCKED_TYPE:
            return False, "non_active_status_and_non_stocked_type"
        if status != ACTIVE_STATUS:
            return False, "non_active_status"
        if kind != STOCKED_TYPE:
            return False, "non_stocked_type"
        return True, ""

    # ---- availability (sentinel semantics live here) ---------------------
    def availability(self, record):
        raw = _text(record.get("StockAvailable"))
        if raw == "":
            return "UNKNOWN", None
        try:
            qty = int(raw)
        except (TypeError, ValueError):
            raise FeedValidationError(
                "StockAvailable is not an integer: %r" % raw) from None
        if qty == UNLIMITED_STOCK_SENTINEL:
            return "UNLIMITED", None
        if qty < 0:
            raise FeedValidationError("StockAvailable cannot be negative")
        if qty == 0:
            return "UNAVAILABLE", 0
        return "FINITE", qty

    # ---- normalisation ---------------------------------------------------
    def normalise(self, record):
        sku = _text(record.get("StockCode"))
        name = _text(record.get("StockDescription"))
        manufacturer = _text(record.get("Vendor"))
        mpn = _text(record.get("VendorStockCode"))
        cost = _text(record.get("DealerEx"))
        state, qty = self.availability(record)

        required = {"StockCode": sku, "StockDescription": name,
                    "Vendor": manufacturer, "VendorStockCode": mpn,
                    "DealerEx": cost}
        missing = [k for k, v in required.items() if not v]
        if missing:
            raise FeedValidationError(
                "missing required " + ", ".join(missing))

        try:
            price = Decimal(cost)
        except InvalidOperation:
            raise FeedValidationError("Invalid DealerEx") from None
        if not price.is_finite() or price < 0:
            raise FeedValidationError("Invalid DealerEx")

        out = dict(record)          # retain every native column for audit
        out.update({
            "sku": sku, "name": name, "manufacturer": manufacturer,
            "brand": None, "gtin": None, "mpn": mpn,
            "price": str(price), "currency": CURRENCY,
            "stock": qty, "availability_state": state, "moq": None,
        })
        return out

    def validate_record(self, normalised):
        if not normalised.get("sku"):
            raise FeedValidationError("Supplier SKU required")
        if not normalised.get("name"):
            raise FeedValidationError("Product name required")
        if normalised.get("currency") != CURRENCY:
            raise FeedValidationError("DickerData cost must be AUD")
        state = normalised.get("availability_state")
        qty = normalised.get("stock")
        if state == "FINITE" and (qty is None or int(qty) <= 0):
            raise FeedValidationError("FINITE requires positive quantity")
        if state == "UNAVAILABLE" and qty != 0:
            raise FeedValidationError("UNAVAILABLE requires zero quantity")
        if state in ("UNKNOWN", "UNLIMITED") and qty is not None:
            raise FeedValidationError(state + " must not carry a quantity")
        return True

    # ---- identity: exact StockCode only ---------------------------------
    def resolve_identity(self, cursor, supplier_sku):
        """StockCode -> Saleor variant SKU -> canonical variant bridge.

        No MPN fallback. Ambiguity is a conflict, never a guess.
        """
        from importer.engine.canonical.supplier_import import (
            _resolve_dickerdata_variant,
        )
        return _resolve_dickerdata_variant(cursor, supplier_sku)

    def lead_time_days(self, record):
        # ETA is free text ('TBA', dates). Never invent a numeric lead time.
        return None


register(DickerDataAdapter)
