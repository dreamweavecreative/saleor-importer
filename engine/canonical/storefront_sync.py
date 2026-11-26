"""Storefront price/cost sync from active supplier offers (RRP-anchored).

Policy (confirmed):
  clean row      -> price = RRPEx * 1.1 (GST-inclusive), cost = DealerEx
  sentinel RRP/cost (0, 9999, 9999999) -> SKIP (quote-only, never priced)
  cost > RRPEx   -> SKIP (below-cost review queue, never auto-repriced)
  no usable RRP  -> SKIP

Only rows whose stored price/cost actually differ are written. discounted_price
is left to Saleor's recalculation task via the dirty flag. Supplier availability
is never written to warehouse stock. No product/variant deletion.

Pure classification is separated from DB access so it is unit-testable without
Django. All prices use ROUND_HALF_UP at 3dp to match the stored catalogue.
"""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

CENTS = Decimal("0.001")
GST = Decimal("1.1")
SENTINELS = {Decimal("0"), Decimal("9999"), Decimal("9999999")}

UPDATE = "UPDATE"
SKIP_SENTINEL = "SKIP_SENTINEL"
SKIP_BELOW_COST = "SKIP_BELOW_COST"
SKIP_NO_RRP = "SKIP_NO_RRP"
UNCHANGED = "UNCHANGED"


def _dec(value):
    try:
        d = Decimal(str(value).strip())
        return d if d.is_finite() else None
    except (InvalidOperation, ValueError, AttributeError):
        return None


def classify(rrp_ex, dealer_ex, stored_price, stored_cost):
    """Pure. Returns (action, target_price, target_cost)."""
    rrp = _dec(rrp_ex)
    cost = _dec(dealer_ex)
    if rrp is None:
        return SKIP_NO_RRP, None, None
    if rrp in SENTINELS or (cost is not None and cost in SENTINELS):
        return SKIP_SENTINEL, None, None
    if rrp <= 0:
        return SKIP_NO_RRP, None, None
    if cost is None or cost < 0:
        return SKIP_NO_RRP, None, None
    if cost > rrp:
        return SKIP_BELOW_COST, None, None
    price = (rrp * GST).quantize(CENTS, rounding=ROUND_HALF_UP)
    cost_q = cost.quantize(CENTS, rounding=ROUND_HALF_UP)
    sp = _dec(stored_price)
    sc = _dec(stored_cost)
    if sp == price and sc == cost_q:
        return UNCHANGED, price, cost_q
    return UPDATE, price, cost_q


def _rows(connection):
    from django.db import transaction
    with transaction.atomic():
        with connection.cursor() as cur:
            cur.execute("SET TRANSACTION READ ONLY")
            cur.execute("""
                SELECT cp.saleor_variant_id, l.supplier_sku,
                       o.unit_cost, o.raw_data->>'RRPEx' AS rrp_ex,
                       vl.price_amount, vl.cost_price_amount,
                       cp.saleor_product_id
                FROM catalogue.supplier_offers o
                JOIN catalogue.product_supplier_links l ON l.id=o.supplier_link_id
                JOIN catalogue.supplier_catalogue_state s
                  ON s.supplier_id=l.supplier_id AND s.active_batch_id=o.snapshot_batch_id
                JOIN catalogue.supplier_import_batches b
                  ON b.id=o.snapshot_batch_id AND b.snapshot_status='PROMOTED'
                JOIN catalogue.canonical_products cp ON cp.id=l.canonical_product_id
                JOIN public.product_productvariantchannellisting vl
                  ON vl.variant_id::text=cp.saleor_variant_id AND vl.channel_id=1
                WHERE o.is_active
                  AND (o.valid_until IS NULL OR o.valid_until > NOW())
                ORDER BY cp.saleor_variant_id
            """)
            return cur.fetchall()


def plan(connection):
    """READ-ONLY. Classify every active offer; return counts + update targets."""
    counts = {UPDATE:0, UNCHANGED:0, SKIP_SENTINEL:0, SKIP_BELOW_COST:0, SKIP_NO_RRP:0}
    updates, below_cost = [], []
    for vid, sku, cost, rrp, price, stored_cost, pid in _rows(connection):
        action, tp, tc = classify(rrp, cost, price, stored_cost)
        counts[action] += 1
        if action == UPDATE:
            updates.append({"variant_id": vid, "product_id": pid, "sku": sku,
                            "price": str(tp), "cost": str(tc),
                            "old_price": str(price), "old_cost": str(stored_cost)})
        elif action == SKIP_BELOW_COST:
            below_cost.append({"sku": sku, "rrp_ex": rrp, "dealer_ex": str(cost)})
    return {"mode":"READ_ONLY_PLAN", "counts":counts,
            "update_count":len(updates), "updates":updates,
            "below_cost_review":below_cost,
            "saleor_written":False}


def apply(connection, updates, *, confirm=False, max_changes=2000, chunk_size=500):
    """Write price/cost only for classified UPDATE rows. Chunked, guarded."""
    if not confirm:
        return {"applied":False, "error":"Write refused: pass confirm=True"}
    if not updates:
        return {"applied":True, "chunks":0, "variant_listings_changed":0,
                "products_flagged":0, "warehouse_stock_written":0}
    if len(updates) > int(max_changes):
        return {"applied":False, "error":"Scope exceeds max_changes",
                "would_change":len(updates), "max_changes":int(max_changes)}
    from django.db import transaction
    changed = flagged = chunks = 0
    for start in range(0, len(updates), int(chunk_size)):
        batch = updates[start:start+int(chunk_size)]
        with transaction.atomic():
            with connection.cursor() as cur:
                cur.execute("SET LOCAL lock_timeout='15s'")
                cur.execute("SET LOCAL statement_timeout='5min'")
                for u in batch:
                    cur.execute("""
                        UPDATE public.product_productvariantchannellisting
                        SET price_amount=%s, cost_price_amount=%s
                        WHERE variant_id::text=%s AND channel_id=1
                          AND (price_amount IS DISTINCT FROM %s
                               OR cost_price_amount IS DISTINCT FROM %s)
                    """, (u["price"], u["cost"], u["variant_id"],
                          u["price"], u["cost"]))
                    changed += cur.rowcount
                pids = list({u["product_id"] for u in batch if u["product_id"]})
                if pids:
                    cur.execute("""
                        UPDATE public.product_productchannellisting
                        SET discounted_price_dirty=TRUE
                        WHERE product_id::text = ANY(%s) AND channel_id=1
                          AND NOT discounted_price_dirty
                    """, (pids,))
                    flagged += cur.rowcount
        chunks += 1
    return {"applied":True, "chunks":chunks,
            "variant_listings_changed":changed, "products_flagged":flagged,
            "warehouse_stock_written":0, "products_deleted":0}
