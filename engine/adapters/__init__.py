"""Supplier adapter registry.

The shared lifecycle knows only this contract. No supplier field names,
sentinel values or eligibility rules appear outside an adapter.
"""
from .base import (
    SupplierAdapter,
    AdapterError,
    FeedValidationError,
    register,
    get,
    available,
)


def _autoload():
    from . import dickerdata  # noqa: F401  (registers on import)


_autoload()

__all__ = ["SupplierAdapter", "AdapterError", "FeedValidationError",
           "register", "get", "available"]
