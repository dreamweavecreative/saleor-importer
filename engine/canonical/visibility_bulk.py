"""Set-based cross-supplier visibility evaluation. One SQL pass, read-only.

visibility_policy.evaluate() issues one offer query per canonical product.
That is correct but O(n) and unusable at 72,513 rows. This computes the same
decision for the whole catalogue in a single pass.

Policy matches visibility_policy.decide():
  supply = has_eligible_active_offer OR has_owned_stock
  no supply and currently sellable  -> WITHDRAW
  no supply and already withdrawn   -> UNCHANGED
  supply and reactivate=True        -> REACTIVATE
  otherwise                         -> UNCHANGED

Eligibility matches availability_policy.purchase_eligible(requested=1):
  UNLIMITED   eligible
  FINITE      eligible when quantity >= effective_quantity(1, moq, pack)
  UNKNOWN     excluded unless allow_unknown
  UNAVAILABLE never eligible

NOTE: ELIGIBLE_CASE is substituted into a CTE selecting FROM active, so its
column references are deliberately UNQUALIFIED. The active CTE owns the `o`
alias; downstream CTEs do not.

Nothing here writes to any table.
"""

ELIGIBLE_CASE = """
    CASE
      WHEN availability_state = 'UNLIMITED' THEN TRUE
      WHEN availability_state = 'UNKNOWN' AND %(allow_unknown)s THEN TRUE
      WHEN availability_state = 'FINITE'
           AND available_quantity IS NOT NULL
           AND available_quantity >= (
                 CEIL(GREATEST(1, COALESCE(minimum_order_quantity, 1))
                      / GREATEST(1, COALESCE(pack_quantity, 1)))
                 * GREATEST(1, COALESCE(pack_quantity, 1))
               )
           THEN TRUE
      ELSE FALSE
    END
"""

ACTIVE_CTE = """
active AS (
    SELECT l.canonical_product_id,
           o.availability_state,
           o.available_quantity,
           o.minimum_order_quantity,
           o.pack_quantity
    FROM catalogue.supplier_offers o
    JOIN catalogue.product_supplier_links l ON l.id = o.supplier_link_id
    JOIN catalogue.supplier_catalogue_state s
      ON s.supplier_id = l.supplier_id
     AND s.active_batch_id = o.snapshot_batch_id
    JOIN catalogue.supplier_import_batches b
      ON b.id = o.snapshot_batch_id
     AND b.snapshot_status = 'PROMOTED'
    WHERE o.is_active = TRUE
      AND (o.valid_until IS NULL OR o.valid_until > NOW())
),
supply AS (
    SELECT canonical_product_id,
           BOOL_OR(""" + ELIGIBLE_CASE + """) AS has_eligible
    FROM active
    GROUP BY canonical_product_id
),
owned AS (
    SELECT cp.id AS canonical_product_id,
           SUM(GREATEST(0, ws.quantity - ws.quantity_allocated)) > 0 AS has_owned
    FROM catalogue.canonical_products cp
    JOIN public.warehouse_stock ws
      ON ws.product_variant_id::text = cp.saleor_variant_id
    GROUP BY cp.id
)
"""

REPORT_SQL = """
WITH """ + ACTIVE_CTE + """,
pub AS (
    SELECT cp.id AS canonical_product_id,
           BOOL_OR(COALESCE(pl.is_published, FALSE)) AS is_published,
           BOOL_OR(COALESCE(pl.visible_in_listings, FALSE)) AS visible
    FROM catalogue.canonical_products cp
    LEFT JOIN public.product_productchannellisting pl
           ON pl.product_id::text = cp.saleor_product_id
    GROUP BY cp.id
),
buy AS (
    SELECT cp.id AS canonical_product_id,
           BOOL_OR(COALESCE(vl.is_available_for_purchase, FALSE)) AS purchasable
    FROM catalogue.canonical_products cp
    LEFT JOIN public.product_productvariantchannellisting vl
           ON vl.variant_id::text = cp.saleor_variant_id
    GROUP BY cp.id
),
decided AS (
    SELECT cp.id::text AS canonical_product_id,
           COALESCE(supply.has_eligible, FALSE) AS has_eligible,
           COALESCE(owned.has_owned, FALSE) AS has_owned,
           COALESCE(pub.is_published, FALSE) AS is_published,
           COALESCE(pub.visible, FALSE) AS visible,
           COALESCE(buy.purchasable, FALSE) AS purchasable,
           (COALESCE(supply.has_eligible, FALSE)
            OR COALESCE(owned.has_owned, FALSE)) AS has_supply
    FROM catalogue.canonical_products cp
    LEFT JOIN supply ON supply.canonical_product_id = cp.id
    LEFT JOIN owned  ON owned.canonical_product_id = cp.id
    LEFT JOIN pub    ON pub.canonical_product_id = cp.id
    LEFT JOIN buy    ON buy.canonical_product_id = cp.id
)
SELECT
    COUNT(*) AS canonical_products,
    COUNT(*) FILTER (WHERE has_eligible) AS with_eligible_offer,
    COUNT(*) FILTER (WHERE has_owned) AS with_owned_stock,
    COUNT(*) FILTER (WHERE has_supply) AS with_supply,
    COUNT(*) FILTER (WHERE NOT has_supply) AS no_supply,
    COUNT(*) FILTER (WHERE NOT has_supply
                     AND (is_published OR visible OR purchasable)) AS withdraw,
    COUNT(*) FILTER (WHERE NOT has_supply
                     AND NOT (is_published OR visible OR purchasable))
        AS already_withdrawn,
    COUNT(*) FILTER (WHERE has_supply
                     AND NOT (is_published AND visible AND purchasable))
        AS reactivate_candidates,
    COUNT(*) FILTER (WHERE NOT has_eligible AND has_owned) AS owned_stock_only
FROM decided
"""

SAMPLE_SQL = """
WITH """ + ACTIVE_CTE + """
SELECT cp.canonical_name, cp.saleor_product_id, cp.saleor_variant_id,
       v.sku, pt.slug AS product_type,
       COALESCE(supply.has_eligible, FALSE) AS has_eligible,
       COALESCE(owned.has_owned, FALSE) AS has_owned
FROM catalogue.canonical_products cp
LEFT JOIN supply ON supply.canonical_product_id = cp.id
LEFT JOIN owned  ON owned.canonical_product_id = cp.id
LEFT JOIN public.product_productvariant v ON v.id::text = cp.saleor_variant_id
LEFT JOIN public.product_product p ON p.id = v.product_id
LEFT JOIN public.product_producttype pt ON pt.id = p.product_type_id
WHERE NOT (COALESCE(supply.has_eligible, FALSE)
           OR COALESCE(owned.has_owned, FALSE))
ORDER BY cp.canonical_name
LIMIT %(sample)s
"""

REASON_SQL = """
SELECT o.availability_state, COUNT(*) AS offers
FROM catalogue.supplier_offers o
JOIN catalogue.product_supplier_links l ON l.id = o.supplier_link_id
JOIN catalogue.supplier_catalogue_state s
  ON s.supplier_id = l.supplier_id AND s.active_batch_id = o.snapshot_batch_id
JOIN catalogue.supplier_import_batches b
  ON b.id = o.snapshot_batch_id AND b.snapshot_status = 'PROMOTED'
WHERE o.is_active = TRUE
  AND (o.valid_until IS NULL OR o.valid_until > NOW())
GROUP BY o.availability_state ORDER BY o.availability_state
"""


def report(connection, allow_unknown=False, sample=25):
    """READ-ONLY whole-catalogue visibility report."""
    from django.db import transaction
    with transaction.atomic():
        with connection.cursor() as cur:
            cur.execute("SET TRANSACTION READ ONLY")
            cur.execute(REPORT_SQL, {"allow_unknown": bool(allow_unknown)})
            names = [c[0] for c in cur.description]
            summary = dict(zip(names, cur.fetchone()))
            cur.execute(REASON_SQL)
            states = {r[0]: r[1] for r in cur.fetchall()}
            cur.execute(SAMPLE_SQL, {"allow_unknown": bool(allow_unknown),
                                     "sample": int(sample)})
            cols = [c[0] for c in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    summary["mode"] = "READ_ONLY"
    summary["allow_unknown_stock"] = bool(allow_unknown)
    summary["active_offer_states"] = states
    summary["withdraw_samples"] = rows
    summary["saleor_publication_changed"] = False
    summary["warehouse_stock_written"] = False
    summary["products_deleted"] = False
    return summary
