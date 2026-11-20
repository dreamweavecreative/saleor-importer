
"""Multi-supplier landed-cost and purchasing selection engine."""

import json
from decimal import Decimal, ROUND_CEILING, InvalidOperation

from django.db import connection, transaction


D = Decimal
FOUR = D("0.0001")
SIX = D("0.000001")


def money(value):
    return D(str(value)).quantize(FOUR)


def effective_quantity(requested, moq, pack):

    quantity = max(requested, moq)

    packs = (
        quantity / pack
    ).to_integral_value(rounding=ROUND_CEILING)

    return packs * pack


class LandedCostEngine:

    @staticmethod
    def configure_supplier(
        supplier_id,
        currency,
        shipping_flat=0,
        shipping_per_unit=0,
        tax_rate=0,
        duty_rate=0,
        priority=100,
        reliability_score=50,
        is_active=True,
    ):

        supplier_id = str(supplier_id).strip().lower()
        currency = str(currency).strip().upper()

        if not supplier_id:
            raise ValueError("Supplier ID required")

        if len(currency) != 3 or not currency.isalpha():
            raise ValueError("Invalid currency")

        try:
            shipping_flat = D(str(shipping_flat))
            shipping_per_unit = D(str(shipping_per_unit))
            tax_rate = D(str(tax_rate))
            duty_rate = D(str(duty_rate))
            reliability_score = D(str(reliability_score))
        except InvalidOperation:
            raise ValueError("Invalid supplier cost configuration")

        values = [
            shipping_flat,
            shipping_per_unit,
            tax_rate,
            duty_rate,
            reliability_score,
        ]

        if not all(v.is_finite() for v in values):
            raise ValueError("Non-finite supplier configuration")

        if shipping_flat < 0 or shipping_per_unit < 0:
            raise ValueError("Shipping cannot be negative")

        if not D("0") <= tax_rate <= D("1"):
            raise ValueError("Tax rate must be between 0 and 1")

        if not D("0") <= duty_rate <= D("1"):
            raise ValueError("Duty rate must be between 0 and 1")

        if not D("0") <= reliability_score <= D("100"):
            raise ValueError("Reliability must be between 0 and 100")

        if isinstance(priority, bool) or not isinstance(priority, int):
            raise ValueError("Priority must be an integer")

        if priority < 0:
            raise ValueError("Priority cannot be negative")

        with transaction.atomic():

            with connection.cursor() as cursor:

                cursor.execute("""
                    INSERT INTO catalogue.supplier_landed_cost_rules (
                        supplier_id,
                        currency,
                        shipping_flat,
                        shipping_per_unit,
                        tax_rate,
                        duty_rate,
                        priority,
                        reliability_score,
                        is_active,
                        updated_at
                    )
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())

                    ON CONFLICT (supplier_id)
                    DO UPDATE SET

                        currency = EXCLUDED.currency,
                        shipping_flat = EXCLUDED.shipping_flat,
                        shipping_per_unit = EXCLUDED.shipping_per_unit,
                        tax_rate = EXCLUDED.tax_rate,
                        duty_rate = EXCLUDED.duty_rate,
                        priority = EXCLUDED.priority,
                        reliability_score =
                            EXCLUDED.reliability_score,
                        is_active = EXCLUDED.is_active,
                        updated_at = NOW()
                """, [
                    supplier_id,
                    currency,
                    shipping_flat,
                    shipping_per_unit,
                    tax_rate,
                    duty_rate,
                    priority,
                    reliability_score,
                    is_active,
                ])

        return True

    @staticmethod
    def calculate(
        unit_cost,
        quantity,
        shipping_flat=0,
        shipping_per_unit=0,
        tax_rate=0,
        duty_rate=0,
        tax_inclusive=False,
    ):

        unit_cost = D(str(unit_cost))
        quantity = D(str(quantity))

        goods = unit_cost * quantity

        shipping = (
            D(str(shipping_flat))
            + D(str(shipping_per_unit)) * quantity
        )

        duty = goods * D(str(duty_rate))

        # Avoid charging tax twice when supplier pricing
        # explicitly states that the price includes tax.

        tax = (
            D("0")
            if tax_inclusive
            else (goods + shipping + duty) * D(str(tax_rate))
        )

        total = goods + shipping + duty + tax

        return {
            "goods_cost": money(goods),
            "shipping_cost": money(shipping),
            "duty_cost": money(duty),
            "tax_cost": money(tax),
            "landed_total": money(total),
            "landed_unit_cost": (
                total / quantity
            ).quantize(SIX),
            "quantity": quantity,
        }

    @staticmethod
    def select_best_offer(
        canonical_product_id,
        requested_quantity,
        currency="AUD",
        allow_unknown_stock=False,
    ):

        requested = D(str(requested_quantity))
        currency = str(currency).upper().strip()

        if not requested.is_finite() or requested <= 0:
            raise ValueError("Requested quantity must be positive")

        if len(currency) != 3 or not currency.isalpha():
            raise ValueError("Invalid target currency")

        eligible = []
        rejected = []

        with transaction.atomic():

            with connection.cursor() as cursor:

                # Verify canonical product exists.

                cursor.execute("""
                    SELECT id
                    FROM catalogue.canonical_products
                    WHERE id = %s
                    FOR SHARE
                """, [str(canonical_product_id)])

                if cursor.fetchone() is None:
                    raise ValueError("Canonical product not found")

                cursor.execute("""
                    SELECT
                        o.id AS offer_id,
                        o.supplier_link_id,
                        o.unit_cost,
                        o.currency,
                        o.tax_inclusive,
                        o.available_quantity,
                        o.minimum_order_quantity,
                        o.pack_quantity,
                        o.lead_time_days,
                        o.valid_until,
                        o.is_active,

                        l.supplier_id,
                        l.supplier_sku,

                        r.shipping_flat,
                        r.shipping_per_unit,
                        r.tax_rate,
                        r.duty_rate,
                        r.priority,
                        r.reliability_score

                    FROM catalogue.supplier_offers o

                    JOIN catalogue.product_supplier_links l
                      ON l.id = o.supplier_link_id

                    JOIN catalogue.supplier_landed_cost_rules r
                      ON r.supplier_id = l.supplier_id

                    WHERE l.canonical_product_id = %s
                      AND o.is_active = TRUE
                      AND r.is_active = TRUE
                      AND o.currency = %s
                      AND r.currency = %s

                      AND (
                          o.valid_until IS NULL
                          OR o.valid_until > NOW()
                      )

                    ORDER BY l.supplier_id
                """, [
                    str(canonical_product_id),
                    currency,
                    currency,
                ])

                columns = [
                    column.name
                    for column in cursor.description
                ]

                rows = [
                    dict(zip(columns, row))
                    for row in cursor.fetchall()
                ]

                for row in rows:

                    stock = row["available_quantity"]

                    if stock is None and not allow_unknown_stock:
                        rejected.append({
                            "supplier": row["supplier_id"],
                            "reason": "UNKNOWN_STOCK",
                        })
                        continue

                    quantity = effective_quantity(
                        requested,
                        D(str(row["minimum_order_quantity"])),
                        D(str(row["pack_quantity"])),
                    )

                    if stock is not None and D(str(stock)) < quantity:
                        rejected.append({
                            "supplier": row["supplier_id"],
                            "reason": "INSUFFICIENT_STOCK",
                        })
                        continue

                    costs = LandedCostEngine.calculate(
                        unit_cost=row["unit_cost"],
                        quantity=quantity,
                        shipping_flat=row["shipping_flat"],
                        shipping_per_unit=row["shipping_per_unit"],
                        tax_rate=row["tax_rate"],
                        duty_rate=row["duty_rate"],
                        tax_inclusive=row["tax_inclusive"],
                    )

                    candidate = {
                        "supplier_id": row["supplier_id"],
                        "supplier_sku": row["supplier_sku"],
                        "supplier_link_id": row["supplier_link_id"],
                        "offer_id": row["offer_id"],
                        "priority": row["priority"],
                        "reliability_score": row["reliability_score"],
                        "lead_time_days": row["lead_time_days"],
                        **costs,
                    }

                    eligible.append(candidate)

                # --- 8.16 diagnostics: classify offers excluded pre-evaluation ---
                seen_offer_ids = {row["offer_id"] for row in rows}
                cursor.execute("""
                    SELECT
                        o.id AS offer_id,
                        l.supplier_id,
                        o.currency AS offer_currency,
                        o.is_active AS offer_active,
                        (o.valid_until IS NOT NULL AND o.valid_until <= NOW()) AS offer_expired,
                        r.supplier_id AS rule_supplier,
                        r.currency AS rule_currency,
                        r.is_active AS rule_active
                    FROM catalogue.supplier_offers o
                    JOIN catalogue.product_supplier_links l
                      ON l.id = o.supplier_link_id
                    LEFT JOIN catalogue.supplier_landed_cost_rules r
                      ON r.supplier_id = l.supplier_id
                    WHERE l.canonical_product_id = %s
                    ORDER BY l.supplier_id, o.id, r.is_active DESC NULLS LAST
                """, [str(canonical_product_id)])
                diag_cols = [c.name for c in cursor.description]
                diag_seen = set()
                for drow in cursor.fetchall():
                    d = dict(zip(diag_cols, drow))
                    oid = d["offer_id"]
                    if oid in seen_offer_ids or oid in diag_seen:
                        continue
                    if d["rule_supplier"] is None:
                        reason = "SUPPLIER_NOT_CONFIGURED"
                    elif d["rule_active"] is not True:
                        reason = "SUPPLIER_RULE_INACTIVE"
                    elif d["offer_active"] is not True:
                        reason = "OFFER_INACTIVE"
                    elif d["offer_expired"]:
                        reason = "OFFER_EXPIRED"
                    elif str(d["offer_currency"]).upper().strip() != currency:
                        reason = "OFFER_CURRENCY_MISMATCH"
                    elif str(d["rule_currency"]).upper().strip() != currency:
                        reason = "RULE_CURRENCY_MISMATCH"
                    else:
                        reason = "EXCLUDED_UNCLASSIFIED"
                    diag_seen.add(oid)
                    rejected.append({
                        "supplier": d["supplier_id"],
                        "reason": reason,
                        "offer_id": oid,
                    })

                # Lowest landed unit cost wins.
                # Total commitment is the secondary cost criterion.
                # Reliability and priority resolve further ties.

                eligible.sort(
                    key=lambda item: (
                        item["landed_unit_cost"],
                        item["landed_total"],
                        -D(str(item["reliability_score"])),
                        item["priority"],
                        item["supplier_id"],
                    )
                )

                selected = eligible[0] if eligible else None

                details = {
                    "requested_quantity": str(requested),
                    "currency": currency,
                    "eligible_count": len(eligible),
                    "rejected": rejected,
                    "candidates": eligible,
                    "selection_method": "LANDED_UNIT_COST",
                }

                cursor.execute("""
                    INSERT INTO catalogue.supplier_selection_history (
                        canonical_product_id,
                        selected_supplier_link_id,
                        requested_quantity,
                        selected_quantity,
                        currency,
                        landed_total,
                        landed_unit_cost,
                        decision_status,
                        details
                    )
                    VALUES (
                        %s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb
                    )
                    RETURNING id
                """, [
                    str(canonical_product_id),
                    selected["supplier_link_id"] if selected else None,
                    requested,
                    selected["quantity"] if selected else None,
                    currency,
                    selected["landed_total"] if selected else None,
                    selected["landed_unit_cost"] if selected else None,
                    "SELECTED" if selected else "NO_ELIGIBLE_SUPPLIER",
                    json.dumps(details, default=str),
                ])

                history_id = cursor.fetchone()[0]

                return {
                    "status": (
                        "SELECTED"
                        if selected
                        else "NO_ELIGIBLE_SUPPLIER"
                    ),
                    "selection_id": history_id,
                    "selected": selected,
                    "eligible_offers": eligible,
                    "rejected_offers": rejected,
                    "currency": currency,
                }

    @staticmethod
    def get_selection_history(canonical_product_id, limit=20):

        if isinstance(limit, bool) or not isinstance(limit, int):
            raise ValueError("Invalid history limit")

        limit = max(1, min(limit, 100))

        with connection.cursor() as cursor:

            cursor.execute("""
                SELECT
                    id,
                    selected_supplier_link_id,
                    requested_quantity,
                    selected_quantity,
                    currency,
                    landed_total,
                    landed_unit_cost,
                    decision_status,
                    details,
                    created_at

                FROM catalogue.supplier_selection_history

                WHERE canonical_product_id = %s

                ORDER BY created_at DESC, id DESC

                LIMIT %s
            """, [
                str(canonical_product_id),
                limit,
            ])

            columns = [
                column.name
                for column in cursor.description
            ]

            return [
                dict(zip(columns, row))
                for row in cursor.fetchall()
            ]
