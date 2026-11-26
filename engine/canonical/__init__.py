"""Canonical catalogue package.

Exports are lazy (PEP 562). Eagerly importing .identity here pulled Saleor
models in at package-import time, which broke every host-side pure test.
"""
__all__ = ["resolve_identity", "MatchResult", "reconcile"]


def __getattr__(name):
    if name in ("resolve_identity", "MatchResult"):
        from . import identity
        return getattr(identity, name)
    if name == "reconcile":
        from .reconciliation import reconcile
        return reconcile
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__():
    return sorted(list(globals().keys()) + __all__)
