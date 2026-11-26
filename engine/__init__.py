"""CAAG importer engine package.

Exports are lazy (PEP 562). Eagerly importing .orchestrator here pulled in
Saleor models at package-import time, which broke every standalone script
that touched any engine submodule before django.setup().
"""
__all__ = ["run_import"]


def __getattr__(name):
    if name == "run_import":
        from .orchestrator import run_import
        return run_import
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__():
    return sorted(list(globals().keys()) + __all__)
