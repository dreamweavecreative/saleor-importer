"""Explicit supplier availability semantics. Pure functions, no database.

FINITE      known finite supplier quantity  (quantity > 0)
UNLIMITED   supplier explicitly guarantees unlimited (quantity NULL)
UNKNOWN     no reliable availability information (quantity NULL)
UNAVAILABLE supplier states unavailable/discontinued (quantity = 0)

DickerData StockAvailable=999999999 is a SENTINEL, not a stock count.
Without documented supplier evidence it maps to UNKNOWN, never UNLIMITED.
"""

STATES = ("FINITE", "UNLIMITED", "UNKNOWN", "UNAVAILABLE")
DICKERDATA_SENTINEL = 999999999


class AvailabilityError(ValueError):
    pass


def classify(raw, *, sentinel=DICKERDATA_SENTINEL, sentinel_state="UNKNOWN"):
    """Return (state, quantity). Never invents a quantity."""
    if sentinel_state not in ("UNKNOWN", "UNLIMITED"):
        raise AvailabilityError("sentinel_state must be UNKNOWN or UNLIMITED")
    if raw is None:
        return "UNKNOWN", None
    text = str(raw).strip()
    if text == "":
        return "UNKNOWN", None
    try:
        qty = int(text)
    except (TypeError, ValueError):
        raise AvailabilityError("Stock quantity is not an integer: " + repr(raw)) from None
    if qty == sentinel:
        return sentinel_state, None
    if qty < 0:
        raise AvailabilityError("Negative stock quantity is invalid")
    if qty == 0:
        return "UNAVAILABLE", 0
    return "FINITE", qty


def validate(state, quantity):
    """Reject inconsistent state/quantity pairs before any write."""
    if state not in STATES:
        raise AvailabilityError("Invalid availability state: " + repr(state))
    if state == "FINITE":
        if quantity is None or int(quantity) <= 0:
            raise AvailabilityError("FINITE requires quantity greater than zero")
        return True
    if state == "UNAVAILABLE":
        if quantity is None or int(quantity) != 0:
            raise AvailabilityError("UNAVAILABLE requires quantity exactly zero")
        return True
    if quantity is not None:
        raise AvailabilityError(state + " must not carry a numeric quantity")
    return True


def effective_quantity(requested, moq=1, pack=1):
    requested = int(requested)
    moq = max(1, int(moq or 1))
    pack = max(1, int(pack or 1))
    qty = max(requested, moq)
    if pack > 1:
        qty = ((qty + pack - 1) // pack) * pack
    return qty


def purchase_eligible(state, quantity, requested=1, moq=1, pack=1,
                      allow_unknown=False):
    """Return (eligible, reason). UNKNOWN is opt-in, never guaranteed."""
    validate(state, quantity)
    if int(requested) <= 0:
        return False, "INVALID_REQUESTED_QUANTITY"
    need = effective_quantity(requested, moq, pack)
    if state == "UNAVAILABLE":
        return False, "UNAVAILABLE"
    if state == "UNKNOWN":
        if not allow_unknown:
            return False, "UNKNOWN_STOCK"
        return True, "UNKNOWN_STOCK_ALLOWED"
    if state == "UNLIMITED":
        return True, "UNLIMITED"
    if int(quantity) < need:
        return False, "INSUFFICIENT_STOCK"
    return True, "FINITE_OK"
