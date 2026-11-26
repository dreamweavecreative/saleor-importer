"""Importer-owned, explicit SQL migration runner.

The caller supplies a DB-API connection. This module never creates
a connection or applies migrations implicitly.
"""
from __future__ import annotations

import hashlib
from pathlib import Path


LEDGER = "catalogue.importer_schema_migrations"


class MigrationError(RuntimeError):
    pass


def _migration_files(migrations_dir):
    directory = Path(migrations_dir).resolve()
    files = sorted(directory.glob("[0-9][0-9][0-9][0-9]_*.sql"))
    if not files:
        raise MigrationError(
            f"No numbered SQL migrations found in {directory}"
        )
    return files


def _checksum(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_applied(connection):
    with connection.cursor() as cursor:
        cursor.execute("SELECT to_regclass(%s)", (LEDGER,))
        exists = cursor.fetchone()[0]
        if not exists:
            return {}
        cursor.execute(
            f"SELECT migration_name, checksum FROM {LEDGER} "
            "ORDER BY migration_name"
        )
        return dict(cursor.fetchall())


def run_migrations(connection, migrations_dir, *, apply=False):
    """List pending migrations by default; apply only with apply=True."""
    files = _migration_files(migrations_dir)
    applied = _read_applied(connection)
    pending = []

    known_names = {path.name for path in files}
    unknown_applied = set(applied) - known_names
    if unknown_applied:
        raise MigrationError(
            "Database contains migrations absent from this codebase: "
            + ", ".join(sorted(unknown_applied))
        )

    for path in files:
        name = path.name
        digest = _checksum(path)
        previous = applied.get(name)
        if previous is not None:
            if previous != digest:
                raise MigrationError(
                    f"Checksum mismatch for applied migration {name}"
                )
            continue
        pending.append((path, digest))

    if not apply:
        return {
            "mode": "check-only",
            "applied": len(applied),
            "pending": [path.name for path, _ in pending],
        }

    with connection.cursor() as cursor:
        cursor.execute("CREATE SCHEMA IF NOT EXISTS catalogue")
        cursor.execute(f"""
            CREATE TABLE IF NOT EXISTS {LEDGER} (
                migration_name TEXT PRIMARY KEY,
                checksum TEXT NOT NULL,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
    connection.commit()

    applied_now = []
    try:
        for path, digest in pending:
            sql = path.read_text(encoding="utf-8")
            with connection.cursor() as cursor:
                cursor.execute(sql)
                cursor.execute(
                    f"INSERT INTO {LEDGER} "
                    "(migration_name, checksum) VALUES (%s, %s)",
                    (path.name, digest),
                )
            connection.commit()
            applied_now.append(path.name)
    except Exception:
        connection.rollback()
        raise

    return {
        "mode": "apply",
        "applied": len(applied),
        "applied_now": applied_now,
        "pending": [],
    }
