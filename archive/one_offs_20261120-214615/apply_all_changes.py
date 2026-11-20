#!/usr/bin/env python3
"""
apply_all_changes.py - Consolidated engine fix applier for CAAG Saleor importer.
Backs up current engine, writes all corrected files, verifies integrity.
Run: python3 apply_all_changes.py
"""
import os, sys, shutil, hashlib
from datetime import datetime, timezone
from pathlib import Path

IMPORTER_DIR = Path(__file__).parent.resolve()
ENGINE_DIR = IMPORTER_DIR / "engine"
BACKUP_ROOT = IMPORTER_DIR / "backups"

def utc_now():
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

def backup_engine():
    ts = utc_now()
    backup_dir = BACKUP_ROOT / f"engine_backup_{ts}"
    backup_dir.mkdir(parents=True, exist_ok=True)
    if ENGINE_DIR.exists():
        shutil.copytree(ENGINE_DIR, backup_dir / "engine", dirs_exist_ok=True)
    print(f"[BACKUP] {backup_dir}")
    return backup_dir

def write_file(rel_path, content):
    full_path = IMPORTER_DIR / rel_path
    full_path.parent.mkdir(parents=True, exist_ok=True)
    with open(full_path, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"[WRITE]  {rel_path}  ({len(content):,} bytes)")

# ============================================================
# ENGINE FILES
# ============================================================

PRICING_PY = '''"""Saleor pricing integration with CAAG safety checks."""
from decimal import Decimal, ROUND_HALF_UP

SENTINEL_PRICE = Decimal("9999999")
SENTINEL_COST = Decimal("9999")
GST_RATE = Decimal("1.10")


def caag_check_pricing_safety(cost, price):
    """Returns True if pricing is safe, False if it should be quarantined."""
    if price is None or cost is None:
        return False
    try:
        p, c = Decimal(str(price)), Decimal(str(cost))
        if p <= 0:
            return False
        if p >= SENTINEL_PRICE:
            return False
        if c >= SENTINEL_COST:
            return False
        if c > p:
            return False
        return True
    except Exception:
        return False


def calculate_selling_price(rrp_ex_gst):
    """Calculate storefront selling price from RRP ex GST (channel uses prices_entered_with_tax=True)."""
    if rrp_ex_gst is None:
        return None
    try:
        rrp = Decimal(str(rrp_ex_gst))
        if rrp <= 0:
            return None
        return (rrp * GST_RATE).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except Exception:
        return None


def calculate_margin(cost_ex_gst, selling_price_inc_gst):
    """Calculate margin percentage."""
    try:
        cost = Decimal(str(cost_ex_gst))
        price = Decimal(str(selling_price_inc_gst))
        if price <= 0:
            return None
        price_ex = price / GST_RATE
        margin = ((price_ex - cost) / price_ex) * 100
        return margin.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except Exception:
        return None
'''

PRODUCTS_PY = '''"""Saleor product integration with CAAG enrichment."""
import re

BRAND_DISPLAY = {
    "MICROSOFT": "Microsoft", "CISCO": "Cisco", "HPE": "HPE", "HP": "HP",
    "LENOVO": "Lenovo", "DELL": "Dell", "DYNABOOK": "Dynabook",
    "TREND MICR": "Trend Micro", "POWERSHIEL": "PowerShield",
    "COOLERMAST": "Cooler Master", "CONNECTWIS": "ConnectWise",
    "CHECKPOINT": "Check Point", "TPLINK": "TP-Link", "DLINK": "D-Link",
    "STARTECH": "StarTech.com", "EXTREME": "Extreme Networks",
    "TUNGSTEN": "Tungsten Automation", "AUDIOCODES": "AudioCodes",
    "WATCHGUARD": "WatchGuard", "KENSINGTON": "Kensington",
    "YEALINK": "Yealink", "SONICWALL": "SonicWall", "JABRA": "Jabra",
    "EPSON": "Epson", "ADOBE": "Adobe", "EATON": "Eaton",
    "SHURE": "Shure", "SAMSUNG": "Samsung", "COMMBOX": "CommBox",
    "LOGITECH": "Logitech", "HIKVISION": "Hikvision", "NEAT": "Neat",
    "NVIDIA": "NVIDIA", "UBIQUITI": "Ubiquiti", "KYOCERA": "Kyocera",
    "QNAP": "QNAP", "AVEPOINT": "AvePoint", "ASUS": "ASUS",
    "BAMBU": "Bambu Lab", "TARGUS": "Targus", "ATDEC": "ATDEC",
    "INSTA360": "Insta360", "SHARP": "Sharp", "REYEE": "Reyee",
    "RUCKUS": "Ruckus", "BRIEFCAM": "BriefCam", "BELKIN": "Belkin",
    "BARCO": "Barco", "H3C": "H3C", "ATEN": "ATEN",
    "IDEMIA": "IDEMIA", "BTICINO": "BTicino", "ACCELTEX": "Acceltex",
    "NETGEAR": "NETGEAR", "HUNTRESS": "Huntress", "HUDDLY": "Huddly",
    "ADATA": "ADATA", "SEAGATE": "Seagate", "ASUSTOR": "ASUSTOR",
    "PHILIPS": "Philips", "NEDAP": "Nedap", "TRUSTWAVE": "Trustwave",
    "COMSOL": "COMSOL", "LUMIFY": "Lumify", "POLY": "Poly",
    "INTERACTIV": "Interactiv", "APC": "APC", "LG": "LG", "HID": "HID",
}

NONPHYSICAL_PRIMARIES = {"SOFTWARE", "WARRANTY", "SERVICES"}


def display_brand(vendor_code):
    if not vendor_code:
        return ""
    v = str(vendor_code).strip().upper()
    return BRAND_DISPLAY.get(v, v.title())


def is_digital_product(primary_category):
    if not primary_category:
        return False
    return str(primary_category).strip().upper() in NONPHYSICAL_PRIMARIES


def caag_enrich_product(product_data, feed_data):
    """Apply CAAG product enrichments: brand, type, metadata, SEO."""
    vendor = feed_data.get("Vendor", "")
    primary_cat = feed_data.get("PrimaryCategory", "")
    name = product_data.get("name", "")

    if product_data.get("weight") == 0.5:
        product_data["weight"] = None

    if is_digital_product(primary_cat):
        product_data["product_type_slug"] = "digital-services"
        product_data["is_shipping_required"] = False
    else:
        product_data["product_type_slug"] = "physical-goods"
        product_data["is_shipping_required"] = True

    brand = display_brand(vendor)
    metadata = product_data.get("metadata") or {}
    if isinstance(metadata, str):
        metadata = {}
    metadata["brand"] = brand
    metadata["supplier_category"] = {
        "primary": feed_data.get("PrimaryCategory", ""),
        "secondary": feed_data.get("SecondaryCategory", ""),
        "tertiary": feed_data.get("TertiaryCategory", ""),
    }
    metadata["source_product_type"] = feed_data.get("Type", "")
    product_data["metadata"] = metadata

    if not product_data.get("seo_title"):
        product_data["seo_title"] = f"{brand} {name}".strip()[:70]
    if not product_data.get("seo_description"):
        product_data["seo_description"] = f"Buy {name} from {brand}. Genuine Australian stock, fast delivery."[:300]

    return product_data


def find_product_by_sku(sku):
    from saleor.product.models import ProductVariant
    return ProductVariant.objects.filter(sku__iexact=sku).select_related("product").first()


def parse_specs(description):
    if not description:
        return {}
    specs = {}
    d = str(description)
    m = re.search(r'(\\d{2}(?:\\.\\d)?)\\s*(?:inch|")', d, re.I)
    if m:
        specs["screen_size"] = m.group(1) + '"'
    toks = re.findall(r'\\b(\\d+(?:\\.\\d+)?)\\s*(GB|TB)\\b', d, re.I)
    vals = sorted({(float(v) * (1024 if u.upper() == "TB" else 1), v + u.upper()) for v, u in toks})
    if len(vals) >= 2:
        specs["ram"] = vals[0][1]
        specs["storage"] = vals[-1][1]
    elif len(vals) == 1:
        specs["storage"] = vals[0][1]
    m = re.search(r'\\b(\\d)\\s*Y(?:R|EAR)', d, re.I)
    if m:
        specs["warranty"] = m.group(1) + " Years"
    return specs
'''

VARIANTS_PY = '''"""Saleor variant integration with CAAG adjustments."""

UNLIMITED_QUANTITY_THRESHOLD = 999999999


def caag_adjust_variant(variant_data, stock_qty):
    """Apply CAAG variant adjustments. Supplier availability != owned stock."""
    if variant_data.get("sort_order") is None:
        variant_data["sort_order"] = 0

    try:
        qty = int(stock_qty) if stock_qty is not None else 0
    except (TypeError, ValueError):
        qty = 0

    if qty >= UNLIMITED_QUANTITY_THRESHOLD:
        variant_data["track_inventory"] = False
        variant_data["availability_mode"] = "supplier_available_to_order"
        variant_data["owned_stock_qty"] = 0
    elif qty > 0:
        variant_data["track_inventory"] = False
        variant_data["availability_mode"] = "supplier_available_to_order"
        variant_data["owned_stock_qty"] = 0
    else:
        variant_data["track_inventory"] = True
        variant_data["availability_mode"] = "owned_stock_only"
        variant_data["owned_stock_qty"] = 0

    return variant_data


def is_unlimited_quantity(qty):
    try:
        return int(qty) >= UNLIMITED_QUANTITY_THRESHOLD
    except (TypeError, ValueError):
        return False


def find_variant(sku):
    from engine.products import find_product_by_sku
    return find_product_by_sku(sku)
'''

INVENTORY_PY = '''"""Saleor inventory integration for CAAG.

KEY PRINCIPLE: Supplier availability is NOT owned warehouse inventory.
- Supplier quantities go into variant.private_metadata.supplier_offers
- Owned Stock.quantity only from actual warehouse counts
- Supplier-available variants use track_inventory=False
- Sentinel (999999999) must NEVER be written to Stock.quantity
"""
import logging
from django.db import transaction

logger = logging.getLogger(__name__)
UNLIMITED_QUANTITY_THRESHOLD = 999999999


class InventoryIntegrationError(Exception):
    pass


def resolve_channel_warehouse(channel):
    from saleor.warehouse.models import ChannelWarehouse
    assignments = list(
        ChannelWarehouse.objects.filter(channel=channel)
        .select_related("warehouse").order_by("sort_order", "id")
    )
    if not assignments:
        raise InventoryIntegrationError(f"No warehouse assigned to channel: {channel.slug}")
    if len(assignments) != 1:
        raise InventoryIntegrationError(f"Expected one warehouse for {channel.slug}; found {len(assignments)}")
    return assignments[0].warehouse


def get_stock(variant, warehouse):
    from saleor.warehouse.models import Stock
    return Stock.objects.filter(product_variant=variant, warehouse=warehouse).first()


def get_stock_snapshot(variant, warehouse):
    stock = get_stock(variant, warehouse)
    if stock is None:
        return {"exists": False, "quantity": 0, "quantity_allocated": 0, "available_quantity": 0}
    return {
        "exists": True, "quantity": stock.quantity,
        "quantity_allocated": stock.quantity_allocated,
        "available_quantity": stock.quantity - stock.quantity_allocated,
    }


@transaction.atomic
def set_owned_stock(variant, warehouse, quantity):
    """Set OWNED warehouse inventory. NEVER call with supplier availability."""
    from saleor.warehouse.models import Stock

    if isinstance(quantity, bool) or not isinstance(quantity, int):
        raise ValueError("Inventory quantity must be an integer")
    if quantity < 0:
        raise ValueError("Inventory quantity cannot be negative")
    if quantity >= UNLIMITED_QUANTITY_THRESHOLD:
        raise InventoryIntegrationError(
            f"Refusing to write sentinel quantity {quantity} to owned stock. "
            "Use track_inventory=False for supplier-available items."
        )

    stock = Stock.objects.select_for_update().filter(
        product_variant=variant, warehouse=warehouse
    ).first()

    if stock is None:
        if quantity == 0:
            return None
        return Stock.objects.create(
            product_variant=variant, warehouse=warehouse,
            quantity=quantity, quantity_allocated=0,
        )

    if quantity < stock.quantity_allocated:
        raise InventoryIntegrationError(
            f"Cannot set stock below allocated. Allocated={stock.quantity_allocated}, requested={quantity}"
        )
    stock.quantity = quantity
    stock.save(update_fields=["quantity"])
    return stock


def update_supplier_availability(variant, supplier, supplier_sku, quantity, cost=None, currency="AUD"):
    """Record supplier availability in private metadata. Does NOT modify Stock."""
    from django.utils import timezone

    meta = dict(variant.private_metadata or {})
    offers = dict(meta.get("supplier_offers") or {})
    offers[supplier] = {
        "supplier_sku": supplier_sku,
        "quantity": int(quantity) if quantity else 0,
        "cost_ex_gst": str(cost) if cost else None,
        "currency": currency,
        "last_synced": timezone.now().isoformat(),
    }
    meta["supplier_offers"] = offers

    qty = int(quantity) if quantity else 0
    if qty >= UNLIMITED_QUANTITY_THRESHOLD or qty > 0:
        variant.track_inventory = False
        meta["availability_mode"] = "supplier_available_to_order"
        action = "AVAILABLE_TO_ORDER"
    else:
        variant.track_inventory = True
        meta["availability_mode"] = "owned_stock_only"
        action = "OWNED_STOCK_ONLY"

    variant.private_metadata = meta
    return action


def validate_inventory_environment(channel):
    from saleor.warehouse.models import Stock
    warehouse = resolve_channel_warehouse(channel)
    return {
        "warehouse": warehouse, "warehouse_id": str(warehouse.pk),
        "warehouse_slug": warehouse.slug, "channel_slug": channel.slug,
        "existing_stock_records": Stock.objects.filter(warehouse=warehouse).count(),
    }
'''

SEARCH_INDEX_PY = '''"""Search index management using Saleor's search_vector pipeline."""
import logging
logger = logging.getLogger(__name__)
BATCH_SIZE = 100


def reindex_dirty_products(dry_run=False):
    from saleor.product.models import Product
    from saleor.product.search import update_products_search_vector

    dirty_ids = list(
        Product.objects.filter(search_index_dirty=True).order_by("id").values_list("id", flat=True)
    )
    total = len(dirty_ids)
    logger.info(f"Search reindex: {total} dirty products")

    if dry_run or total == 0:
        return {"total_dirty": total, "reindexed": 0, "dry_run": dry_run}

    reindexed = 0
    for i in range(0, total, BATCH_SIZE):
        batch = dirty_ids[i:i + BATCH_SIZE]
        try:
            update_products_search_vector(batch)
            reindexed += len(batch)
        except Exception as e:
            logger.error(f"Search reindex batch failed at {i}: {e}")

    remaining = Product.objects.filter(search_index_dirty=True).count()
    logger.info(f"Search reindex complete: {reindexed}/{total}, remaining dirty: {remaining}")
    return {"total_dirty": total, "reindexed": reindexed, "remaining_dirty": remaining}


def get_search_stats():
    from saleor.product.models import Product
    total = Product.objects.count()
    populated = Product.objects.exclude(search_vector=None).count()
    dirty = Product.objects.filter(search_index_dirty=True).count()
    return {
        "total_products": total,
        "search_vector_populated": populated,
        "search_vector_missing": total - populated,
        "search_index_dirty": dirty,
        "coverage_pct": round(populated / total * 100, 2) if total else 0,
    }
'''

POST_IMPORT_PY = '''"""Post-import remediation for CAAG Saleor catalogue."""
import logging
logger = logging.getLogger(__name__)
UNLIMITED_QUANTITY_THRESHOLD = 999999999


def caag_run_post_import_fixes(dry_run=False):
    """Master post-import fix routine."""
    results = {}
    logger.info("CAAG POST-IMPORT FIXES - Starting")
    results["sentinel_stock"] = fix_sentinel_stock(dry_run)
    results["sort_order"] = fix_variant_sort_order(dry_run)
    results["search_index"] = rebuild_search_index(dry_run)
    results["discounted_prices"] = recalc_discounted_prices(dry_run)
    results["orphans"] = cleanup_orphan_products(dry_run)
    results["weights"] = fix_fabricated_weights(dry_run)
    logger.info("CAAG POST-IMPORT FIXES - Complete")
    return results


def fix_sentinel_stock(dry_run=False):
    from saleor.warehouse.models import Stock
    from saleor.product.models import ProductVariant

    sentinel_stocks = Stock.objects.filter(
        quantity__gte=UNLIMITED_QUANTITY_THRESHOLD, quantity_allocated=0
    )
    count = sentinel_stocks.count()
    if dry_run or count == 0:
        return {"found": count, "fixed": 0}

    variant_ids = list(sentinel_stocks.values_list("product_variant_id", flat=True))
    ProductVariant.objects.filter(id__in=variant_ids).update(track_inventory=False)
    updated = sentinel_stocks.update(quantity=0)
    logger.info(f"[SENTINEL] Zeroed {updated} stock records, untracked {len(variant_ids)} variants")
    return {"found": count, "fixed": updated}


def fix_variant_sort_order(dry_run=False):
    from django.db import connection
    with connection.cursor() as c:
        c.execute("SELECT COUNT(*) FROM product_productvariant WHERE sort_order IS NULL")
        count = c.fetchone()[0]
    if dry_run or count == 0:
        return {"found": count, "fixed": 0}
    with connection.cursor() as c:
        c.execute("""
            UPDATE product_productvariant v SET sort_order = s.rn
            FROM (SELECT id, ROW_NUMBER() OVER (PARTITION BY product_id ORDER BY id) - 1 AS rn
                  FROM product_productvariant WHERE sort_order IS NULL) s
            WHERE v.id = s.id
        """)
        updated = c.rowcount
    logger.info(f"[SORT ORDER] Fixed {updated} NULL sort_orders")
    return {"found": count, "fixed": updated}


def rebuild_search_index(dry_run=False):
    from engine.search_index import reindex_dirty_products
    return reindex_dirty_products(dry_run=dry_run)


def recalc_discounted_prices(dry_run=False):
    if dry_run:
        return {"status": "skipped"}
    try:
        from django.core.management import call_command
        call_command("update_all_products_discounted_prices", verbosity=0)
        return {"status": "completed"}
    except Exception as e:
        logger.error(f"[DISCOUNTED PRICES] Failed: {e}")
        return {"status": "failed", "error": str(e)}


def cleanup_orphan_products(dry_run=False):
    from saleor.product.models import Product
    from django.db import connection

    orphans = list(Product.objects.filter(variants__isnull=True).values_list("id", flat=True))
    if not orphans:
        return {"found": 0, "deleted": 0}
    if dry_run:
        return {"found": len(orphans), "deleted": 0}

    fk_tables = [
        ("discount_voucher_products", "product_id"),
        ("shipping_shippingmethod_excluded_products", "product_id"),
        ("giftcard_giftcard", "product_id"),
        ("attribute_attributevalue", "reference_product_id"),
        ("attribute_assignedproductattributevalue", "product_id"),
        ("product_productmedia", "product_id"),
        ("product_producttranslation", "product_id"),
        ("product_collectionproduct", "product_id"),
        ("product_productchannellisting", "product_id"),
    ]

    safe = []
    with connection.cursor() as c:
        for pid in orphans:
            has_refs = False
            for table, col in fk_tables:
                c.execute(f"SELECT COUNT(*) FROM {table} WHERE {col} = %s", [pid])
                if c.fetchone()[0] > 0:
                    has_refs = True
                    break
            if not has_refs:
                safe.append(pid)

    deleted = 0
    if safe:
        deleted, _ = Product.objects.filter(id__in=safe).delete()
    logger.info(f"[ORPHANS] Found {len(orphans)}, deleted {deleted}")
    return {"found": len(orphans), "deleted": deleted}


def fix_fabricated_weights(dry_run=False):
    from saleor.product.models import Product
    count = Product.objects.filter(weight=0.5).count()
    if dry_run or count == 0:
        return {"found": count, "fixed": 0}
    updated = Product.objects.filter(weight=0.5).update(weight=None)
    logger.info(f"[WEIGHTS] Nulled {updated} fabricated weights")
    return {"found": count, "fixed": updated}
'''

# ============================================================
# MAIN
# ============================================================
FILES = {
    "engine/pricing.py": PRICING_PY,
    "engine/products.py": PRODUCTS_PY,
    "engine/variants.py": VARIANTS_PY,
    "engine/inventory.py": INVENTORY_PY,
    "engine/search_index.py": SEARCH_INDEX_PY,
    "engine/post_import.py": POST_IMPORT_PY,
}

def main():
    print("=" * 70)
    print("CAAG IMPORTER ENGINE - APPLY ALL CHANGES")
    print(f"Time: {utc_now()}")
    print(f"Dir:  {IMPORTER_DIR}")
    print("=" * 70)

    if not ENGINE_DIR.exists():
        print(f"[ERROR] Engine dir not found: {ENGINE_DIR}")
        return 1

    print("\\n--- BACKUP ---")
    backup_engine()

    print("\\n--- WRITE FILES ---")
    for rel_path, content in FILES.items():
        write_file(rel_path, content)

    print("\\n--- VERIFY ---")
    all_ok = True
    for rel_path in FILES:
        p = IMPORTER_DIR / rel_path
        if p.exists() and p.stat().st_size > 0:
            print(f"  [OK]   {rel_path:<40} {p.stat().st_size:>6,} bytes")
        else:
            print(f"  [FAIL] {rel_path}")
            all_ok = False

    print("\\n" + "=" * 70)
    if all_ok:
        print("SUCCESS - All engine files updated.")
        print("\\nTo deploy to container:")
        print("  docker cp /opt/saleor-caag/imports/importer saleor-caag-api:/tmp/imports/importer")
        print("\\nTo run post-import fixes:")
        print("  docker exec -i -e PYTHONPATH=/app:/tmp saleor-caag-api python -c \\")
        print('    "from imports.importer.engine.post_import import caag_run_post_import_fixes; print(caag_run_post_import_fixes())"')
    else:
        print("WARNING - Some files failed. Check output.")
    print("=" * 70)
    return 0 if all_ok else 1

if __name__ == "__main__":
    sys.exit(main())
