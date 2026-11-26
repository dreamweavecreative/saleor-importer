"""Single source of truth for the supplier snapshot lifecycle.

Three lifecycle modules accumulated during development. Only ONE is
authoritative. This module declares it and detects drift, so a caller
cannot silently use a weaker implementation.

AUTHORITATIVE:
    snapshot_engine.py      lifecycle, locking, ordering, promotion, rollback
    availability.py         FINITE / UNLIMITED / UNKNOWN / UNAVAILABLE
    active_catalogue.py     active-generation reads
    offer_writer.py         snapshot-scoped offer persistence
    offer_selection.py      active-generation selection
    visibility_policy.py    publication / purchase decisions
    enrichment_policy.py    image-sourcing decisions

DEPRECATED (retained on disk, never imported by new code):
    snapshot_lifecycle.py   lock-order inversion in promote()
    snapshot_pipeline.py    superseded by snapshot_engine
    supplier_offers.py      one-offer-per-link conflict key
    landed_cost.py          reads all is_active offers, no generation filter
"""

AUTHORITATIVE = (
    "snapshot_engine",
    "availability",
    "active_catalogue",
    "offer_writer",
    "offer_selection",
    "visibility_policy",
    "enrichment_policy",
)

DEPRECATED = {
    "snapshot_lifecycle": "promote() locks batch before supplier; use snapshot_engine",
    "snapshot_pipeline": "superseded by snapshot_engine",
    "supplier_offers": "ON CONFLICT (supplier_link_id) is not snapshot-scoped; use offer_writer",
    "landed_cost": "no active-generation filter; use offer_selection",
}


class DeprecatedLifecycleUse(RuntimeError):
    pass


def guard(module_name):
    """Raise if a caller reaches for a superseded lifecycle module."""
    key = str(module_name or "").rsplit(".", 1)[-1]
    if key in DEPRECATED:
        raise DeprecatedLifecycleUse(
            key + " is deprecated: " + DEPRECATED[key]
        )
    return True


def inventory(canonical_dir):
    """Report which lifecycle modules exist. Pure filesystem inspection."""
    from pathlib import Path
    directory = Path(canonical_dir)
    present = {}
    for name in AUTHORITATIVE:
        present[name] = (directory / (name + ".py")).is_file()
    legacy = {}
    for name in DEPRECATED:
        legacy[name] = (directory / (name + ".py")).is_file()
    return {
        "authoritative_present": present,
        "authoritative_complete": all(present.values()),
        "deprecated_present": legacy,
        "deprecated_count": sum(1 for v in legacy.values() if v),
    }
