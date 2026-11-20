
"""Canonical category resolution and supplier mapping."""

import re
import unicodedata

from django.db import connection, transaction


def normalize_category(value):

    if not value:
        raise ValueError("Category name is required")

    value = unicodedata.normalize("NFKC", str(value))

    value = value.replace("\\", "/")

    parts = [
        " ".join(part.split()).casefold()
        for part in value.split("/")
        if part.strip()
    ]

    if not parts:
        raise ValueError("Invalid category path")

    return " / ".join(parts)


def normalize_name(value):

    return normalize_category(value).split(" / ")[-1]


class CategoryRepository:

    @staticmethod
    def create_category(
        name,
        parent_id=None,
        description=None
    ):

        normalized_name = normalize_name(name)

        with transaction.atomic():

            with connection.cursor() as cursor:

                if parent_id:

                    cursor.execute("""
                        SELECT normalized_path
                        FROM catalogue.canonical_categories
                        WHERE id = %s
                        FOR SHARE
                    """, [str(parent_id)])

                    parent = cursor.fetchone()

                    if not parent:
                        raise ValueError(
                            "Parent category does not exist"
                        )

                    normalized_path = (
                        parent[0] + " / " + normalized_name
                    )

                else:
                    normalized_path = normalized_name

                cursor.execute("""
                    INSERT INTO catalogue.canonical_categories (
                        parent_id,
                        name,
                        normalized_name,
                        normalized_path,
                        description
                    )
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (normalized_path)
                    DO NOTHING
                    RETURNING id
                """, [
                    str(parent_id) if parent_id else None,
                    str(name).strip(),
                    normalized_name,
                    normalized_path,
                    description,
                ])

                result = cursor.fetchone()

                if result:
                    return result[0]

                cursor.execute("""
                    SELECT id, parent_id
                    FROM catalogue.canonical_categories
                    WHERE normalized_path = %s
                """, [normalized_path])

                existing = cursor.fetchone()

                if not existing:
                    raise RuntimeError(
                        "Canonical category creation failed"
                    )

                if (
                    str(existing[1]) if existing[1] else None
                ) != (
                    str(parent_id) if parent_id else None
                ):
                    raise ValueError(
                        "Category hierarchy conflict"
                    )

                return existing[0]

    @staticmethod
    def get_category(category_id):

        with connection.cursor() as cursor:

            cursor.execute("""
                SELECT *
                FROM catalogue.canonical_categories
                WHERE id = %s
            """, [str(category_id)])

            row = cursor.fetchone()

            if row is None:
                return None

            return dict(zip(
                [column.name for column in cursor.description],
                row
            ))

    @staticmethod
    def find_by_path(path):

        normalized = normalize_category(path)

        with connection.cursor() as cursor:

            cursor.execute("""
                SELECT id
                FROM catalogue.canonical_categories
                WHERE normalized_path = %s
                  AND status = 'ACTIVE'
            """, [normalized])

            result = cursor.fetchone()

            return result[0] if result else None

    @staticmethod
    def map_supplier_category(
        supplier_id,
        supplier_path,
        canonical_category_id,
        confidence=0,
        status="PENDING",
        matching_method="MANUAL"
    ):

        if not supplier_id:
            raise ValueError("Supplier ID required")

        if not 0 <= float(confidence) <= 1:
            raise ValueError("Invalid confidence")

        if status not in (
            "PENDING",
            "APPROVED",
            "REJECTED"
        ):
            raise ValueError("Invalid mapping status")

        normalized = normalize_category(supplier_path)

        with transaction.atomic():

            with connection.cursor() as cursor:

                cursor.execute("""
                    SELECT id
                    FROM catalogue.canonical_categories
                    WHERE id = %s
                """, [str(canonical_category_id)])

                if not cursor.fetchone():
                    raise ValueError(
                        "Canonical category does not exist"
                    )

                cursor.execute("""
                    INSERT INTO catalogue.supplier_category_mappings (
                        supplier_id,
                        supplier_category_path,
                        normalized_supplier_path,
                        canonical_category_id,
                        confidence,
                        status,
                        matching_method
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s)

                    ON CONFLICT (
                        supplier_id,
                        normalized_supplier_path
                    )
                    DO UPDATE SET
                        supplier_category_path =
                            EXCLUDED.supplier_category_path,
                        canonical_category_id =
                            EXCLUDED.canonical_category_id,
                        confidence =
                            EXCLUDED.confidence,
                        status =
                            EXCLUDED.status,
                        matching_method =
                            EXCLUDED.matching_method,
                        updated_at = NOW()

                    RETURNING id
                """, [
                    supplier_id.strip().lower(),
                    supplier_path.strip(),
                    normalized,
                    str(canonical_category_id),
                    confidence,
                    status,
                    matching_method,
                ])

                return cursor.fetchone()[0]

    @staticmethod
    def resolve_supplier_category(
        supplier_id,
        supplier_path
    ):

        normalized = normalize_category(supplier_path)

        with connection.cursor() as cursor:

            cursor.execute("""
                SELECT
                    canonical_category_id,
                    confidence,
                    status,
                    matching_method
                FROM catalogue.supplier_category_mappings
                WHERE supplier_id = %s
                  AND normalized_supplier_path = %s
            """, [
                supplier_id.strip().lower(),
                normalized,
            ])

            row = cursor.fetchone()

            if not row:
                return None

            if row[2] != "APPROVED":
                return None

            return {
                "canonical_category_id": row[0],
                "confidence": float(row[1]),
                "status": row[2],
                "matching_method": row[3],
            }

    @staticmethod
    def link_saleor_category(
        canonical_category_id,
        saleor_category_id
    ):

        if not saleor_category_id:
            raise ValueError("Saleor category ID required")

        with transaction.atomic():

            with connection.cursor() as cursor:

                cursor.execute("""
                    SELECT id
                    FROM catalogue.canonical_categories
                    WHERE id = %s
                """, [str(canonical_category_id)])

                if not cursor.fetchone():
                    raise ValueError(
                        "Canonical category does not exist"
                    )

                cursor.execute("""
                    INSERT INTO catalogue.saleor_category_links (
                        canonical_category_id,
                        saleor_category_id
                    )
                    VALUES (%s, %s)
                    ON CONFLICT DO NOTHING
                    RETURNING id
                """, [
                    str(canonical_category_id),
                    str(saleor_category_id),
                ])

                result = cursor.fetchone()

                if result:
                    return result[0]

                cursor.execute("""
                    SELECT id
                    FROM catalogue.saleor_category_links
                    WHERE canonical_category_id = %s
                      AND saleor_category_id = %s
                """, [
                    str(canonical_category_id),
                    str(saleor_category_id),
                ])

                existing = cursor.fetchone()

                if existing:
                    return existing[0]

                raise ValueError(
                    "Saleor category already linked elsewhere"
                )

    @staticmethod
    def record_history(
        supplier_id,
        supplier_path,
        canonical_category_id,
        matching_method,
        confidence,
        decision,
        details=None
    ):

        import json

        with connection.cursor() as cursor:

            cursor.execute("""
                INSERT INTO catalogue.category_resolution_history (
                    supplier_id,
                    supplier_category_path,
                    canonical_category_id,
                    matching_method,
                    confidence,
                    decision,
                    details
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
                RETURNING id
            """, [
                supplier_id,
                supplier_path,
                str(canonical_category_id)
                    if canonical_category_id else None,
                matching_method,
                confidence,
                decision,
                json.dumps(details or {}),
            ])

            return cursor.fetchone()[0]


def resolve_exact_category(path):

    category_id = CategoryRepository.find_by_path(path)

    if category_id:
        return {
            "canonical_category_id": category_id,
            "confidence": 1.0,
            "matching_method": "EXACT_PATH",
            "status": "PENDING",
        }

    return None
