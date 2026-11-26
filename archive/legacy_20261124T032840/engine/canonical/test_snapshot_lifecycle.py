"""Isolated tests; deliberately avoid importing engine.canonical."""
import importlib.util
import unittest
from datetime import datetime, timezone
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().with_name("snapshot_lifecycle.py")
SPEC = importlib.util.spec_from_file_location(
    "snapshot_lifecycle_under_test", MODULE_PATH
)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("Could not load snapshot_lifecycle.py")

MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

InvalidSnapshotTransition = MODULE.InvalidSnapshotTransition
SnapshotLifecycleRepository = MODULE.SnapshotLifecycleRepository
SnapshotOrderingError = MODULE.SnapshotOrderingError


class SnapshotLifecycleContractTests(unittest.TestCase):
    def test_allowed_transition(self):
        SnapshotLifecycleRepository.validate_transition(
            "STAGING", "VALIDATING"
        )
        SnapshotLifecycleRepository.validate_transition(
            "VALIDATING", "READY"
        )

    def test_disallowed_transition(self):
        with self.assertRaises(InvalidSnapshotTransition):
            SnapshotLifecycleRepository.validate_transition(
                "STAGING", "PROMOTED"
            )

    def test_sequence_must_increase(self):
        SnapshotLifecycleRepository.assert_newer(12, None, 11, None)

    def test_stale_sequence_rejected(self):
        with self.assertRaises(SnapshotOrderingError):
            SnapshotLifecycleRepository.assert_newer(
                10, None, 11, None
            )

    def test_timestamp_must_increase(self):
        old = datetime(2026, 1, 1, tzinfo=timezone.utc)
        new = datetime(2026, 1, 2, tzinfo=timezone.utc)
        SnapshotLifecycleRepository.assert_newer(
            None, new, None, old
        )

    def test_missing_ordering_metadata_rejected(self):
        with self.assertRaises(SnapshotOrderingError):
            SnapshotLifecycleRepository.assert_newer(
                None, None, None, None
            )

    def test_explicit_unordered_override(self):
        SnapshotLifecycleRepository.assert_newer(
            None, None, None, None, allow_unordered=True
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
