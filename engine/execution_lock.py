"""
DickerData V4.7 execution lock.

Uses Linux fcntl.flock for exclusive process-level locking.

The lock file is persistent and is NEVER automatically deleted.
Interrupted executions require explicit operator reconciliation.
"""

import fcntl
import json
import os
import socket
import tempfile

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def default_lock_path():
    root = Path(__file__).resolve().parents[2]
    return root / "logs" / "import-execution.lock"


def _read_metadata(handle):
    handle.seek(0)
    raw = handle.read()

    if not raw.strip():
        return {}

    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "Execution lock metadata is corrupt. "
            "Manual reconciliation required."
        ) from exc


def _write_metadata(handle, metadata):
    payload = json.dumps(
        metadata,
        indent=2,
        sort_keys=True,
    )

    handle.seek(0)
    handle.truncate()
    handle.write(payload)
    handle.write("\n")
    handle.flush()
    os.fsync(handle.fileno())


@contextmanager
def execution_lock(import_id, lock_path=None):

    path = Path(lock_path or default_lock_path())
    path.parent.mkdir(parents=True, exist_ok=True)

    # Never replace or unlink this file. flock protects its inode.
    with path.open("a+", encoding="utf-8") as handle:

        try:
            fcntl.flock(
                handle.fileno(),
                fcntl.LOCK_EX | fcntl.LOCK_NB,
            )
        except BlockingIOError as exc:
            raise RuntimeError(
                "Another importer process currently holds "
                f"the execution lock: {path}"
            ) from exc

        metadata = _read_metadata(handle)

        previous_state = metadata.get("state")

        if previous_state in (
            "RUNNING",
            "COMMITTING",
            "RECONCILIATION_REQUIRED",
        ):
            raise RuntimeError(
                "Previous import has an unresolved execution state: "
                f"{previous_state}. "
                "Inspect its manifest and reconcile the database "
                "before explicitly recovering the lock."
            )

        started = utc_now()

        current = {
            "version": "1.0",
            "state": "RUNNING",
            "import_id": str(import_id),
            "pid": os.getpid(),
            "hostname": socket.gethostname(),
            "started_at": started,
            "completed_at": None,
            "previous_import_id": metadata.get("import_id"),
            "lock_file": str(path),
            "reconciliation_reference": None,
        }

        _write_metadata(handle, current)

        try:
            yield current

        except BaseException as exc:

            current["state"] = "RECONCILIATION_REQUIRED"
            current["completed_at"] = utc_now()
            current["error"] = (
                f"{type(exc).__name__}: {exc}"
            )

            _write_metadata(handle, current)
            raise

        else:

            current["state"] = "COMPLETED"
            current["completed_at"] = utc_now()

            _write_metadata(handle, current)

        finally:
            # flock is released by closing the descriptor.
            # The persistent lock file is deliberately retained.
            pass


def reconcile_execution_lock(
    reference,
    confirmation,
    lock_path=None,
):
    """
    Explicitly acknowledge manual database/manifest reconciliation.

    This function does NOT inspect or repair PostgreSQL.
    An operator must independently verify the actual outcome first.
    """

    required = "I_HAVE_RECONCILED_DATABASE_AND_MANIFEST"

    if confirmation != required:
        raise RuntimeError("Explicit reconciliation confirmation required")

    if not reference or not reference.strip():
        raise RuntimeError("Reconciliation reference is required")

    path = Path(lock_path or default_lock_path())

    if not path.is_file():
        raise FileNotFoundError(path)

    with path.open("r+", encoding="utf-8") as handle:

        try:
            fcntl.flock(
                handle.fileno(),
                fcntl.LOCK_EX | fcntl.LOCK_NB,
            )
        except BlockingIOError as exc:
            raise RuntimeError(
                "Importer is still holding the execution lock"
            ) from exc

        metadata = _read_metadata(handle)

        if metadata.get("state") not in (
            "RUNNING",
            "COMMITTING",
            "RECONCILIATION_REQUIRED",
        ):
            raise RuntimeError(
                "Lock does not require interrupted-import recovery"
            )

        metadata["state_before_reconciliation"] = metadata["state"]
        metadata["state"] = "RECONCILED"
        metadata["reconciled_at"] = utc_now()
        metadata["reconciliation_reference"] = reference.strip()
        metadata["reconciled_by_pid"] = os.getpid()

        _write_metadata(handle, metadata)

        return metadata
