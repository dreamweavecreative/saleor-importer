-- Importer-owned supplier snapshot lifecycle.
-- This file is NOT executed by file creation.

CREATE SCHEMA IF NOT EXISTS catalogue;

ALTER TABLE catalogue.supplier_import_batches
    ADD COLUMN IF NOT EXISTS snapshot_status TEXT NOT NULL DEFAULT 'LEGACY',
    ADD COLUMN IF NOT EXISTS feed_kind TEXT NOT NULL DEFAULT 'UNKNOWN',
    ADD COLUMN IF NOT EXISTS source_sequence BIGINT,
    ADD COLUMN IF NOT EXISTS source_updated_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS validation_summary JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS promoted_at TIMESTAMPTZ;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'catalogue.supplier_import_batches'::regclass
          AND conname = 'supplier_import_batches_snapshot_status_ck'
    ) THEN
        ALTER TABLE catalogue.supplier_import_batches
            ADD CONSTRAINT supplier_import_batches_snapshot_status_ck
            CHECK (snapshot_status IN (
                'LEGACY', 'STAGING', 'VALIDATING', 'READY',
                'PROMOTED', 'SUPERSEDED', 'FAILED', 'REJECTED'
            ));
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'catalogue.supplier_import_batches'::regclass
          AND conname = 'supplier_import_batches_feed_kind_ck'
    ) THEN
        ALTER TABLE catalogue.supplier_import_batches
            ADD CONSTRAINT supplier_import_batches_feed_kind_ck
            CHECK (feed_kind IN ('FULL', 'DELTA', 'UNKNOWN'));
    END IF;
END $$;

ALTER TABLE catalogue.supplier_offers
    ADD COLUMN IF NOT EXISTS snapshot_batch_id UUID,
    ADD COLUMN IF NOT EXISTS availability_state TEXT NOT NULL DEFAULT 'UNKNOWN',
    ADD COLUMN IF NOT EXISTS last_seen_at TIMESTAMPTZ;

-- Every supplier with existing offers must have exactly one
-- completed batch before historical offers can be baselined.
DO $$
DECLARE
    ambiguous_suppliers TEXT;
BEGIN
    SELECT string_agg(x.supplier_id, ', ' ORDER BY x.supplier_id)
      INTO ambiguous_suppliers
    FROM (
        SELECT l.supplier_id
        FROM catalogue.supplier_offers o
        JOIN catalogue.product_supplier_links l
          ON l.id = o.supplier_link_id
        LEFT JOIN catalogue.supplier_import_batches b
          ON b.supplier_id = l.supplier_id
         AND b.status = 'COMPLETED'
        GROUP BY l.supplier_id
        HAVING COUNT(DISTINCT b.id) <> 1
    ) x;

    IF ambiguous_suppliers IS NOT NULL THEN
        RAISE EXCEPTION
            'Cannot baseline supplier offers: expected exactly one completed batch per supplier; ambiguous suppliers: %',
            ambiguous_suppliers;
    END IF;
END $$;

UPDATE catalogue.supplier_offers o
   SET snapshot_batch_id = b.id,
       last_seen_at = COALESCE(o.observed_at, b.completed_at, NOW())
  FROM catalogue.product_supplier_links l,
       catalogue.supplier_import_batches b
 WHERE l.id = o.supplier_link_id
   AND b.supplier_id = l.supplier_id
   AND b.status = 'COMPLETED'
   AND o.snapshot_batch_id IS NULL;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM catalogue.supplier_offers
        WHERE snapshot_batch_id IS NULL
    ) THEN
        RAISE EXCEPTION
            'Cannot complete snapshot migration: offers remain without a baseline batch';
    END IF;
END $$;

-- Historical NULL quantities are UNKNOWN, not unlimited.
UPDATE catalogue.supplier_offers
   SET availability_state = CASE
       WHEN available_quantity > 0 THEN 'FINITE'
       WHEN available_quantity = 0 THEN 'UNAVAILABLE'
       ELSE 'UNKNOWN'
   END;

-- Remove the old one-offer-per-link unique constraint/index.
DO $$
DECLARE
    c RECORD;
    offer_attnum SMALLINT;
    i RECORD;
BEGIN
    SELECT attnum INTO offer_attnum
    FROM pg_attribute
    WHERE attrelid = 'catalogue.supplier_offers'::regclass
      AND attname = 'supplier_link_id'
      AND NOT attisdropped;

    FOR c IN
        SELECT conname
        FROM pg_constraint
        WHERE conrelid = 'catalogue.supplier_offers'::regclass
          AND contype = 'u'
          AND conkey = ARRAY[offer_attnum]::SMALLINT[]
    LOOP
        EXECUTE format(
            'ALTER TABLE catalogue.supplier_offers DROP CONSTRAINT %I',
            c.conname
        );
    END LOOP;

    FOR i IN
        SELECT indexrelid::regclass AS index_name
        FROM pg_index
        WHERE indrelid = 'catalogue.supplier_offers'::regclass
          AND indisunique
          AND indpred IS NULL
          AND indnatts = 1
          AND indkey[0] = offer_attnum
    LOOP
        EXECUTE format('DROP INDEX IF EXISTS %s', i.index_name);
    END LOOP;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'catalogue.supplier_offers'::regclass
          AND conname = 'supplier_offers_snapshot_batch_fk'
    ) THEN
        ALTER TABLE catalogue.supplier_offers
            ADD CONSTRAINT supplier_offers_snapshot_batch_fk
            FOREIGN KEY (snapshot_batch_id)
            REFERENCES catalogue.supplier_import_batches(id)
            ON DELETE RESTRICT;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'catalogue.supplier_offers'::regclass
          AND conname = 'supplier_offers_availability_state_ck'
    ) THEN
        ALTER TABLE catalogue.supplier_offers
            ADD CONSTRAINT supplier_offers_availability_state_ck
            CHECK (availability_state IN (
                'FINITE', 'UNLIMITED', 'UNKNOWN', 'UNAVAILABLE'
            ));
    END IF;
END $$;

ALTER TABLE catalogue.supplier_offers
    ALTER COLUMN snapshot_batch_id SET NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS
    supplier_offers_link_snapshot_uq
    ON catalogue.supplier_offers (supplier_link_id, snapshot_batch_id);

CREATE INDEX IF NOT EXISTS
    supplier_offers_snapshot_batch_idx
    ON catalogue.supplier_offers (snapshot_batch_id);

CREATE TABLE IF NOT EXISTS catalogue.supplier_catalogue_state (
    supplier_id TEXT PRIMARY KEY,
    active_batch_id UUID,
    previous_batch_id UUID,
    promoted_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'catalogue.supplier_catalogue_state'::regclass
          AND conname = 'supplier_catalogue_state_active_batch_fk'
    ) THEN
        ALTER TABLE catalogue.supplier_catalogue_state
            ADD CONSTRAINT supplier_catalogue_state_active_batch_fk
            FOREIGN KEY (active_batch_id)
            REFERENCES catalogue.supplier_import_batches(id)
            ON DELETE RESTRICT;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'catalogue.supplier_catalogue_state'::regclass
          AND conname = 'supplier_catalogue_state_previous_batch_fk'
    ) THEN
        ALTER TABLE catalogue.supplier_catalogue_state
            ADD CONSTRAINT supplier_catalogue_state_previous_batch_fk
            FOREIGN KEY (previous_batch_id)
            REFERENCES catalogue.supplier_import_batches(id)
            ON DELETE RESTRICT;
    END IF;
END $$;

-- Establish the existing completed batch as the baseline active
-- snapshot. Grouping by batch avoids UUID aggregate assumptions.
INSERT INTO catalogue.supplier_catalogue_state (
    supplier_id, active_batch_id, promoted_at, updated_at
)
SELECT l.supplier_id,
       o.snapshot_batch_id,
       MAX(b.completed_at),
       NOW()
FROM catalogue.supplier_offers o
JOIN catalogue.product_supplier_links l
  ON l.id = o.supplier_link_id
JOIN catalogue.supplier_import_batches b
  ON b.id = o.snapshot_batch_id
GROUP BY l.supplier_id, o.snapshot_batch_id
ON CONFLICT (supplier_id) DO NOTHING;

UPDATE catalogue.supplier_import_batches b
   SET snapshot_status = 'PROMOTED',
       promoted_at = COALESCE(b.promoted_at, b.completed_at, NOW())
 WHERE EXISTS (
     SELECT 1
     FROM catalogue.supplier_catalogue_state s
     WHERE s.active_batch_id = b.id
 );

CREATE TABLE IF NOT EXISTS catalogue.supplier_snapshot_events (
    id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    supplier_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    from_batch_id UUID,
    to_batch_id UUID,
    actor TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT supplier_snapshot_events_type_ck
        CHECK (event_type IN ('PROMOTE', 'ROLLBACK'))
);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'catalogue.supplier_snapshot_events'::regclass
          AND conname = 'supplier_snapshot_events_from_batch_fk'
    ) THEN
        ALTER TABLE catalogue.supplier_snapshot_events
            ADD CONSTRAINT supplier_snapshot_events_from_batch_fk
            FOREIGN KEY (from_batch_id)
            REFERENCES catalogue.supplier_import_batches(id)
            ON DELETE RESTRICT;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'catalogue.supplier_snapshot_events'::regclass
          AND conname = 'supplier_snapshot_events_to_batch_fk'
    ) THEN
        ALTER TABLE catalogue.supplier_snapshot_events
            ADD CONSTRAINT supplier_snapshot_events_to_batch_fk
            FOREIGN KEY (to_batch_id)
            REFERENCES catalogue.supplier_import_batches(id)
            ON DELETE RESTRICT;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS
    supplier_snapshot_events_supplier_created_idx
    ON catalogue.supplier_snapshot_events (supplier_id, created_at DESC);
