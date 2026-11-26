"""Post-promotion catalogue state: price sync, withdrawal, safe reactivation.

Runs AFTER the promotion transaction commits. Each phase uses its own bounded
transactions so a failure cannot roll back a valid promotion.

Order:
  1. Storefront price/cost sync from active offers (RRP-anchored, guarded).
  2. Withdraw products with no eligible supply and no owned stock.
  3. Reactivate ONLY products the automation previously withdrew.

Two independent reactivation gates, both required:
  * LEDGER GATE — the last automation action must be WITHDRAW. A product an
    operator unpublished has no ledger row and is never touched.
  * PRICE GATE — the active offer must not carry a sentinel RRP/cost and must
    not be below cost. Availability alone is NOT sufficient: a variant can be
    UNLIMITED yet have a defective price, and republishing it would put a
    known-bad price back on sale.
"""
import json

ACTOR = "automation"


def _ledger_exists(connection):
    with connection.cursor() as cur:
        cur.execute("SELECT to_regclass('catalogue.visibility_actions')")
        return cur.fetchone()[0] is not None


def _record(connection, items, action, reason):
    if not items:
        return 0
    with connection.cursor() as cur:
        cur.executemany("""
            INSERT INTO catalogue.visibility_actions
              (canonical_product_id, saleor_product_id, saleor_variant_id,
               action, reason, actor)
            VALUES (%s,%s,%s,%s,%s,%s)
        """, [(i["canonical_product_id"], i.get("saleor_product_id"),
               i.get("saleor_variant_id"), action, reason, ACTOR)
              for i in items])
        return len(items)


def reactivation_targets(connection):
    """Products with eligible supply, valid price, and a prior WITHDRAW."""
    if not _ledger_exists(connection):
        return []
    from django.db import transaction
    with transaction.atomic():
        with connection.cursor() as cur:
            cur.execute("SET TRANSACTION READ ONLY")
            cur.execute(r"""
            WITH act AS (
              SELECT l.canonical_product_id, o.availability_state,
                     o.available_quantity, o.minimum_order_quantity,
                     o.unit_cost,
                     NULLIF(BTRIM(o.raw_data->>'RRPEx'),'') AS rrp_ex
              FROM catalogue.supplier_offers o
              JOIN catalogue.product_supplier_links l ON l.id=o.supplier_link_id
              JOIN catalogue.supplier_catalogue_state s
                ON s.supplier_id=l.supplier_id AND s.active_batch_id=o.snapshot_batch_id
              JOIN catalogue.supplier_import_batches b
                ON b.id=o.snapshot_batch_id AND b.snapshot_status='PROMOTED'
              WHERE o.is_active AND (o.valid_until IS NULL OR o.valid_until>NOW())
            ), elig AS (
              SELECT canonical_product_id,
                BOOL_OR(CASE WHEN availability_state='UNLIMITED' THEN TRUE
                  WHEN availability_state='FINITE' AND available_quantity >=
                       GREATEST(1,COALESCE(minimum_order_quantity,1)) THEN TRUE
                  ELSE FALSE END) AS ok,
                BOOL_OR(
                  unit_cost IN (9999,9999999)
                  OR (rrp_ex ~ '^[0-9]+(\.[0-9]+)?$'
                      AND rrp_ex::numeric IN (0,9999,9999999))
                ) AS price_sentinel,
                BOOL_OR(
                  rrp_ex ~ '^[0-9]+(\.[0-9]+)?$' AND rrp_ex::numeric > 0
                  AND rrp_ex::numeric NOT IN (9999,9999999)
                  AND unit_cost NOT IN (9999,9999999)
                  AND unit_cost > rrp_ex::numeric
                ) AS below_cost
              FROM act GROUP BY canonical_product_id
            ), pub AS (
              SELECT product_id::text pid, BOOL_OR(is_published) pub,
                     BOOL_OR(visible_in_listings) vis
              FROM public.product_productchannellisting WHERE channel_id=1
              GROUP BY product_id
            ), buy AS (
              SELECT variant_id::text vid, BOOL_OR(is_available_for_purchase) buy
              FROM public.product_productvariantchannellisting WHERE channel_id=1
              GROUP BY variant_id
            ), last_action AS (
              SELECT DISTINCT ON (canonical_product_id)
                     canonical_product_id, action
              FROM catalogue.visibility_actions
              ORDER BY canonical_product_id, created_at DESC, id DESC
            )
            SELECT cp.id::text, cp.saleor_product_id, cp.saleor_variant_id
            FROM catalogue.canonical_products cp
            JOIN elig e ON e.canonical_product_id=cp.id AND e.ok
                       AND NOT e.price_sentinel AND NOT e.below_cost
            JOIN last_action la ON la.canonical_product_id=cp.id
                               AND la.action='WITHDRAW'
            LEFT JOIN pub ON pub.pid=cp.saleor_product_id
            LEFT JOIN buy ON buy.vid=cp.saleor_variant_id
            WHERE NOT (COALESCE(pub.pub,FALSE) AND COALESCE(pub.vis,FALSE)
                       AND COALESCE(buy.buy,FALSE))
            ORDER BY cp.id
            """)
            return [{"canonical_product_id": r[0], "saleor_product_id": r[1],
                     "saleor_variant_id": r[2]} for r in cur.fetchall()]


def apply_reactivation(connection, targets, *, confirm=False, max_changes=5000,
                       chunk_size=500):
    if not confirm:
        return {"applied": False, "error": "Write refused: pass confirm=True"}
    if not targets:
        return {"applied": True, "chunks": 0, "products": 0, "variants": 0}
    if len(targets) > int(max_changes):
        return {"applied": False, "error": "Scope exceeds max_changes",
                "would_change": len(targets)}
    from django.db import transaction
    products = variants = chunks = 0
    for start in range(0, len(targets), int(chunk_size)):
        batch = targets[start:start+int(chunk_size)]
        pids = [t["saleor_product_id"] for t in batch if t["saleor_product_id"]]
        vids = [t["saleor_variant_id"] for t in batch if t["saleor_variant_id"]]
        with transaction.atomic():
            with connection.cursor() as cur:
                cur.execute("SET LOCAL lock_timeout='15s'")
                if pids:
                    cur.execute("""
                        UPDATE public.product_productchannellisting
                        SET is_published=TRUE, visible_in_listings=TRUE
                        WHERE channel_id=1 AND product_id::text = ANY(%s)
                          AND (NOT is_published OR NOT visible_in_listings)
                    """, (pids,))
                    products += cur.rowcount
                if vids:
                    cur.execute("""
                        UPDATE public.product_productvariantchannellisting
                        SET is_available_for_purchase=TRUE
                        WHERE channel_id=1 AND variant_id::text = ANY(%s)
                          AND NOT is_available_for_purchase
                    """, (vids,))
                    variants += cur.rowcount
                if pids:
                    cur.execute("""
                        UPDATE public.product_product SET search_index_dirty=TRUE
                        WHERE id::text = ANY(%s) AND NOT search_index_dirty
                    """, (pids,))
        chunks += 1
    _record(connection, targets, "REACTIVATE", "eligible_supply_and_valid_price")
    return {"applied": True, "chunks": chunks, "products": products,
            "variants": variants}


def plan(connection):
    """READ-ONLY combined plan for all three post-promotion phases."""
    from importer.engine.canonical import storefront_sync as ss
    from importer.engine.canonical import visibility_apply as va
    prices = ss.plan(connection)
    vis = va.plan(connection)
    react = reactivation_targets(connection)
    return {
        "mode": "READ_ONLY_PLAN",
        "ledger_present": _ledger_exists(connection),
        "price_update_count": prices["update_count"],
        "price_counts": prices["counts"],
        "withdraw_count": vis["withdraw_count"],
        "protected_checkout_count": vis["protected_checkout_count"],
        "reactivate_count": len(react),
        "reactivate_sample": react[:10],
        "saleor_written": False,
    }


def run(connection, *, confirm=False, max_price_changes=2000,
        max_visibility_changes=20000, max_reactivate=5000, progress=None):
    """Execute all three phases. Promotion is already committed by the caller."""
    # Guard FIRST — no imports, no DB access, no work without confirmation.
    if not confirm:
        return {"applied": False, "error": "Write refused: pass confirm=True"}

    from importer.engine.canonical import storefront_sync as ss
    from importer.engine.canonical import visibility_apply as va

    result = {"applied": True, "phases": {}}

    p = ss.plan(connection)
    result["phases"]["prices"] = ss.apply(
        connection, p["updates"], confirm=True, max_changes=max_price_changes)

    v = va.plan(connection)
    targets = v["actions"][va.WITHDRAW]
    outcome = va.apply_withdrawal(
        connection, targets, confirm=True, max_changes=max_visibility_changes)
    result["phases"]["withdraw"] = outcome
    if outcome.get("applied") and targets and _ledger_exists(connection):
        _record(connection, targets, "WITHDRAW", "no_eligible_supply")

    react = reactivation_targets(connection)
    result["phases"]["reactivate"] = apply_reactivation(
        connection, react, confirm=True, max_changes=max_reactivate)

    if progress:
        progress(result)
    return result
