-- Reclassify DickerData sentinel offers from UNKNOWN to UNLIMITED.
--
-- Justification: the CAAG specification states StockAvailable = 999999999
-- means unlimited availability. The initial migration conservatively mapped
-- it to UNKNOWN, which made 51,331 offers purchase-ineligible.
--
-- Guarded: only offers whose retained source payload carries the exact
-- sentinel AND are currently UNKNOWN. No other offer is touched.
-- Idempotent. NOT executed by file creation.

DO $$
DECLARE n INTEGER;
BEGIN
  SELECT COUNT(*) INTO n
  FROM catalogue.supplier_offers o
  JOIN catalogue.product_supplier_links l ON l.id = o.supplier_link_id
  WHERE l.supplier_id = 'dickerdata'
    AND o.availability_state = 'UNKNOWN'
    AND o.available_quantity IS NULL
    AND (o.raw_data->>'StockAvailable') = '999999999';
  RAISE NOTICE 'Sentinel offers eligible for reclassification: %', n;
END $$;

UPDATE catalogue.supplier_offers o
SET availability_state = 'UNLIMITED',
    updated_at = NOW()
FROM catalogue.product_supplier_links l
WHERE l.id = o.supplier_link_id
  AND l.supplier_id = 'dickerdata'
  AND o.availability_state = 'UNKNOWN'
  AND o.available_quantity IS NULL
  AND (o.raw_data->>'StockAvailable') = '999999999';

-- Post-condition: no sentinel offer remains UNKNOWN.
DO $$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM catalogue.supplier_offers o
    JOIN catalogue.product_supplier_links l ON l.id = o.supplier_link_id
    WHERE l.supplier_id = 'dickerdata'
      AND o.availability_state = 'UNKNOWN'
      AND (o.raw_data->>'StockAvailable') = '999999999'
  ) THEN
    RAISE EXCEPTION 'Sentinel reclassification incomplete';
  END IF;
END $$;
