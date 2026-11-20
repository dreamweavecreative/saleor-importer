"""Supplier-agnostic row normalization.

Supports:
1. DickerData native feed headers discovered from DickerData.csv.
2. Generic importer template headers discovered from product_import_template.csv.

No Saleor writes here. Pure normalization/validation.
"""
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
import re
import logging

logger = logging.getLogger(__name__)

GST_RATE = Decimal("1.1")
UNLIMITED_QUANTITY_THRESHOLD = 999999999
ACTIVE_STATUSES = {"A"}


@dataclass
class NormalizedRow:
    import_flag: str
    supplier: str
    supplier_sku: str
    sku: str
    product_name: str
    brand: str
    category: str
    collection: str
    description: str
    cost_ex_gst: Decimal | None
    selling_price_inc_gst: Decimal | None
    supplier_quantity: int
    image_url: str
    weight_kg: float | None
    supplier_url: str
    raw_errors: list = field(default_factory=list)

    category_level_1: str = ""
    category_level_2: str = ""
    category_level_3: str = ""
    source_product_type: str = ""
    supplier_status: str = ""


def _clean(value) -> str:
    return str(value or "").strip()


def _decimal(value):
    text = _clean(value)
    if not text:
        return None
    text = re.sub(r"[^0-9.\-]", "", text)
    if text in {"", "-", ".", "-."}:
        return None
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return None


def _int(value, default=0):
    text = _clean(value)
    if not text:
        return default
    text = re.sub(r"[^0-9.\-]", "", text)
    if not text:
        return default
    try:
        return int(float(text))
    except (ValueError, TypeError):
        return default


def _float(value):
    text = _clean(value)
    if not text or text.lower() in {"n/a", "none", "null"}:
        return None
    text = re.sub(r"[^0-9.\-]", "", text)
    if not text:
        return None
    try:
        return float(text)
    except (ValueError, TypeError):
        return None


def _truthy(value) -> bool:
    return _clean(value).lower() in {"true", "1", "yes", "y", "import"}


def normalize_row(row: dict):
    """Return NormalizedRow or None. Errors are carried in raw_errors."""
    if not isinstance(row, dict):
        return None

    errors = []

    # DickerData native format.
    if "StockCode" in row:
        supplier = "DickerData"
        sku = _clean(row.get("StockCode"))
        supplier_sku = _clean(row.get("VendorStockCode")) or sku
        product_name = _clean(row.get("StockDescription"))
        brand = _clean(row.get("Vendor"))

        cat1 = _clean(row.get("PrimaryCategory"))
        cat2 = _clean(row.get("SecondaryCategory"))
        cat3 = _clean(row.get("TertiaryCategory"))
        category = " > ".join([c for c in (cat1, cat2, cat3) if c])

        status = _clean(row.get("Status")).upper()
        import_flag = "TRUE" if status in ACTIVE_STATUSES else "FALSE"

        cost_ex_gst = _decimal(row.get("DealerEx"))
        rrp_ex_gst = _decimal(row.get("RRPEx"))
        selling_price_inc_gst = (
            rrp_ex_gst * GST_RATE if rrp_ex_gst is not None else None
        )

        supplier_quantity = _int(row.get("StockAvailable"), 0)
        source_product_type = _clean(row.get("Type"))
        description = product_name
        weight_kg = None
        image_url = ""
        supplier_url = ""
        collection = ""

        if not sku:
            errors.append("Missing StockCode")
        if not product_name:
            errors.append("Missing StockDescription")

    # Generic template format.
    else:
        supplier = _clean(row.get("supplier"))
        supplier_sku = _clean(row.get("supplier_sku"))
        sku = _clean(row.get("sku"))
        product_name = _clean(row.get("product_name"))
        brand = _clean(row.get("brand"))
        category = _clean(row.get("category"))
        collection = _clean(row.get("collection"))
        description = _clean(row.get("description")) or product_name
        import_flag = "TRUE" if _truthy(row.get("import", "TRUE")) else "FALSE"

        cost_ex_gst = _decimal(row.get("cost_ex_gst"))
        selling_price_inc_gst = _decimal(row.get("selling_price_inc_gst"))
        supplier_quantity = _int(row.get("supplier_quantity"), 0)
        weight_kg = _float(row.get("weight_kg"))
        image_url = _clean(row.get("image_url"))
        supplier_url = _clean(row.get("supplier_url"))

        cat1 = category
        cat2 = ""
        cat3 = ""
        source_product_type = ""
        status = ""

        if not supplier:
            errors.append("Missing supplier")
        if not supplier_sku:
            errors.append("Missing supplier_sku")
        if not sku:
            errors.append("Missing sku")
        if not product_name:
            errors.append("Missing product_name")

    return NormalizedRow(
        import_flag=import_flag,
        supplier=supplier,
        supplier_sku=supplier_sku,
        sku=sku,
        product_name=product_name,
        brand=brand,
        category=category,
        collection=collection,
        description=description,
        cost_ex_gst=cost_ex_gst,
        selling_price_inc_gst=selling_price_inc_gst,
        supplier_quantity=supplier_quantity,
        image_url=image_url,
        weight_kg=weight_kg,
        supplier_url=supplier_url,
        raw_errors=errors,
        category_level_1=cat1,
        category_level_2=cat2,
        category_level_3=cat3,
        source_product_type=source_product_type,
        supplier_status=status,
    )
