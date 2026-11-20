"""DickerData supplier configuration."""

REQUIRED_FIELDS = (
    "StockCode",
    "Vendor",
    "VendorStockCode",
    "StockDescription",
    "PrimaryCategory",
    "RRPEx",
    "DealerEx",
    "StockAvailable",
    "Status",
    "Type",
)

SUPPORTED_FORMATS = {".csv", ".xlsx", ".ods"}

ACTIVE_STATUS = {"A"}

INACTIVE_STATUS = {"T"}

SUPPLIER = "DickerData"

DEFAULT_CURRENCY = "AUD"

# DealerEx is the supplier's dealer cost.
# RRPEx is the supplier's recommended retail price.
#
# Neither is automatically assumed to be the final
# Saleor selling price. That requires pricing policy.
