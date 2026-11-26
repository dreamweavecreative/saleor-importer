"""Active-generation supplier selection.

Every read joins the supplier active-snapshot pointer. Offers in unpromoted
or superseded generations are invisible. This is the correction the legacy
landed_cost selector lacked.

Ranking is pure and testable; landed-cost arithmetic is delegated to the
existing engine so there is only one implementation of the money maths.
"""
from decimal import Decimal

from .availability import purchase_eligible, effective_quantity

ACTIVE_JOIN = """
    JOIN catalogue.product_supplier_links l ON l.id = o.supplier_link_id
    JOIN catalogue.supplier_catalogue_state s
      ON s.supplier_id = l.supplier_id
     AND s.active_batch_id = o.snapshot_batch_id
    JOIN catalogue.supplier_import_batches b
      ON b.id = o.snapshot_batch_id
     AND b.snapshot_status = 'PROMOTED'
"""


def rank_candidates(candidates):
    """Pure ordering. Lowest landed unit cost, then total, reliability, priority."""
    def key(c):
        return (
            Decimal(str(c["landed_unit_cost"])),
            Decimal(str(c["landed_total"])),
            -Decimal(str(c.get("reliability_score", 50))),
            int(c.get("priority", 100)),
            str(c.get("supplier_id", "")),
        )
    return sorted(candidates, key=key)


def screen(offer, requested=1, allow_unknown=False):
    """Pure eligibility screen for one active offer."""
    ok, reason = purchase_eligible(
        offer.get("availability_state"), offer.get("available_quantity"),
        requested=requested,
        moq=offer.get("minimum_order_quantity") or 1,
        pack=offer.get("pack_quantity") or 1,
        allow_unknown=allow_unknown)
    if not ok:
        return False, reason
    # supplier_landed_cost_rules is OPTIONAL (shipping/duty/priority).
    # A missing row means no adjustments, not an ineligible supplier.
    # Only an explicitly inactive rule disqualifies the offer.
    if offer.get("rule_active") is False:
        return False, "SUPPLIER_RULE_INACTIVE"
    return True, "ELIGIBLE"


def active_offers(connection, canonical_product_id, currency="AUD"):
    sql = """
        SELECT o.id AS offer_id, o.supplier_link_id, o.unit_cost, o.currency,
               o.tax_inclusive, o.available_quantity, o.availability_state,
               o.minimum_order_quantity, o.pack_quantity, o.lead_time_days,
               o.snapshot_batch_id,
               l.supplier_id, l.supplier_sku,
               r.shipping_flat, r.shipping_per_unit, r.tax_rate, r.duty_rate,
               r.priority, r.reliability_score, r.is_active AS rule_active
        FROM catalogue.supplier_offers o
    """ + ACTIVE_JOIN + """
        LEFT JOIN catalogue.supplier_landed_cost_rules r
               ON r.supplier_id = l.supplier_id AND r.currency = %s
        WHERE l.canonical_product_id = %s
          AND o.is_active = TRUE
          AND o.currency = %s
          AND (o.valid_until IS NULL OR o.valid_until > NOW())
        ORDER BY l.supplier_id
    """
    code = str(currency).strip().upper()
    with connection.cursor() as cur:
        cur.execute(sql, (code, str(canonical_product_id), code))
        names = [c[0] for c in cur.description]
        return [dict(zip(names, r)) for r in cur.fetchall()]


def select(connection, canonical_product_id, requested_quantity=1,
           currency="AUD", allow_unknown=False, record_history=False):
    """Select the best purchasable offer from active generations only."""
    from .landed_cost import LandedCostEngine   # money maths only, no reads
    requested = Decimal(str(requested_quantity))
    if not requested.is_finite() or requested <= 0:
        raise ValueError("Requested quantity must be positive")
    offers = active_offers(connection, canonical_product_id, currency)
    eligible, rejected = [], []
    for o in offers:
        ok, reason = screen(o, requested=requested, allow_unknown=allow_unknown)
        if not ok:
            rejected.append({"supplier_id": o["supplier_id"],
                             "offer_id": o["offer_id"], "reason": reason})
            continue
        qty = effective_quantity(requested,
                                 o["minimum_order_quantity"] or 1,
                                 o["pack_quantity"] or 1)
        costs = LandedCostEngine.calculate(
            unit_cost=o["unit_cost"], quantity=qty,
            shipping_flat=o["shipping_flat"] or 0,
            shipping_per_unit=o["shipping_per_unit"] or 0,
            tax_rate=o["tax_rate"] or 0, duty_rate=o["duty_rate"] or 0,
            tax_inclusive=o["tax_inclusive"])
        eligible.append({
            "supplier_id": o["supplier_id"], "supplier_sku": o["supplier_sku"],
            "supplier_link_id": o["supplier_link_id"], "offer_id": o["offer_id"],
            "snapshot_batch_id": str(o["snapshot_batch_id"]),
            "availability_state": o["availability_state"],
            "priority": o["priority"], "reliability_score": o["reliability_score"],
            "lead_time_days": o["lead_time_days"], **costs})
    eligible = rank_candidates(eligible)
    selected = eligible[0] if eligible else None
    return {"status": "SELECTED" if selected else "NO_ELIGIBLE_SUPPLIER",
            "selected": selected, "eligible_offers": eligible,
            "rejected_offers": rejected, "currency": str(currency).upper(),
            "selection_method": "ACTIVE_GENERATION_LANDED_UNIT_COST",
            "history_recorded": False}
