"""Supply-gated image enrichment runner.

Only products with eligible active supply are enriched. Digital/service items
receive a generated branded card with zero provider spend. Dry-run by default.

Reuses the existing discovery waterfall and attachment modules; it does not
reimplement identity verification or weaken the acceptance threshold.
"""
import logging

logger = logging.getLogger(__name__)


def plan(connection, limit=50, allow_unknown=False, force=False):
    """READ-ONLY plan: what would be sourced, carded, or skipped."""
    from importer.engine.canonical import enrichment_policy as ep
    from importer.engine.canonical.active_catalogue import eligible_offers
    with connection.cursor() as cur:
        cur.execute("""
            SELECT cp.id::text, cp.saleor_product_id, cp.saleor_variant_id
            FROM catalogue.canonical_products cp
            WHERE cp.saleor_product_id IS NOT NULL
            ORDER BY cp.id LIMIT %s
        """, (int(limit),))
        rows = cur.fetchall()
        decisions = []
        for cid, spid, svid in rows:
            cur.execute("""
                SELECT p.name, p.metadata, pt.slug,
                       EXISTS(SELECT 1 FROM public.product_productmedia m
                              WHERE m.product_id=p.id
                                AND COALESCE(m.metadata->'image_provenance'->>'status','')
                                    NOT IN ('placeholder','branded_card'))
                FROM public.product_product p
                JOIN public.product_producttype pt ON pt.id=p.product_type_id
                WHERE p.id=%s::integer
            """, (spid,))
            fact = cur.fetchone()
            if not fact:
                continue
            name, meta, slug, has_media = fact
            meta = meta if isinstance(meta, dict) else {}
            primary = (meta.get("supplier_category") or {}).get("primary")
            supply = bool(eligible_offers(connection, cid,
                                          allow_unknown=allow_unknown)["eligible"])
            d = ep.decide(supply, bool(has_media), name, primary, slug, force=force)
            d.update({"canonical_product_id": cid, "saleor_product_id": spid,
                      "name": name})
            decisions.append(d)
    return {"mode": "DRY_RUN", **ep.summarise(decisions), "decisions": decisions,
            "media_written": False}


def run(connection, limit=20, allow_unknown=False, force=False, confirm=False):
    """Execute the plan. Requires explicit confirm for any media write."""
    result = plan(connection, limit=limit, allow_unknown=allow_unknown, force=force)
    if not confirm:
        result["applied"] = False
        result["error"] = "Media write refused: pass confirm=True explicitly"
        return result
    from importer.engine.media_tasks import process_product_media_sync
    outcomes = {}
    for d in result["decisions"]:
        if d["action"] == "SKIP":
            outcomes["skipped"] = outcomes.get("skipped", 0) + 1
            continue
        try:
            r = process_product_media_sync(int(d["saleor_product_id"]),
                                           force=force,
                                           allow_search=(d["action"] == "SOURCE"))
        except Exception as exc:
            logger.warning("media failed pid=%s: %s", d["saleor_product_id"], exc)
            r = "failed"
        outcomes[str(r)] = outcomes.get(str(r), 0) + 1
    result["applied"] = True
    result["outcomes"] = outcomes
    result["media_written"] = True
    return result
