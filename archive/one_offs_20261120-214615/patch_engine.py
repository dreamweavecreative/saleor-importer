#!/usr/bin/env python3
"""
CAAG Importer Engine - Diagnostic & Safe Patching Script
Checks current engine files, appends required fix functions, and maps the execution flow.
"""
import os
import re

ENGINE_DIR = "/opt/saleor-caag/imports/importer/engine"

# 1. Define the helper functions to be appended
HELPERS = {
    "pricing.py": {
        "marker": "# --- CAAG FIX: QUARANTINE UNSAFE PRICING ---",
        "code": """
# --- CAAG FIX: QUARANTINE UNSAFE PRICING ---
def caag_check_pricing_safety(cost, price):
    \"\"\"Returns True if pricing is safe, False if it should be quarantined.\"\"\"
    from decimal import Decimal
    if price is None or cost is None: return False
    try:
        p, c = Decimal(str(price)), Decimal(str(cost))
        return not (p <= 0 or p >= Decimal("9999999") or c > p)
    except: return False
# --- END CAAG FIX ---
"""
    },
    "products.py": {
        "marker": "# --- CAAG FIX: PRODUCT ENRICHMENT ---",
        "code": """
# --- CAAG FIX: PRODUCT ENRICHMENT ---
def caag_enrich_product(product_data, feed_data):
    \"\"\"Applies CAAG specific product enrichments.\"\"\"
    if product_data.get("weight") == 0.5: product_data["weight"] = None
    primary_cat = (feed_data.get("PrimaryCategory") or "").upper()
    product_data["product_type_slug"] = "digital-services" if primary_cat in {"SOFTWARE", "WARRANTY", "SERVICES"} else "physical-goods"
    metadata = product_data.get("metadata", {})
    metadata["brand"] = feed_data.get("Vendor", "")
    metadata["supplier_category"] = {"primary": feed_data.get("PrimaryCategory", ""), "secondary": feed_data.get("SecondaryCategory", ""), "tertiary": feed_data.get("TertiaryCategory", "")}
    product_data["metadata"] = metadata
    if not product_data.get("seo_title"):
        brand, name = feed_data.get("Vendor", ""), product_data.get("name", "")
        product_data["seo_title"] = f"{brand} {name}".strip()[:70]
        product_data["seo_description"] = f"Buy {name} at CAA Global. Genuine Australian stock, fast delivery."[:300]
    return product_data
# --- END CAAG FIX ---
"""
    },
    "variants.py": {
        "marker": "# --- CAAG FIX: VARIANT ADJUSTMENTS ---",
        "code": """
# --- CAAG FIX: VARIANT ADJUSTMENTS ---
def caag_adjust_variant(variant_data, stock_qty):
    \"\"\"Applies CAAG specific variant adjustments.\"\"\"
    if stock_qty == 999999999: variant_data["track_inventory"] = False
    if variant_data.get("sort_order") is None: variant_data["sort_order"] = 0
    return variant_data
# --- END CAAG FIX ---
"""
    },
    "post_import.py": {
        "marker": "# --- CAAG FIX: POST IMPORT HOOKS ---",
        "code": """
# --- CAAG FIX: POST IMPORT HOOKS ---
import os, sys
sys.path.insert(0, "/app")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "saleor.settings")
import django; django.setup()
from django.db import connection
from saleor.product.models import Product

def caag_run_post_import_fixes():
    \"\"\"Runs all post-import remediation tasks.\"\"\"
    print("[CAAG] Running post-import fixes...")
    with connection.cursor() as c:
        c.execute(\"\"\"UPDATE product_productvariant v SET sort_order = s.rn
                     FROM (SELECT id, ROW_NUMBER() OVER (PARTITION BY product_id ORDER BY id)-1 AS rn
                           FROM product_productvariant WHERE sort_order IS NULL) s
                     WHERE v.id = s.id\"\"\")
    Product.objects.update(search_index_dirty=True)
    print("[CAAG] Post-import fixes complete.")
# --- END CAAG FIX ---
"""
    }
}

def main():
    print("="*60)
    print("CAAG Importer Engine - Diagnostic & Safe Patching")
    print("="*60)
    
    if not os.path.exists(ENGINE_DIR):
        print(f"[ERROR] Engine directory not found: {ENGINE_DIR}")
        return

    # Step 1: Append helper functions
    for filename, info in HELPERS.items():
        filepath = os.path.join(ENGINE_DIR, filename)
        if not os.path.exists(filepath):
            print(f"[CREATE] {filename}")
            with open(filepath, "w") as f: f.write(info["code"])
            continue
            
        with open(filepath, "r") as f: content = f.read()
        if info["marker"] in content:
            print(f"[OK] {filename} already patched.")
        else:
            print(f"[PATCH] Appending helpers to {filename}...")
            with open(filepath, "a") as f: f.write("\n" + info["code"])
            
    # Step 2: Check orchestrator/writer for injection points
    print("\n" + "="*60)
    print("Analyzing Orchestrator & Writer for injection points...")
    print("="*60)
    
    dump_file = "/tmp/engine_structure_report.txt"
    with open(dump_file, "w") as out:
        for fname in ["orchestrator.py", "saleor_writer.py", "catalog_enrichment.py"]:
            fpath = os.path.join(ENGINE_DIR, fname)
            if os.path.exists(fpath):
                with open(fpath, "r") as f:
                    content = f.read()
                out.write(f"\n{'='*20} {fname} {'='*20}\n")
                out.write(content)
                
                # Find potential function definitions
                funcs = re.findall(r'^def\s+([a-zA-Z0-9_]+)\s*\(', content, re.MULTILINE)
                print(f"[INFO] {fname} contains functions: {', '.join(funcs[:10])}...")
            else:
                print(f"[MISSING] {fname}")
                
    print("\n" + "="*60)
    print("ACTION REQUIRED:")
    print("To strictly avoid making assumptions about your execution flow,")
    print("I have saved the current structure of your engine files to:")
    print(f"  {dump_file}")
    print("\nPlease run: cat /tmp/engine_structure_report.txt")
    print("and paste the output back to me.")
    print("I will then generate the EXACT code to hook these functions into your main loop.")
    print("="*60)

if __name__ == "__main__":
    main()
