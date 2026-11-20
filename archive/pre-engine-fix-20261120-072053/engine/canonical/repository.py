"""Persistent canonical catalogue repository."""

from django.db import connection, transaction


class CanonicalRepository:

    @staticmethod
    def create_product(
        name,
        manufacturer=None,
        brand=None,
        description=None,
    ):
        if not name or not str(name).strip():
            raise ValueError("Canonical product name is required")

        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("""
                    INSERT INTO catalogue.canonical_products (
                        canonical_name,
                        manufacturer,
                        brand,
                        description
                    )
                    VALUES (%s, %s, %s, %s)
                    RETURNING id
                """, [
                    str(name).strip(),
                    manufacturer,
                    brand,
                    description,
                ])

                return cursor.fetchone()[0]

    @staticmethod
    def get_product(product_id):
        with connection.cursor() as cursor:
            cursor.execute("""
                SELECT *
                FROM catalogue.canonical_products
                WHERE id = %s
            """, [str(product_id)])

            row = cursor.fetchone()

            if row is None:
                return None

            columns = [
                column.name
                for column in cursor.description
            ]

            return dict(zip(columns, row))

    @staticmethod
    def add_identifier(
        product_id,
        identifier_type,
        value,
        namespace="",
        source=None,
    ):
        allowed = {
            "GTIN",
            "EAN",
            "UPC",
            "MPN",
            "MANUFACTURER_SKU",
        }

        if identifier_type not in allowed:
            raise ValueError("Unsupported identifier type")

        if not value:
            raise ValueError("Identifier value is required")

        with transaction.atomic():
            with connection.cursor() as cursor:

                cursor.execute("""
                    INSERT INTO catalogue.product_identifiers (
                        canonical_product_id,
                        identifier_type,
                        normalized_value,
                        namespace,
                        source
                    )
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (
                        identifier_type,
                        namespace,
                        normalized_value
                    )
                    DO NOTHING
                    RETURNING id
                """, [
                    str(product_id),
                    identifier_type,
                    value,
                    namespace or "",
                    source,
                ])

                result = cursor.fetchone()

                if result is None:
                    cursor.execute("""
                        SELECT canonical_product_id
                        FROM catalogue.product_identifiers
                        WHERE identifier_type = %s
                          AND namespace = %s
                          AND normalized_value = %s
                    """, [
                        identifier_type,
                        namespace or "",
                        value,
                    ])

                    existing = cursor.fetchone()

                    if existing is None:
                        raise RuntimeError(
                            "Identifier insertion conflict"
                        )

                    if str(existing[0]) != str(product_id):
                        raise ValueError(
                            "Identifier already belongs to "
                            "another canonical product"
                        )

                    return None

                return result[0]

    @staticmethod
    def link_supplier(
        product_id,
        supplier_id,
        supplier_sku,
    ):
        if not supplier_id or not supplier_sku:
            raise ValueError(
                "Supplier ID and supplier SKU are required"
            )

        with transaction.atomic():
            with connection.cursor() as cursor:

                cursor.execute("""
                    INSERT INTO catalogue.product_supplier_links (
                        canonical_product_id,
                        supplier_id,
                        supplier_sku
                    )
                    VALUES (%s, %s, %s)
                    ON CONFLICT (supplier_id, supplier_sku)
                    DO NOTHING
                    RETURNING id
                """, [
                    str(product_id),
                    supplier_id.strip().lower(),
                    supplier_sku.strip().upper(),
                ])

                result = cursor.fetchone()

                if result is None:
                    cursor.execute("""
                        SELECT canonical_product_id
                        FROM catalogue.product_supplier_links
                        WHERE supplier_id = %s
                          AND supplier_sku = %s
                    """, [
                        supplier_id.strip().lower(),
                        supplier_sku.strip().upper(),
                    ])

                    existing = cursor.fetchone()

                    if existing is None or str(existing[0]) != str(product_id):
                        raise ValueError(
                            "Supplier SKU already linked to "
                            "another canonical product"
                        )

                    return None

                return result[0]

    @staticmethod
    def record_reconciliation(
        product_id,
        supplier_id,
        supplier_sku,
        matching_method,
        confidence,
        decision,
        details=None,
    ):
        import json

        with connection.cursor() as cursor:
            cursor.execute("""
                INSERT INTO catalogue.reconciliation_history (
                    canonical_product_id,
                    supplier_id,
                    supplier_sku,
                    matching_method,
                    confidence,
                    decision,
                    details
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
                RETURNING id
            """, [
                str(product_id) if product_id else None,
                supplier_id,
                supplier_sku,
                matching_method,
                confidence,
                decision,
                json.dumps(details or {}),
            ])

            return cursor.fetchone()[0]
