"""Supplier-independent availability and whole-feed policy. Pure functions.

No supplier names, source columns or sentinel values appear here.
"""
from decimal import Decimal

STATES = ("FINITE", "UNLIMITED", "UNKNOWN", "UNAVAILABLE")


class PolicyError(ValueError):
    pass


def validate_state_quantity(state, quantity):
    if state not in STATES:
        raise PolicyError("Invalid availability state: %r" % (state,))
    if state == "FINITE":
        if quantity is None or int(quantity) <= 0:
            raise PolicyError("FINITE requires quantity greater than zero")
        return True
    if state == "UNAVAILABLE":
        if quantity is None or int(quantity) != 0:
            raise PolicyError("UNAVAILABLE requires quantity exactly zero")
        return True
    if quantity is not None:
        raise PolicyError(state + " must not carry a numeric quantity")
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
    """UNKNOWN is opt-in and never becomes a guarantee."""
    validate_state_quantity(state, quantity)
    if int(requested) <= 0:
        return False, "INVALID_REQUESTED_QUANTITY"
    need = effective_quantity(requested, moq, pack)
    if state == "UNAVAILABLE":
        return False, "UNAVAILABLE"
    if state == "UNKNOWN":
        return (True, "UNKNOWN_STOCK_ALLOWED") if allow_unknown \
            else (False, "UNKNOWN_STOCK")
    if state == "UNLIMITED":
        return True, "UNLIMITED"
    if int(quantity) < need:
        return False, "INSUFFICIENT_STOCK"
    return True, "FINITE_OK"


DEFAULT_THRESHOLDS = {
    "min_eligible_rows": 1,
    "max_row_decrease_pct": 10.0,
    "max_row_increase_pct": 50.0,
    "max_invalid_pct": 0.0,
    "max_unmatched_pct": 0.0,
    "max_duplicate_skus": 0,
}


def evaluate_feed(counts, previous_eligible_rows=None, thresholds=None):
    """Whole-feed gate. An incomplete feed can never retire a catalogue."""
    t = dict(DEFAULT_THRESHOLDS)
    t.update(thresholds or {})
    c = {k: int(v) for k, v in counts.items()}
    reasons = []

    source = c.get("source_rows", 0)
    eligible = c.get("eligible_rows", 0)
    excluded = c.get("excluded_rows", 0)
    matched = c.get("matched_rows", 0)
    offers = c.get("inserted_offers", 0)
    unmatched = c.get("unmatched_rows", 0)
    invalid = c.get("invalid_rows", 0)
    dupes = c.get("duplicate_skus", 0)

    if source != eligible + excluded:
        reasons.append("SOURCE_ROW_TOTALS_DO_NOT_RECONCILE")
    if eligible != matched:
        reasons.append("NOT_ALL_ELIGIBLE_ROWS_MATCHED")
    if eligible != offers:
        reasons.append("NOT_ALL_ELIGIBLE_ROWS_HAVE_OFFERS")
    if eligible < int(t["min_eligible_rows"]):
        reasons.append("BELOW_MINIMUM_ELIGIBLE_ROWS")
    if dupes > int(t["max_duplicate_skus"]):
        reasons.append("DUPLICATE_SUPPLIER_SKUS")
    if source > 0:
        if (invalid / source) * 100.0 > float(t["max_invalid_pct"]):
            reasons.append("INVALID_ROW_PERCENTAGE_EXCEEDED")
        if (unmatched / source) * 100.0 > float(t["max_unmatched_pct"]):
            reasons.append("UNMATCHED_ROW_PERCENTAGE_EXCEEDED")
    if previous_eligible_rows:
        prev = int(previous_eligible_rows)
        if prev > 0:
            if ((prev - eligible) / prev) * 100.0 > float(t["max_row_decrease_pct"]):
                reasons.append("ROW_COUNT_DECREASE_EXCEEDED")
            if ((eligible - prev) / prev) * 100.0 > float(t["max_row_increase_pct"]):
                reasons.append("ROW_COUNT_INCREASE_EXCEEDED")
    return (not reasons), reasons
