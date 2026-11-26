import unittest


def _reasons(result):
    if isinstance(result, dict):
        return [str(x) for x in result.get("reasons", [])]
    if isinstance(result, (list, tuple)):
        for item in result:
            if isinstance(item, (list, tuple, set)):
                return [str(x) for x in item]
        return [str(x) for x in result]
    return []


def _eval(se, counts, previous=None, thresholds=None):
    try:
        return se.evaluate_thresholds(counts, previous_rows=previous, thresholds=thresholds)
    except TypeError:
        return se.evaluate_thresholds(counts, previous, thresholds)


class VariableFeedValidationTests(unittest.TestCase):
    def setUp(self):
        from importer.engine.canonical import snapshot_engine as se
        self.se = se

    def _counts(self, eligible, matched, offers, unmatched=0, invalid=0,
                dupes=0, excluded=0, source=None):
        return {
            "source_rows": source if source is not None else eligible,
            "eligible_rows": eligible, "excluded_rows": excluded,
            "matched_rows": matched, "inserted_offers": offers,
            "unmatched_rows": unmatched, "invalid_rows": invalid,
            "duplicate_skus": dupes,
        }

    def test_default_strict_blocks_unmatched(self):
        c = self._counts(eligible=100, matched=90, offers=90, unmatched=10)
        r = _reasons(_eval(self.se, c, thresholds={"min_eligible_rows": 1}))
        self.assertIn("NOT_ALL_ELIGIBLE_ROWS_MATCHED", r)

    def test_non_strict_allows_unmatched_new_products(self):
        c = self._counts(eligible=32022, matched=31374, offers=31374, unmatched=648)
        th = {"min_eligible_rows": 1, "max_unmatched_pct": 100.0,
              "max_row_decrease_pct": 100.0, "max_row_increase_pct": 10000.0,
              "strict_match": False}
        r = _reasons(_eval(self.se, c, previous=72513, thresholds=th))
        self.assertNotIn("NOT_ALL_ELIGIBLE_ROWS_MATCHED", r)
        self.assertNotIn("NOT_ALL_ELIGIBLE_ROWS_HAVE_OFFERS", r)
        self.assertNotIn("MATCHED_ROWS_MISSING_OFFERS", r)
        self.assertNotIn("ROW_COUNT_DECREASE_EXCEEDED", r)

    def test_non_strict_still_detects_missing_offers(self):
        c = self._counts(eligible=100, matched=90, offers=80, unmatched=10)
        th = {"min_eligible_rows": 1, "max_unmatched_pct": 100.0, "strict_match": False}
        r = _reasons(_eval(self.se, c, thresholds=th))
        self.assertIn("MATCHED_ROWS_MISSING_OFFERS", r)

    def test_non_strict_still_detects_duplicates(self):
        c = self._counts(eligible=100, matched=100, offers=100, dupes=2)
        th = {"min_eligible_rows": 1, "max_duplicate_skus": 0, "strict_match": False}
        r = _reasons(_eval(self.se, c, thresholds=th))
        self.assertIn("DUPLICATE_SUPPLIER_SKUS", r)

    def test_large_decrease_allowed_when_threshold_relaxed(self):
        c = self._counts(eligible=32022, matched=32022, offers=32022)
        th = {"min_eligible_rows": 1, "max_row_decrease_pct": 100.0, "strict_match": False}
        r = _reasons(_eval(self.se, c, previous=72513, thresholds=th))
        self.assertNotIn("ROW_COUNT_DECREASE_EXCEEDED", r)

    def test_empty_feed_still_blocked(self):
        c = self._counts(eligible=0, matched=0, offers=0, source=0)
        th = {"min_eligible_rows": 1, "strict_match": False}
        r = _reasons(_eval(self.se, c, thresholds=th))
        self.assertIn("BELOW_MINIMUM_ELIGIBLE_ROWS", r)


if __name__ == "__main__":
    unittest.main()
