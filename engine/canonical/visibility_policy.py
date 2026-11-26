"""Publication and purchase-eligibility policy across ALL suppliers.

Safety asymmetry, deliberate:
  * WITHDRAWAL is automatic. If no supplier can supply and there is no owned
    stock, customers must not be able to buy.
  * PUBLICATION requires explicit intent (reactivate=True). We never
    force-publish a product an operator deliberately unpublished.

Never deletes products or variants. Never writes warehouse stock.
"""

WITHDRAW = "WITHDRAW"
REACTIVATE = "REACTIVATE"
UNCHANGED = "UNCHANGED"


def decide(has_eligible_offer, has_owned_stock, is_published,
           visible_in_listings, is_available_for_purchase,
           reactivate=False):
    """Pure policy decision. Returns action + target flags + reason."""
    supply = bool(has_eligible_offer) or bool(has_owned_stock)
    if not supply:
        if is_published or visible_in_listings or is_available_for_purchase:
            return {"action": WITHDRAW, "reason": "NO_ELIGIBLE_SUPPLY",
                    "is_published": False, "visible_in_listings": False,
                    "is_available_for_purchase": False}
        return {"action": UNCHANGED, "reason": "ALREADY_WITHDRAWN"}
    if reactivate and not (is_published and visible_in_listings
                           and is_available_for_purchase):
        return {"action": REACTIVATE, "reason": "SUPPLY_AVAILABLE",
                "is_published": True, "visible_in_listings": True,
                "is_available_for_purchase": True}
    return {"action": UNCHANGED, "reason": "SUPPLY_AVAILABLE_NO_INTENT"}


def summarise(decisions):
    """Pure aggregation for reporting."""
    out = {"WITHDRAW": 0, "REACTIVATE": 0, "UNCHANGED": 0}
    reasons = {}
    for d in decisions:
        out[d["action"]] = out.get(d["action"], 0) + 1
        reasons[d["reason"]] = reasons.get(d["reason"], 0) + 1
    return {"counts": out, "reasons": reasons, "evaluated": len(decisions)}


def evaluate(connection, allow_unknown=False, limit=None):
    """READ-ONLY. Aggregate active supply per canonical product, then decide."""
    from .active_catalogue import eligible_offers
    with connection.cursor() as cur:
        cur.execute("""
            SELECT cp.id::text, cp.saleor_product_id, cp.saleor_variant_id
            FROM catalogue.canonical_products cp
            WHERE cp.saleor_variant_id IS NOT NULL
            ORDER BY cp.id
        """)
        rows = cur.fetchall()
        if limit:
            rows = rows[:int(limit)]
        cur.execute("""
            SELECT product_variant_id, SUM(quantity - quantity_allocated)
            FROM public.warehouse_stock GROUP BY product_variant_id
        """)
        owned = {str(r[0]): int(r[1] or 0) for r in cur.fetchall()}
        cur.execute("""
            SELECT v.id::text, l.is_published, l.visible_in_listings
            FROM public.product_productvariant v
            JOIN public.product_productchannellisting l ON l.product_id=v.product_id
        """)
        pub = {str(r[0]): (bool(r[1]), bool(r[2])) for r in cur.fetchall()}
        cur.execute("""
            SELECT variant_id::text, is_available_for_purchase
            FROM public.product_productvariantchannellisting
        """)
        buy = {str(r[0]): bool(r[1]) for r in cur.fetchall()}
    decisions = []
    for cid, spid, svid in rows:
        res = eligible_offers(connection, cid, allow_unknown=allow_unknown)
        p, v = pub.get(str(svid), (False, False))
        d = decide(bool(res["eligible"]), owned.get(str(svid), 0) > 0,
                   p, v, buy.get(str(svid), False))
        d.update({"canonical_product_id": cid, "saleor_product_id": spid,
                  "saleor_variant_id": svid})
        decisions.append(d)
    return {"mode": "DRY_RUN", "allow_unknown_stock": bool(allow_unknown),
            **summarise(decisions), "decisions": decisions,
            "products_deleted": False, "warehouse_stock_written": False}


def apply(connection, decisions, confirm=False, max_changes=500,
          actions=(WITHDRAW,)):
    """Apply only the requested actions. Circuit breaker on change volume."""
    if not confirm:
        return {"applied": False,
                "error": "Database write refused: pass confirm=True explicitly"}
    targets = [d for d in decisions if d["action"] in actions]
    if len(targets) > int(max_changes):
        return {"applied": False, "error": "Change count exceeds max_changes",
                "would_change": len(targets), "max_changes": int(max_changes)}
    changed_products = changed_variants = 0
    with connection.cursor() as cur:
        for d in targets:
            cur.execute("""
                UPDATE public.product_productchannellisting
                SET is_published=%s, visible_in_listings=%s
                WHERE product_id=%s::integer
                  AND (is_published IS DISTINCT FROM %s
                       OR visible_in_listings IS DISTINCT FROM %s)
            """, (d["is_published"], d["visible_in_listings"],
                  d["saleor_product_id"], d["is_published"],
                  d["visible_in_listings"]))
            changed_products += cur.rowcount
            cur.execute("""
                UPDATE public.product_productvariantchannellisting
                SET is_available_for_purchase=%s
                WHERE variant_id=%s::integer
                  AND is_available_for_purchase IS DISTINCT FROM %s
            """, (d["is_available_for_purchase"], d["saleor_variant_id"],
                  d["is_available_for_purchase"]))
            changed_variants += cur.rowcount
    return {"applied": True, "actions_applied": list(actions),
            "product_listings_changed": changed_products,
            "variant_listings_changed": changed_variants,
            "products_deleted": 0, "warehouse_stock_written": 0,
            "supplier_offers_modified": 0}
