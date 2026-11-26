"""Active-snapshot offer reads and cross-supplier visibility.

Every read joins the supplier active pointer. Offers from unpromoted or
superseded generations are never returned. No warehouse stock is read as
supplier availability, and no warehouse stock is ever written here.
"""

from .availability import purchase_eligible

ACTIVE_JOIN = """
    JOIN catalogue.product_supplier_links l ON l.id = o.supplier_link_id
    JOIN catalogue.supplier_catalogue_state s
      ON s.supplier_id = l.supplier_id
     AND s.active_batch_id = o.snapshot_batch_id
    JOIN catalogue.supplier_import_batches b
      ON b.id = o.snapshot_batch_id
     AND b.snapshot_status = 'PROMOTED'
"""

ACTIVE_WHERE = """
    o.is_active = TRUE
    AND (o.valid_until IS NULL OR o.valid_until > NOW())
"""


def _rows(cur):
    names = [c[0] for c in cur.description]
    return [dict(zip(names, r)) for r in cur.fetchall()]


def active_offers(connection, canonical_product_id=None, supplier_id=None):
    sql = ("SELECT o.*, l.supplier_id, l.supplier_sku, l.canonical_product_id "
           "FROM catalogue.supplier_offers o " + ACTIVE_JOIN +
           " WHERE " + ACTIVE_WHERE)
    params = []
    if canonical_product_id is not None:
        sql += " AND l.canonical_product_id = %s"
        params.append(str(canonical_product_id))
    if supplier_id is not None:
        sql += " AND l.supplier_id = %s"
        params.append(str(supplier_id).strip().lower())
    sql += " ORDER BY l.supplier_id, o.unit_cost ASC"
    with connection.cursor() as cur:
        cur.execute(sql, params)
        return _rows(cur)


def eligible_offers(connection, canonical_product_id, requested=1,
                    allow_unknown=False):
    """Availability-aware eligibility. UNKNOWN is excluded unless opted in."""
    eligible, excluded = [], []
    for offer in active_offers(connection, canonical_product_id):
        ok, reason = purchase_eligible(
            offer.get("availability_state"),
            offer.get("available_quantity"),
            requested=requested,
            moq=offer.get("minimum_order_quantity") or 1,
            pack=offer.get("pack_quantity") or 1,
            allow_unknown=allow_unknown,
        )
        if ok:
            eligible.append(dict(offer, eligibility_reason=reason))
        else:
            excluded.append({"supplier_id": offer.get("supplier_id"),
                             "supplier_sku": offer.get("supplier_sku"),
                             "offer_id": offer.get("id"),
                             "reason": reason})
    return {"eligible": eligible, "excluded": excluded}


def supplier_snapshot_status(connection):
    with connection.cursor() as cur:
        cur.execute("""
            SELECT s.supplier_id, s.active_batch_id, s.previous_batch_id,
                   s.promoted_at, b.total_rows, b.feed_kind, b.snapshot_status,
                   b.validation_summary
            FROM catalogue.supplier_catalogue_state s
            LEFT JOIN catalogue.supplier_import_batches b ON b.id = s.active_batch_id
            ORDER BY s.supplier_id
        """)
        return _rows(cur)


def visibility_report(connection, allow_unknown=False, limit=None):
    """DRY RUN. Aggregate eligibility across ALL suppliers. No writes."""
    with connection.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT l.canonical_product_id
            FROM catalogue.supplier_offers o
        """ + ACTIVE_JOIN + """
            WHERE """ + ACTIVE_WHERE + """
            ORDER BY l.canonical_product_id
        """)
        ids = [r[0] for r in cur.fetchall()]
    if limit:
        ids = ids[:int(limit)]
    eligible_ids, ineligible = [], []
    reasons = {}
    for cpid in ids:
        result = eligible_offers(connection, cpid, allow_unknown=allow_unknown)
        if result["eligible"]:
            eligible_ids.append(str(cpid))
        else:
            ineligible.append(str(cpid))
            for item in result["excluded"]:
                reasons[item["reason"]] = reasons.get(item["reason"], 0) + 1
    return {
        "mode": "DRY_RUN",
        "allow_unknown_stock": bool(allow_unknown),
        "canonical_products_evaluated": len(ids),
        "eligible_products": len(eligible_ids),
        "no_eligible_offer_products": len(ineligible),
        "exclusion_reasons": reasons,
        "eligible_product_ids": eligible_ids[:50],
        "no_eligible_offer_product_ids": ineligible[:50],
        "saleor_publication_changed": False,
        "warehouse_inventory_changed": False,
        "products_deleted": False,
    }


def apply_purchase_eligibility(connection, *, confirm=False, max_changes=500,
                               allow_unknown=False):
    """Withdraw purchase eligibility ONLY where no supplier can supply.

    Never deletes products/variants. Never writes warehouse stock.
    Never touches another supplier's offers. Requires explicit confirm.
    """
    if not confirm:
        return {"applied": False,
                "error": "Database write refused: pass confirm=True explicitly"}
    report = visibility_report(connection, allow_unknown=allow_unknown)
    targets = report["no_eligible_offer_product_ids"]
    if report["no_eligible_offer_products"] > int(max_changes):
        return {"applied": False,
                "error": "Change count exceeds max_changes circuit breaker",
                "would_change": report["no_eligible_offer_products"],
                "max_changes": int(max_changes)}
    changed = 0
    with connection.cursor() as cur:
        for cpid in targets:
            cur.execute("""
                SELECT saleor_variant_id FROM catalogue.canonical_products
                WHERE id = %s AND saleor_variant_id IS NOT NULL
            """, (cpid,))
            row = cur.fetchone()
            if not row:
                continue
            cur.execute("""
                UPDATE public.product_productvariantchannellisting
                SET is_available_for_purchase = FALSE
                WHERE variant_id = %s::integer
                  AND is_available_for_purchase = TRUE
            """, (str(row[0]),))
            changed += cur.rowcount
    return {"applied": True, "variant_listings_withdrawn": changed,
            "products_deleted": 0, "warehouse_stock_written": 0,
            "supplier_offers_modified": 0}
