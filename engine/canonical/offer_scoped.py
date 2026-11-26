"""Snapshot-scoped offer write using the CALLER's cursor and transaction.

Migration 0001/0002 replaced UNIQUE(supplier_link_id) with
UNIQUE(supplier_link_id, snapshot_batch_id) and made snapshot_batch_id
NOT NULL. The legacy ON CONFLICT (supplier_link_id) target is therefore
invalid. This module targets the scoped index.

No locking and no transaction here: reconcile_import_batch already holds
the batch lock and a per-row savepoint. Supplier availability is never
written to Saleor warehouse stock.
"""
import json
import re
from decimal import Decimal, InvalidOperation

from .availability import validate as validate_availability

STATES = ("FINITE", "UNLIMITED", "UNKNOWN", "UNAVAILABLE")


class OfferScopeError(ValueError):
    pass


def resolve_state(availability_state, quantity, raw_data):
    """Explicit state wins; otherwise derive conservatively from the payload."""
    if availability_state:
        state = str(availability_state).strip().upper()
    elif isinstance(raw_data, dict) and raw_data.get("availability_state"):
        state = str(raw_data["availability_state"]).strip().upper()
    elif quantity is None:
        state = "UNKNOWN"
    elif int(quantity) == 0:
        state = "UNAVAILABLE"
    else:
        state = "FINITE"
    if state not in STATES:
        raise OfferScopeError("Invalid availability state: " + repr(state))
    qty = None if quantity is None else int(quantity)
    if state in ("UNKNOWN", "UNLIMITED"):
        qty = None
    elif state == "UNAVAILABLE":
        qty = 0
    validate_availability(state, qty)
    return state, qty


def validate_commercial(unit_cost, currency, minimum_order_quantity,
                        pack_quantity, lead_time_days):
    try:
        cost = Decimal(str(unit_cost))
        moq = Decimal(str(1 if minimum_order_quantity is None
                          else minimum_order_quantity))
        pack = Decimal(str(1 if pack_quantity is None else pack_quantity))
    except (InvalidOperation, TypeError, ValueError):
        raise OfferScopeError("Invalid offer numeric value") from None
    if not cost.is_finite() or cost < 0:
        raise OfferScopeError("Unit cost must be finite and non-negative")
    if not moq.is_finite() or moq <= 0:
        raise OfferScopeError("Minimum order quantity must be positive")
    if not pack.is_finite() or pack <= 0:
        raise OfferScopeError("Pack quantity must be positive")
    code = str(currency or "").strip().upper()
    if not re.fullmatch(r"[A-Z]{3}", code):
        raise OfferScopeError("Three-letter currency required")
    if lead_time_days is not None:
        if isinstance(lead_time_days, bool) or not isinstance(lead_time_days, int):
            raise OfferScopeError("Lead time must be an integer or None")
        if lead_time_days < 0:
            raise OfferScopeError("Lead time cannot be negative")
    return cost, code, moq, pack


UPSERT_SQL = """
INSERT INTO catalogue.supplier_offers
  (supplier_link_id, snapshot_batch_id, unit_cost, currency, tax_inclusive,
   available_quantity, availability_state, minimum_order_quantity,
   pack_quantity, lead_time_days, supplier_product_url, source_updated_at,
   valid_until, raw_data, is_active, observed_at, last_seen_at, updated_at)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,
        TRUE, NOW(), NOW(), NOW())
ON CONFLICT (supplier_link_id, snapshot_batch_id) DO UPDATE SET
   unit_cost = EXCLUDED.unit_cost,
   currency = EXCLUDED.currency,
   tax_inclusive = EXCLUDED.tax_inclusive,
   available_quantity = EXCLUDED.available_quantity,
   availability_state = EXCLUDED.availability_state,
   minimum_order_quantity = EXCLUDED.minimum_order_quantity,
   pack_quantity = EXCLUDED.pack_quantity,
   lead_time_days = EXCLUDED.lead_time_days,
   supplier_product_url = EXCLUDED.supplier_product_url,
   source_updated_at = EXCLUDED.source_updated_at,
   valid_until = EXCLUDED.valid_until,
   raw_data = EXCLUDED.raw_data,
   is_active = TRUE,
   retired_at = NULL,
   retirement_reason = NULL,
   last_seen_at = NOW(),
   updated_at = NOW()
RETURNING id
"""


def upsert_scoped(cursor, batch_id, supplier_link_id, unit_cost, currency,
                  tax_inclusive=False, available_quantity=None,
                  minimum_order_quantity=1, pack_quantity=1, lead_time_days=None,
                  supplier_product_url=None, source_updated_at=None,
                  valid_until=None, raw_data=None, availability_state=None):
    """Write one offer into one generation. Reappearance reactivates it."""
    if batch_id is None:
        raise OfferScopeError("batch_id is required for snapshot-scoped offers")
    if isinstance(raw_data, str):
        try:
            raw_data = json.loads(raw_data)
        except (TypeError, ValueError):
            raw_data = {}
    state, qty = resolve_state(availability_state, available_quantity, raw_data)
    cost, code, moq, pack = validate_commercial(
        unit_cost, currency, minimum_order_quantity, pack_quantity, lead_time_days)
    cursor.execute(UPSERT_SQL, (
        supplier_link_id, str(batch_id), cost, code, bool(tax_inclusive),
        qty, state, moq, pack, lead_time_days, supplier_product_url,
        source_updated_at, valid_until, json.dumps(raw_data or {}, default=str),
    ))
    row = cursor.fetchone()
    return row[0] if row else None
