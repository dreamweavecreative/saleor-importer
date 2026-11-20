# Canonical identity and reconciliation exports
from .identity import resolve_identity, MatchResult
from .reconciliation import reconcile

__all__ = ["resolve_identity", "MatchResult", "reconcile"]
