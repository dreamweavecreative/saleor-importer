"""Saleor variant integration."""

from .products import find_product_by_sku


def find_variant(sku):
    return find_product_by_sku(sku)
