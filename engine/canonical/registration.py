"""
Stage 8.5D — Canonical identity registration contract.

Responsibilities:
- Validate identity before database writes.
- Serialize concurrent registrations using advisory locks.
- Resolve existing canonical identities.
- Create canonical products when no identity exists.
- Register all supplied identifiers atomically.

This module does not create supplier links, offers,
inventory records, or Saleor products.
"""

from django.db import connection, transaction

from .identity import (
    normalize_gtin,
    normalize_mpn,
    normalize_manufacturer,
)

from .reconciliation import (
    resolve_identity,
    register_identity,
    IdentityConflict,
)

from .repository import CanonicalRepository


class RegistrationValidationError(ValueError):
    """Canonical registration input is invalid."""


class CanonicalRegistrationError(Exception):
    """Canonical registration could not be completed."""


def validate_registration(
    canonical_name,
    gtin=None,
    mpn=None,
    manufacturer=None,
    brand=None,
    description=None,
):
    """
    Validate and normalize the complete registration request.

    No database operations occur in this function.
    """

    if not isinstance(canonical_name, str):
        raise RegistrationValidationError(
            "Canonical product name must be text"
        )

    canonical_name = canonical_name.strip()

    if not canonical_name:
        raise RegistrationValidationError(
            "Canonical product name is required"
        )

    manufacturer = normalize_manufacturer(manufacturer)
    normalized_gtin = normalize_gtin(gtin) if gtin else None
    normalized_mpn = normalize_mpn(mpn)

    if not normalized_gtin and not normalized_mpn:
        raise RegistrationValidationError(
            "At least one GTIN or MPN is required"
        )

    if normalized_mpn and not manufacturer:
        raise RegistrationValidationError(
            "Manufacturer is required with MPN"
        )

    if brand is not None:
        brand = str(brand).strip() or None

    if description is not None:
        description = str(description).strip() or None

    return {
        "canonical_name": canonical_name,
        "manufacturer": manufacturer or None,
        "brand": brand,
        "description": description,
        "gtin": normalized_gtin,
        "mpn": normalized_mpn,
    }


def _acquire_identity_locks(identity):
    """
    Acquire transaction-scoped locks in deterministic order.

    Sorting prevents deadlocks when multiple registrations
    contain overlapping GTIN and MPN identifiers.
    """

    keys = []

    if identity["gtin"]:
        keys.append(
            "canonical-identity:v1:GTIN::"
            + identity["gtin"]
        )

    if identity["mpn"]:
        keys.append(
            "canonical-identity:v1:MPN:"
            + identity["manufacturer"]
            + ":"
            + identity["mpn"]
        )

    for key in sorted(keys):
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT pg_advisory_xact_lock(
                    hashtextextended(%s, 0)
                )
                """,
                [key],
            )


def register_canonical_product(
    canonical_name,
    gtin=None,
    mpn=None,
    manufacturer=None,
    brand=None,
    description=None,
):
    """
    Register or reuse a canonical product.

    Returns:
        {
            "canonical_product_id": UUID,
            "status": "CREATED" | "REUSED",
            "matching_method": str,
            "registered_gtin": str | None,
            "registered_mpn": str | None,
            "manufacturer": str | None,
        }

    Transaction guarantee:
        Product creation and identifier registration
        either both commit or both roll back.
    """

    # --------------------------------------------------
    # PHASE 1: VALIDATION
    # No database writes are permitted here.
    # --------------------------------------------------

    identity = validate_registration(
        canonical_name=canonical_name,
        gtin=gtin,
        mpn=mpn,
        manufacturer=manufacturer,
        brand=brand,
        description=description,
    )

    # --------------------------------------------------
    # PHASE 2: ATOMIC REGISTRATION
    # --------------------------------------------------

    with transaction.atomic():

        # Serialize registrations sharing an identifier.
        _acquire_identity_locks(identity)

        # Resolve only after obtaining identity locks.
        existing_id, matching_method = resolve_identity(
            gtin=identity["gtin"],
            mpn=identity["mpn"],
            manufacturer=identity["manufacturer"],
        )

        created = False

        if existing_id:

            product_id = existing_id

        else:

            # No existing identity: create a new canonical product.
            product_id = CanonicalRepository.create_product(
                name=identity["canonical_name"],
                manufacturer=identity["manufacturer"],
                brand=identity["brand"],
                description=identity["description"],
            )

            created = True
            matching_method = "NEW"

        # Register every supplied identifier.
        # Existing identifiers belonging to this product
        # are accepted; conflicting ownership is rejected.

        register_identity(
            product_id=product_id,
            gtin=identity["gtin"],
            mpn=identity["mpn"],
            manufacturer=identity["manufacturer"],
        )

        # Return only after all registration operations succeed.
        return {
            "canonical_product_id": product_id,
            "status": "CREATED" if created else "REUSED",
            "matching_method": matching_method,
            "registered_gtin": identity["gtin"],
            "registered_mpn": identity["mpn"],
            "manufacturer": identity["manufacturer"],
        }
