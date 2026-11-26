
"""Persistent supplier offer repository."""

import json
import re
from decimal import Decimal, InvalidOperation

from django.db import connection, transaction


class SupplierOfferRepository:

    @staticmethod
    def upsert(
        supplier_link_id,
        unit_cost,
        currency,
        tax_inclusive=False,
        available_quantity=None,
        minimum_order_quantity=1,
        pack_quantity=1,
        lead_time_days=None,
        supplier_product_url=None,
        source_updated_at=None,
        valid_until=None,
        raw_data=None,
    ):

        try:
            cost = Decimal(str(unit_cost))
            moq = Decimal(str(minimum_order_quantity))
            pack = Decimal(str(pack_quantity))

            quantity = (
                Decimal(str(available_quantity))
                if available_quantity is not None
                else None
            )

        except (InvalidOperation, ValueError):
            raise ValueError("Invalid supplier offer numeric value")

        if not cost.is_finite() or cost < 0:
            raise ValueError("Invalid unit cost")

        if not moq.is_finite() or moq <= 0:
            raise ValueError("Invalid minimum order quantity")

        if not pack.is_finite() or pack <= 0:
            raise ValueError("Invalid pack quantity")

        if quantity is not None:
            if not quantity.is_finite() or quantity < 0:
                raise ValueError("Invalid available quantity")

        currency = str(currency).upper().strip()

        if not re.fullmatch(r"[A-Z]{3}", currency):
            raise ValueError("Invalid currency code")

        if lead_time_days is not None:
            if (
                isinstance(lead_time_days, bool)
                or not isinstance(lead_time_days, int)
                or lead_time_days < 0
            ):
                raise ValueError("Invalid supplier lead time")

        if not isinstance(tax_inclusive, bool):
            raise ValueError("tax_inclusive must be a boolean")

        try:
            if raw_data is None:
                raw_payload = {}
            elif isinstance(raw_data, str):
                # PostgreSQL JSONB may be returned as serialized JSON text.
                # Decode it before json.dumps(), otherwise JSON objects become
                # JSON string scalars when stored in the JSONB column.
                raw_payload = json.loads(raw_data)
            else:
                raw_payload = raw_data

            if not isinstance(raw_payload, dict):
                raise ValueError("Supplier offer raw data must be a JSON object")

            payload = json.dumps(raw_payload, default=str)
        except (TypeError, ValueError):
            raise ValueError("Invalid supplier offer raw data; expected a JSON object") from None

        with transaction.atomic():

            with connection.cursor() as cursor:

                cursor.execute("""
                    INSERT INTO catalogue.supplier_offers (
                        supplier_link_id,
                        unit_cost,
                        currency,
                        tax_inclusive,
                        available_quantity,
                        minimum_order_quantity,
                        pack_quantity,
                        lead_time_days,
                        supplier_product_url,
                        source_updated_at,
                        valid_until,
                        raw_data,
                        observed_at,
                        updated_at
                    )
                    VALUES (
                        %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s,
                        %s::jsonb, NOW(), NOW()
                    )
                    ON CONFLICT (supplier_link_id)
                    DO UPDATE SET

                        unit_cost = EXCLUDED.unit_cost,
                        currency = EXCLUDED.currency,
                        tax_inclusive = EXCLUDED.tax_inclusive,
                        available_quantity = EXCLUDED.available_quantity,
                        minimum_order_quantity =
                            EXCLUDED.minimum_order_quantity,
                        pack_quantity = EXCLUDED.pack_quantity,
                        lead_time_days = EXCLUDED.lead_time_days,
                        supplier_product_url =
                            EXCLUDED.supplier_product_url,
                        source_updated_at =
                            EXCLUDED.source_updated_at,
                        valid_until = EXCLUDED.valid_until,
                        raw_data = EXCLUDED.raw_data,
                        observed_at = NOW(),
                        updated_at = NOW()

                    RETURNING id
                """, [
                    supplier_link_id,
                    cost,
                    currency,
                    tax_inclusive,
                    quantity,
                    moq,
                    pack,
                    lead_time_days,
                    supplier_product_url,
                    source_updated_at,
                    valid_until,
                    payload,
                ])

                return cursor.fetchone()[0]

    @staticmethod
    def get_by_supplier_link(supplier_link_id):

        with connection.cursor() as cursor:

            cursor.execute("""
                SELECT
                    o.*,
                    l.canonical_product_id,
                    l.supplier_id,
                    l.supplier_sku

                FROM catalogue.supplier_offers o

                JOIN catalogue.product_supplier_links l
                  ON l.id = o.supplier_link_id

                WHERE o.supplier_link_id = %s
            """, [supplier_link_id])

            row = cursor.fetchone()

            if row is None:
                return None

            columns = [
                column.name
                for column in cursor.description
            ]

            return dict(zip(columns, row))

    @staticmethod
    def deactivate(supplier_link_id):

        with connection.cursor() as cursor:

            cursor.execute("""
                UPDATE catalogue.supplier_offers
                SET
                    is_active = FALSE,
                    updated_at = NOW()

                WHERE supplier_link_id = %s

                RETURNING id
            """, [supplier_link_id])

            result = cursor.fetchone()

            return result[0] if result else None

    @staticmethod
    def get_active_offers(canonical_product_id):

        with connection.cursor() as cursor:

            cursor.execute("""
                SELECT
                    o.*,
                    l.supplier_id,
                    l.supplier_sku

                FROM catalogue.supplier_offers o

                JOIN catalogue.product_supplier_links l
                  ON l.id = o.supplier_link_id

                WHERE l.canonical_product_id = %s
                  AND o.is_active = TRUE

                  AND (
                      o.valid_until IS NULL
                      OR o.valid_until > NOW()
                  )

                ORDER BY o.unit_cost ASC
            """, [str(canonical_product_id)])

            columns = [
                column.name
                for column in cursor.description
            ]

            return [
                dict(zip(columns, row))
                for row in cursor.fetchall()
            ]
