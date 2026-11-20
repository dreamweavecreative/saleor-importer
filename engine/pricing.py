"""Saleor pricing integration with CAAG safety checks."""
from decimal import Decimal, ROUND_HALF_UP

SENTINEL_PRICE = Decimal("9999999")
SENTINEL_COST = Decimal("9999")
GST_RATE = Decimal("1.10")


def caag_check_pricing_safety(cost, price):
    """Returns True if pricing is safe, False if it should be quarantined."""
    if price is None or cost is None:
        return False
    try:
        p, c = Decimal(str(price)), Decimal(str(cost))
        if p <= 0:
            return False
        if p >= SENTINEL_PRICE:
            return False
        if c >= SENTINEL_COST:
            return False
        if c > p:
            return False
        return True
    except Exception:
        return False


def calculate_selling_price(rrp_ex_gst):
    """Calculate storefront selling price from RRP ex GST (channel uses prices_entered_with_tax=True)."""
    if rrp_ex_gst is None:
        return None
    try:
        rrp = Decimal(str(rrp_ex_gst))
        if rrp <= 0:
            return None
        return (rrp * GST_RATE).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except Exception:
        return None


def calculate_margin(cost_ex_gst, selling_price_inc_gst):
    """Calculate margin percentage."""
    try:
        cost = Decimal(str(cost_ex_gst))
        price = Decimal(str(selling_price_inc_gst))
        if price <= 0:
            return None
        price_ex = price / GST_RATE
        margin = ((price_ex - cost) / price_ex) * 100
        return margin.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except Exception:
        return None
