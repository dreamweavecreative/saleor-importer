-- Mark the verified DickerData baseline batch as a FULL snapshot so it is
-- rollback-eligible, and give supplier ordering a deterministicstart.
-- Guarded by exact fingerprint and reconciled row counts. Idempotent.

DO $$
DECLARE n INTEGER;
BEGIN
  SELECT COUNT(*) INTO n
  FROM catalogue.supplier_import_batches
  WHERE id = '78044adb-dff1-40ec-aff4-627fab329e1c'::uuid
    AND supplier_id = 'dickerdata'
    AND feed_fingerprint = 'b4299a13272d04fd6a49b0016e3239b19e35de17037da5323397533c5b38e7e3'
    AND status = 'COMPLETED'
    AND total_rows = 72513
    AND matched_rows = 72513
    AND unmatched_rows = 0
    AND conflict_rows = 0
    AND invalid_rows = 0;
  IF n <> 1 THEN
    RAISE EXCEPTION 'Baseline batch does not match the verified full-feed signature';
  END IF;
END $$;

UPDATE catalogue.supplier_import_batches
SET feed_kind = 'FULL',
    source_sequence = COALESCE(source_sequence, 1),
    validation_summary = jsonb_build_object(
      'source_rows', 75315,
      'eligible_rows', 72513,
      'excluded_rows', 2802,
      'matched_rows', 72513,
      'inserted_offers', 72513,
      'unmatched_rows', 0,
      'invalid_rows', 0,
      'duplicate_skus', 0,
      'baseline', true
    )
WHERE id = '78044adb-dff1-40ec-aff4-627fab329e1c'::uuid
  AND (feed_kind <> 'FULL' OR source_sequence IS NULL);

UPDATE catalogue.supplier_catalogue_state
SET last_promoted_sequence = 1
WHERE supplier_id = 'dickerdata'
  AND active_batch_id = '78044adb-dff1-40ec-aff4-627fab329e1c'::uuid
  AND last_promoted_sequence IS NULL;
