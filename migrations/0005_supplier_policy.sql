-- Per-supplier policy. Sentinel meaning and thresholds become data, so the
-- shared lifecycle stays supplier-agnostic and operators can recalibrate
-- without a code deploy. Additive and idempotent. NOT executed by creation.

CREATE TABLE IF NOT EXISTS catalogue.supplier_policy (
    supplier_id        TEXT PRIMARY KEY,
    display_name       TEXT NOT NULL,
    default_feed_kind  TEXT NOT NULL DEFAULT 'UNKNOWN',
    ordering_mode      TEXT NOT NULL DEFAULT 'receive',
    sentinel_value     BIGINT,
    sentinel_meaning   TEXT NOT NULL DEFAULT 'UNKNOWN',
    tax_inclusive      BOOLEAN NOT NULL DEFAULT FALSE,
    thresholds         JSONB NOT NULL DEFAULT '{}'::jsonb,
    is_active          BOOLEAN NOT NULL DEFAULT TRUE,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT supplier_policy_feed_kind_ck
        CHECK (default_feed_kind IN ('FULL','DELTA','UNKNOWN')),
    CONSTRAINT supplier_policy_ordering_ck
        CHECK (ordering_mode IN ('sequence','timestamp','receive')),
    CONSTRAINT supplier_policy_sentinel_ck
        CHECK (sentinel_meaning IN ('UNLIMITED','UNKNOWN'))
);

INSERT INTO catalogue.supplier_policy
  (supplier_id, display_name, default_feed_kind, ordering_mode,
   sentinel_value, sentinel_meaning, tax_inclusive, thresholds)
VALUES
  ('dickerdata', 'DickerData', 'FULL', 'sequence',
   999999999, 'UNLIMITED', FALSE,
   '{"min_eligible_rows":1000,"max_row_decrease_pct":10.0,
     "max_row_increase_pct":50.0,"max_invalid_pct":0.0,
     "max_unmatched_pct":0.0,"max_duplicate_skus":0}'::jsonb)
ON CONFLICT (supplier_id) DO NOTHING;
