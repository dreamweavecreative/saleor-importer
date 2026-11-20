
import unittest

from contextlib import nullcontext
from unittest import mock
from unittest.mock import MagicMock
from uuid import uuid4

from importer.engine.canonical import registration
from importer.engine.canonical.reconciliation import IdentityConflict


class CanonicalRegistrationContractTests(unittest.TestCase):

    def setUp(self):
        self.product_id = uuid4()

    # --------------------------------------------------------
    # 1. INPUT VALIDATION
    # --------------------------------------------------------

    def test_valid_gtin_registration_input(self):

        result = registration.validate_registration(
            canonical_name="Test Product",
            gtin="4006381333931",
        )

        self.assertEqual(
            result["canonical_name"],
            "Test Product",
        )

        self.assertEqual(
            result["gtin"],
            "04006381333931",
        )

    def test_invalid_gtin_rejected(self):

        with self.assertRaises(ValueError):

            registration.validate_registration(
                canonical_name="Test Product",
                gtin="4006381333932",
            )

    def test_missing_identifiers_rejected(self):

        with self.assertRaises(ValueError):

            registration.validate_registration(
                canonical_name="Test Product",
            )

    def test_mpn_without_manufacturer_rejected(self):

        with self.assertRaises(ValueError):

            registration.validate_registration(
                canonical_name="Test Product",
                mpn="ABC123",
            )

    def test_empty_product_name_rejected(self):

        with self.assertRaises(ValueError):

            registration.validate_registration(
                canonical_name="   ",
                gtin="4006381333931",
            )

    def test_invalid_input_performs_no_transaction(self):

        with mock.patch.object(
            registration.transaction,
            "atomic",
        ) as atomic:

            with self.assertRaises(ValueError):

                registration.register_canonical_product(
                    canonical_name="Invalid",
                    gtin="123",
                )

            atomic.assert_not_called()

    # --------------------------------------------------------
    # 2. NEW CANONICAL PRODUCT
    # --------------------------------------------------------

    @mock.patch.object(
        registration,
        "_acquire_identity_locks",
    )
    @mock.patch.object(
        registration,
        "resolve_identity",
        return_value=(None, "UNMATCHED"),
    )
    @mock.patch.object(
        registration.CanonicalRepository,
        "create_product",
    )
    @mock.patch.object(
        registration,
        "register_identity",
    )
    def test_new_product_registration(
        self,
        register_identity,
        create_product,
        resolve_identity,
        acquire_locks,
    ):

        create_product.return_value = self.product_id

        with mock.patch.object(
            registration.transaction,
            "atomic",
            return_value=nullcontext(),
        ):

            result = registration.register_canonical_product(
                canonical_name="New Test Product",
                gtin="4006381333931",
            )

        self.assertEqual(
            result["canonical_product_id"],
            self.product_id,
        )

        self.assertEqual(result["status"], "CREATED")
        self.assertEqual(result["matching_method"], "NEW")

        create_product.assert_called_once()
        register_identity.assert_called_once()
        acquire_locks.assert_called_once()

    # --------------------------------------------------------
    # 3. EXISTING IDENTITY REUSE
    # --------------------------------------------------------

    @mock.patch.object(
        registration,
        "_acquire_identity_locks",
    )
    @mock.patch.object(
        registration,
        "resolve_identity",
        return_value=(None, "UNMATCHED"),
    )
    @mock.patch.object(
        registration.CanonicalRepository,
        "create_product",
    )
    @mock.patch.object(
        registration,
        "register_identity",
    )
    def test_existing_gtin_identity_reused(
        self,
        register_identity,
        create_product,
        resolve_identity,
        acquire_locks,
    ):

        resolve_identity.return_value = (
            self.product_id,
            "GTIN",
        )

        with mock.patch.object(
            registration.transaction,
            "atomic",
            return_value=nullcontext(),
        ):

            result = registration.register_canonical_product(
                canonical_name="Existing Product",
                gtin="4006381333931",
            )

        self.assertEqual(result["status"], "REUSED")

        self.assertEqual(
            result["canonical_product_id"],
            self.product_id,
        )

        create_product.assert_not_called()
        register_identity.assert_called_once()

    @mock.patch.object(
        registration,
        "_acquire_identity_locks",
    )
    @mock.patch.object(
        registration,
        "resolve_identity",
    )
    @mock.patch.object(
        registration.CanonicalRepository,
        "create_product",
    )
    @mock.patch.object(
        registration,
        "register_identity",
    )
    def test_existing_mpn_identity_reused(
        self,
        register_identity,
        create_product,
        resolve_identity,
        acquire_locks,
    ):

        resolve_identity.return_value = (
            self.product_id,
            "MPN",
        )

        with mock.patch.object(
            registration.transaction,
            "atomic",
            return_value=nullcontext(),
        ):

            result = registration.register_canonical_product(
                canonical_name="Existing MPN Product",
                mpn="ABC123",
                manufacturer="Example Manufacturer",
            )

        self.assertEqual(result["status"], "REUSED")
        self.assertEqual(result["matching_method"], "MPN")

        create_product.assert_not_called()

    # --------------------------------------------------------
    # 4. IDENTITY CONFLICT
    # --------------------------------------------------------

    @mock.patch.object(
        registration,
        "_acquire_identity_locks",
    )
    @mock.patch.object(
        registration,
        "resolve_identity",
        side_effect=IdentityConflict("Conflicting identities"),
    )
    @mock.patch.object(
        registration.CanonicalRepository,
        "create_product",
    )
    @mock.patch.object(
        registration,
        "register_identity",
    )
    def test_identity_conflict_rejected(
        self,
        register_identity,
        create_product,
        resolve_identity,
        acquire_locks,
    ):

        with mock.patch.object(
            registration.transaction,
            "atomic",
            return_value=nullcontext(),
        ):

            with self.assertRaises(IdentityConflict):

                registration.register_canonical_product(
                    canonical_name="Conflicting Product",
                    gtin="4006381333931",
                )

        create_product.assert_not_called()
        register_identity.assert_not_called()

    # --------------------------------------------------------
    # 5. IDEMPOTENCY
    # --------------------------------------------------------

    @mock.patch.object(
        registration,
        "_acquire_identity_locks",
    )
    @mock.patch.object(
        registration,
        "resolve_identity",
    )
    @mock.patch.object(
        registration.CanonicalRepository,
        "create_product",
    )
    @mock.patch.object(
        registration,
        "register_identity",
    )
    def test_repeated_registration_reuses_identity(
        self,
        register_identity,
        create_product,
        resolve_identity,
        acquire_locks,
    ):

        resolve_identity.side_effect = [
            (None, "UNMATCHED"),
            (self.product_id, "GTIN"),
        ]

        create_product.return_value = self.product_id

        with mock.patch.object(
            registration.transaction,
            "atomic",
            return_value=nullcontext(),
        ):

            first = registration.register_canonical_product(
                canonical_name="Idempotent Product",
                gtin="4006381333931",
            )

            second = registration.register_canonical_product(
                canonical_name="Idempotent Product",
                gtin="4006381333931",
            )

        self.assertEqual(first["status"], "CREATED")
        self.assertEqual(second["status"], "REUSED")

        create_product.assert_called_once()

    # --------------------------------------------------------
    # 6. ADVISORY LOCK GENERATION
    # --------------------------------------------------------

    def test_advisory_locks_are_deterministically_ordered(self):

        cursor_context = MagicMock()

        cursor = cursor_context.__enter__.return_value

        with mock.patch.object(
            registration.connection,
            "cursor",
            return_value=cursor_context,
        ):

            registration._acquire_identity_locks({
                "gtin": "04006381333931",
                "mpn": "ABC123",
                "manufacturer": "EXAMPLE MANUFACTURER",
            })

        self.assertEqual(cursor.execute.call_count, 2)

        keys = [
            call.args[1][0]
            for call in cursor.execute.call_args_list
        ]

        self.assertEqual(keys, sorted(keys))

        self.assertTrue(
            all(
                "pg_advisory_xact_lock" in call.args[0]
                for call in cursor.execute.call_args_list
            )
        )

    # --------------------------------------------------------
    # 7. TRANSACTION FAILURE PROPAGATION
    # --------------------------------------------------------

    def test_identifier_failure_propagates_for_rollback(self):

        transaction_state = {
            "entered": False,
            "exception_seen": False,
        }

        class TrackingTransaction:

            def __enter__(self):
                transaction_state["entered"] = True
                return self

            def __exit__(self, exc_type, exc_value, traceback):

                if exc_type is not None:
                    transaction_state["exception_seen"] = True

                # Do not suppress exceptions.
                return False

        with mock.patch.object(
            registration.transaction,
            "atomic",
            return_value=TrackingTransaction(),
        ):

            with mock.patch.object(
                registration,
                "_acquire_identity_locks",
            ):

                with mock.patch.object(
                    registration,
                    "resolve_identity",
                    return_value=(None, "UNMATCHED"),
                ):

                    with mock.patch.object(
                        registration.CanonicalRepository,
                        "create_product",
                        return_value=self.product_id,
                    ):

                        with mock.patch.object(
                            registration,
                            "register_identity",
                            side_effect=RuntimeError(
                                "Simulated identifier failure"
                            ),
                        ):

                            with self.assertRaises(RuntimeError):

                                registration.register_canonical_product(
                                    canonical_name="Rollback Test",
                                    gtin="4006381333931",
                                )

        self.assertTrue(transaction_state["entered"])

        self.assertTrue(
            transaction_state["exception_seen"]
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
