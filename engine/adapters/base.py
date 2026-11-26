"""Supplier-independent adapter contract.

Every supplier supplies these eleven capabilities. The shared snapshot
lifecycle calls only these methods; it never inspects source column names.

Pure where possible: no Django import at module scope, so the contract and
its implementations are unit-testable without an application registry.
"""
from abc import ABC, abstractmethod

FEED_KINDS = ("FULL", "DELTA", "UNKNOWN")
AVAILABILITY_STATES = ("FINITE", "UNLIMITED", "UNKNOWN", "UNAVAILABLE")
ORDERING_MODES = ("sequence", "timestamp", "receive")


class AdapterError(RuntimeError):
    pass


class FeedValidationError(AdapterError):
    pass


class SupplierAdapter(ABC):
    """Contract every supplier must implement."""

    # ---- 1. identify supplier -------------------------------------------
    @property
    @abstractmethod
    def supplier_id(self):
        """Stable lowercase identifier, e.g. 'dickerdata'."""

    @property
    def display_name(self):
        return self.supplier_id

    # ---- 2. supported feed formats --------------------------------------
    @property
    @abstractmethod
    def supported_formats(self):
        """Tuple of uppercase format names, e.g. ('CSV',)."""

    # ---- feed completeness declaration ----------------------------------
    @property
    def feed_kind(self):
        """FULL only when the supplier guarantees a complete catalogue."""
        return "UNKNOWN"

    # ---- 3. validate headers --------------------------------------------
    @abstractmethod
    def validate_headers(self, headers):
        """Raise FeedValidationError on drift, duplicates or missing columns."""

    # ---- 4. stream source records ---------------------------------------
    @abstractmethod
    def stream_records(self, path):
        """Yield raw source dicts. Must not load the whole feed into memory."""

    # ---- 5. determine eligible records ----------------------------------
    @abstractmethod
    def is_eligible(self, record):
        """Return (eligible: bool, reason: str). Reason empty when eligible."""

    # ---- 6. normalise records -------------------------------------------
    @abstractmethod
    def normalise(self, record):
        """Return canonical fields: supplier_sku, product_name, manufacturer,
        brand, gtin, mpn, supplier_price, currency, stock_quantity,
        minimum_order_quantity, plus source payload retention."""

    # ---- 7. validate individual records ---------------------------------
    @abstractmethod
    def validate_record(self, normalised):
        """Raise FeedValidationError for an unusable commercial record."""

    # ---- 8. define availability semantics -------------------------------
    @abstractmethod
    def availability(self, record):
        """Return (state, quantity). Supplier sentinel logic lives HERE."""

    @property
    def sentinel_policy(self):
        """Declaration for audit: {'value': int|None, 'meaning': state}."""
        return {"value": None, "meaning": "UNKNOWN"}

    # ---- 9. resolve supplier identity -----------------------------------
    @abstractmethod
    def resolve_identity(self, cursor, supplier_sku):
        """Return (canonical_product_id, matching_method) or (None, 'UNMATCHED').
        Must never silently substitute a different identity key."""

    # ---- 10. provide snapshot ordering ----------------------------------
    @property
    def ordering_mode(self):
        return "receive"

    def ordering(self, path, source_sequence=None, source_updated_at=None):
        """Return (sequence, timestamp). Never infer order from file mtime."""
        return source_sequence, source_updated_at

    # ---- 11. validate complete feed -------------------------------------
    @property
    def thresholds(self):
        return {
            "min_eligible_rows": 1,
            "max_row_decrease_pct": 10.0,
            "max_row_increase_pct": 50.0,
            "max_invalid_pct": 0.0,
            "max_unmatched_pct": 0.0,
            "max_duplicate_skus": 0,
        }

    def validate_feed(self, counts, previous_eligible_rows=None):
        """Whole-feed gate. Return (ok, reasons). Delegates to shared policy."""
        from importer.engine.canonical.availability_policy import evaluate_feed
        return evaluate_feed(counts, previous_eligible_rows, self.thresholds)

    # ---- commercial terms -----------------------------------------------
    @property
    def tax_inclusive(self):
        return False

    def lead_time_days(self, record):
        """None unless the supplier states a numeric lead time. Never invent."""
        return None


# ---------------------------------------------------------------- registry
_REGISTRY = {}


def register(adapter_cls):
    """Register one adapter instance. Duplicate suppliers are rejected."""
    instance = adapter_cls()
    key = instance.supplier_id
    if key in _REGISTRY:
        raise AdapterError("Adapter already registered: " + key)
    _REGISTRY[key] = instance
    return instance


def get(supplier_id):
    key = str(supplier_id or "").strip().lower()
    if key not in _REGISTRY:
        raise AdapterError("No adapter registered for supplier: " + key)
    return _REGISTRY[key]


def available():
    return tuple(sorted(_REGISTRY))
