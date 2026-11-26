"""Chunked, resumable publication withdrawal for products with no eligible supply.

Reuses visibility_bulk's CTE fragments so the target set is computed by the
same SQL that produced the read-only report. No duplicated eligibility SQL.

Guarantees:
  * never deletes a product, variant, listing or media row
  * never writes warehouse stock
  * never modifies supplier offers or snapshots
  * never withdraws a variant referenced by a live checkout line
  * withdrawal only; reactivation is a separate explicit action
  * per-chunk transactions, resumable, circuit breaker on total scope
"""

CHUNK = 500
WITHDRAW = "WITHDRAW"
REACTIVATE = "REACTIVATE"
UNCHANGED = "UNCHANGED"
PROTECTED = "PROTECTED_CHECKOUT"


class VisibilityApplyError(RuntimeError):
    pass


def decide(is_published, visible, purchasable, has_supply, reactivate=False):
    """Pure policy. Identical semantics to visibility_policy.decide."""
    sellable = bool(is_published) or bool(visible) or bool(purchasable)
    if not has_supply:
        return WITHDRAW if sellable else UNCHANGED
    if reactivate and not (is_published and visible and purchasable):
        return REACTIVATE
    return UNCHANGED


def partition(rows, reactivate=False):
    """Pure. rows = (cpid, pid, vid, pub, vis, buy, has_supply, in_checkout)."""
    out = {WITHDRAW: [], REACTIVATE: [], UNCHANGED: [], PROTECTED: []}
    for cpid, pid, vid, pub, vis, buy, supply, in_checkout in rows:
        action = decide(pub, vis, buy, supply, reactivate)
        item = {"canonical_product_id": cpid,
                "saleor_product_id": pid,
                "saleor_variant_id": vid}
        if action == WITHDRAW and in_checkout:
            out[PROTECTED].append(item)
        else:
            out[action].append(item)
    return out


def _target_sql():
    from .visibility_bulk import ACTIVE_CTE
    return """
WITH """ + ACTIVE_CTE + """,
pub AS (
    SELECT product_id::text AS pid,
           BOOL_OR(is_published) AS is_published,
           BOOL_OR(visible_in_listings) AS visible
    FROM public.product_productchannellisting
    GROUP BY product_id
),
buy AS (
    SELECT variant_id::text AS vid,
           BOOL_OR(is_available_for_purchase) AS purchasable
    FROM public.product_productvariantchannellisting
    GROUP BY variant_id
),
targets AS (
    SELECT cp.id::text AS cpid,
           cp.saleor_product_id,
           cp.saleor_variant_id,
           COALESCE(pub.is_published, FALSE) AS pub,
           COALESCE(pub.visible, FALSE) AS vis,
           COALESCE(buy.purchasable, FALSE) AS buy,
           (COALESCE(supply.has_eligible, FALSE)
            OR COALESCE(owned.has_owned, FALSE)) AS has_supply,
           EXISTS (SELECT 1 FROM public.checkout_checkoutline cl
                   WHERE cl.variant_id::text = cp.saleor_variant_id) AS in_checkout
    FROM catalogue.canonical_products cp
    LEFT JOIN supply ON supply.canonical_product_id = cp.id
    LEFT JOIN owned  ON owned.canonical_product_id = cp.id
    LEFT JOIN pub    ON pub.pid = cp.saleor_product_id
    LEFT JOIN buy    ON buy.vid = cp.saleor_variant_id
)
SELECT cpid, saleor_product_id, saleor_variant_id,
       pub, vis, buy, has_supply, in_checkout
FROM targets
WHERE NOT has_supply
ORDER BY cpid
"""


def plan(connection, reactivate=False, limit=None):
    """READ-ONLY. Returns partitioned actions with checkout protection."""
    from django.db import transaction
    sql = _target_sql()
    params = {"allow_unknown": False}
    if limit:
        sql += " LIMIT %(limit)s"
        params["limit"] = int(limit)
    with transaction.atomic():
        with connection.cursor() as cur:
            cur.execute("SET TRANSACTION READ ONLY")
            cur.execute(sql, params)
            rows = cur.fetchall()
    parts = partition(rows, reactivate)
    return {"mode": "READ_ONLY_PLAN",
            "evaluated_no_supply": len(rows),
            "withdraw_count": len(parts[WITHDRAW]),
            "protected_checkout_count": len(parts[PROTECTED]),
            "reactivate_count": len(parts[REACTIVATE]),
            "already_withdrawn_count": len(parts[UNCHANGED]),
            "actions": parts}


def apply_withdrawal(connection, targets, *, confirm=False, max_changes=20000,
                     chunk_size=CHUNK, progress=None):
    """Withdraw publication + purchase eligibility. Chunked and resumable."""
    if not confirm:
        return {"applied": False,
                "error": "Write refused: pass confirm=True explicitly"}
    if not targets:
        return {"applied": True, "chunks": 0,
                "product_listings_changed": 0, "variant_listings_changed": 0,
                "products_deleted": 0, "warehouse_stock_written": 0,
                "supplier_offers_modified": 0}
    if len(targets) > int(max_changes):
        return {"applied": False,
                "error": "Scope exceeds max_changes circuit breaker",
                "would_change": len(targets), "max_changes": int(max_changes)}

    from django.db import transaction
    product_changed = variant_changed = chunks = 0
    for start in range(0, len(targets), int(chunk_size)):
        batch = targets[start:start + int(chunk_size)]
        pids = [t["saleor_product_id"] for t in batch if t["saleor_product_id"]]
        vids = [t["saleor_variant_id"] for t in batch if t["saleor_variant_id"]]
        with transaction.atomic():
            with connection.cursor() as cur:
                cur.execute("SET LOCAL lock_timeout = '15s'")
                cur.execute("SET LOCAL statement_timeout = '5min'")
                if vids:
                    cur.execute("""
                        SELECT variant_id::text FROM public.checkout_checkoutline
                        WHERE variant_id::text = ANY(%s)
                    """, (vids,))
                    live = {r[0] for r in cur.fetchall()}
                    if live:
                        vids = [v for v in vids if v not in live]
                        batch = [t for t in batch
                                 if t["saleor_variant_id"] not in live]
                        pids = [t["saleor_product_id"] for t in batch
                                if t["saleor_product_id"]]
                if pids:
                    cur.execute("""
                        UPDATE public.product_productchannellisting
                        SET is_published = FALSE, visible_in_listings = FALSE
                        WHERE product_id::text = ANY(%s)
                          AND (is_published OR visible_in_listings)
                    """, (pids,))
                    product_changed += cur.rowcount
                if vids:
                    cur.execute("""
                        UPDATE public.product_productvariantchannellisting
                        SET is_available_for_purchase = FALSE
                        WHERE variant_id::text = ANY(%s)
                          AND is_available_for_purchase
                    """, (vids,))
                    variant_changed += cur.rowcount
        chunks += 1
        if progress:
            progress(chunks, min(start + chunk_size, len(targets)), len(targets))

    return {"applied": True, "chunks": chunks,
            "product_listings_changed": product_changed,
            "variant_listings_changed": variant_changed,
            "products_deleted": 0, "warehouse_stock_written": 0,
            "supplier_offers_modified": 0}
