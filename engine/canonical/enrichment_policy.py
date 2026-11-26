"""Pure image-sourcing decisions. No Django, no network, no filesystem.

Rule set:
  * A product with no eligible supply is not enriched. Sourcing images for
    withdrawn products spends provider quota for no customer benefit.
  * Digital / service / warranty items get a generated branded card, never a
    provider search. A software subscription has no product photograph.
  * Existing verified media is never replaced without explicit force.
"""

SERVICE_HINTS = (
    "WARRANTY", "SUPPORT", "SERVICE", "SUBSCRIPTION", "LICENCE", "LICENSE",
    "COMMIT", "NBD", "ONSITE", "SMARTNET", "RENEWAL", "PAYG", "NCE",
)
DIGITAL_TYPE_SLUGS = ("digital-services",)


def looks_non_physical(name, primary_category=None, product_type_slug=None):
    if str(product_type_slug or "").strip().lower() in DIGITAL_TYPE_SLUGS:
        return True, "DIGITAL_PRODUCT_TYPE"
    if str(primary_category or "").strip().upper() in ("SOFTWARE", "WARRANTY", "SERVICES"):
        return True, "NON_PHYSICAL_CATEGORY"
    hay = str(name or "").upper()
    for hint in SERVICE_HINTS:
        if hint in hay:
            return True, "SERVICE_NAME:" + hint
    return False, ""


def decide(has_eligible_supply, has_verified_media, name,
           primary_category=None, product_type_slug=None, force=False):
    """Return the enrichment action for one product."""
    if has_verified_media and not force:
        return {"action": "SKIP", "reason": "HAS_VERIFIED_MEDIA",
                "provider_calls": 0}
    if not has_eligible_supply:
        return {"action": "SKIP", "reason": "NO_ELIGIBLE_SUPPLY",
                "provider_calls": 0}
    non_physical, why = looks_non_physical(name, primary_category, product_type_slug)
    if non_physical:
        return {"action": "CARD", "reason": why, "provider_calls": 0,
                "card_kind": "service"}
    return {"action": "SOURCE", "reason": "PHYSICAL_WITH_SUPPLY",
            "provider_calls": 1, "card_kind": "pending"}


def summarise(decisions):
    counts, reasons = {}, {}
    for d in decisions:
        counts[d["action"]] = counts.get(d["action"], 0) + 1
        reasons[d["reason"]] = reasons.get(d["reason"], 0) + 1
    return {"counts": counts, "reasons": reasons,
            "estimated_provider_calls": sum(d.get("provider_calls", 0)
                                            for d in decisions),
            "evaluated": len(decisions)}
