-- Offer retirement / reactivation audit fields. Additive and idempotent.
-- Requires 0002. NOT executed by file creation.

ALTER TABLE catalogue.supplier_offers
  ADD COLUMN IF NOT EXISTS retired_at        TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS retirement_reason TEXT,
  ADD COLUMN IF NOT EXISTS source_version    TEXT;

ALTER TABLE catalogue.supplier_catalogue_state
  ADD COLUMN IF NOT EXISTS last_promoted_sequence BIGINT;

-- Fast active-generation offer lookups.
CREATE INDEX IF NOT EXISTS supplier_offers_active_batch_idx
  ON catalogue.supplier_offers (snapshot_batch_id, is_active);

CREATE INDEX IF NOT EXISTS supplier_offers_link_active_idx
  ON catalogue.supplier_offers (supplier_link_id)
  WHERE is_active;

-- One active pointer per supplier is already enforced by the primary key.
-- Guard against a pointer referencing another supplier's batch.
DO $$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM catalogue.supplier_catalogue_state s
    JOIN catalogue.supplier_import_batches b ON b.id = s.active_batch_id
    WHERE b.supplier_id <> s.supplier_id
  ) THEN
    RAISE EXCEPTION 'Supplier active pointer references a foreign batch';
  END IF;
END $$;
