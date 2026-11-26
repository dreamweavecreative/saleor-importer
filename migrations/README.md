# Importer-owned catalogue migrations

These `.sql` files are APPLIED and tracked by `catalogue.importer_schema_migrations`
(name + SHA-256 checksum). The migration runner only picks up files matching
`[0-9][0-9][0-9][0-9]_*.sql`.

DO NOT rename, reorder, move, or edit an applied file. Doing so will either:
  - trip the checksum guard (applied file modified), or
  - be silently re-applied / treated as unknown (file renamed or removed).

Applied set (verified 2026-11-24):
  0001_supplier_snapshot_lifecycle.sql
  0002_snapshot_hardening.sql
  0003_offer_lifecycle_audit.sql
  0004_baseline_full_feed.sql
  0005_supplier_policy.sql
  0006_sentinel_reclassification.sql

New schema changes go in a NEW numbered file (0007_...), never by editing these.
This README is ignored by the runner's glob.
