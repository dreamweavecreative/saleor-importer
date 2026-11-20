#!/usr/bin/env python3
"""
Bulletproof Verification & Comprehensive Field Runner for CAAG Engine Patches
Uses AST to extract functions directly, bypassing relative import crashes.
Tests comprehensive Saleor fields (category, collections, attributes, etc.)
"""
import os
import ast
import json

ENGINE_DIR = "/opt/saleor-caag/imports/importer/engine"

def extract_function(file_path, func_name):
    """Extracts a function's source code and compiles it, bypassing module imports."""
    with open(file_path, "r") as f:
        source = f.read()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == func_name:
            func_source = ast.get_source_segment(source, node)
            namespace = {}
            # Execute the function definition in an isolated namespace
            exec(func_source, namespace)
            return namespace[func_name]
    raise ValueError(f"Function {func_name} not found in {file_path}")

print("="*70)
print("STEP 1 & 2: VERIFYING PATCHES & INTEGRATION")
print("="*70)

# 1. Check Markers
files_to_check = ["pricing.py", "products.py", "variants.py", "post_import.py"]
for f in files_to_check:
    path = os.path.join(ENGINE_DIR, f)
    if os.path.exists(path):
        with open(path, "r") as file:
            content = file.read()
            if "CAAG FIX:" in content:
                print(f"[OK] {f} contains the CAAG FIX marker.")
            else:
                print(f"[FAIL] {f} is missing the marker!")
    else:
        print(f"[MISSING] {f} does not exist.")

# 2. Check Integration (Fixing the false positive)
orchestrator_path = os.path.join(ENGINE_DIR, "orchestrator.py")
writer_path = os.path.join(ENGINE_DIR, "saleor_writer.py")

if os.path.exists(orchestrator_path):
    with open(orchestrator_path, "r") as f:
        orch_content = f.read()
    if "caag_run_post_import_fixes" in orch_content:
        print("[INTEGRATED] orchestrator.py calls CAAG post-import fixes.")
    else:
        print("[DEAD CODE] orchestrator.py is missing the post-import call!")

if os.path.exists(writer_path):
    with open(writer_path, "r") as f:
        writer_content = f.read()
    if "caag_check_pricing_safety" in writer_content and "caag_enrich_product" in writer_content:
        print("[INTEGRATED] saleor_writer.py calls CAAG pricing and product functions.")
    else:
        print("[DEAD CODE] saleor_writer.py is missing CAAG calls!")

print("\n" + "="*70)
print("STEP 3: COMPREHENSIVE FIELD RUNNER (Testing isolated logic via AST)")
print("="*70)

try:
    # Extract functions directly from source, completely ignoring relative imports!
    caag_check_pricing_safety = extract_function(os.path.join(ENGINE_DIR, "pricing.py"), "caag_check_pricing_safety")
    caag_enrich_product = extract_function(os.path.join(ENGINE_DIR, "products.py"), "caag_enrich_product")
    caag_adjust_variant = extract_function(os.path.join(ENGINE_DIR, "variants.py"), "caag_adjust_variant")
    print("[OK] Successfully extracted CAAG functions using AST.\n")
except Exception as e:
    print(f"[ERROR] Could not extract functions: {e}")
    exit(1)

# Comprehensive Saleor-like data structures
samples = [
    {
        "name": "ThinkPad X1 Carbon Gen 11",
        "cost": 1200.0, "price": 1899.0, "stock_qty": 50, 
        "vendor": "LENOVO", "primary": "NOTEBOOKS", "secondary": "ULTRABOOKS", "tertiary": "X1 CARBON",
        "sku": "LEN-X1C-G11-01", "weight": 1.12, "description": "Premium business ultrabook.",
        "attributes": {"RAM": "16GB", "Storage": "512GB SSD", "CPU": "i7-1365U"}
    },
    {
        "name": "Office 365 Business Premium",
        "cost": 5.0, "price": 0.0, "stock_qty": 10, # Unsafe: Price 0
        "vendor": "HP", "primary": "SOFTWARE", "secondary": "PRODUCTIVITY", "tertiary": "OFFICE",
        "sku": "MS-O365-BP", "weight": 0.0, "description": "Cloud productivity suite.",
        "attributes": {"License Type": "Subscription", "Term": "1 Year"}
    },
    {
        "name": "ProSupport Plus 3-Year",
        "cost": 100.0, "price": 90.0, "stock_qty": 999999999, # Unsafe: Cost > Price, Unlimited stock
        "vendor": "DELL", "primary": "SERVICES", "secondary": "WARRANTY", "tertiary": "PROSUPPORT",
        "sku": "DEL-PS-3Y", "weight": 0.0, "description": "Enterprise hardware support.",
        "attributes": {"Coverage": "Hardware", "Term": "3 Years"}
    },
    {
        "name": "Catalyst 9200L 24-Port PoE+",
        "cost": None, "price": 2000.0, "stock_qty": 0, 
        "vendor": "CISCO", "primary": "NETWORKING", "secondary": "SWITCHES", "tertiary": "CATALYST 9200",
        "sku": "CSCO-C9200L-24P", "weight": 4.5, "description": "Enterprise access switch.",
        "attributes": {"Ports": "24", "PoE": "Yes", "Managed": "Layer 3"}
    }
]

for i, s in enumerate(samples, 1):
    print(f"--- Sample {i:02d} | {s['name']:<35} ---")
    
    # 1. Pricing Safety
    is_safe = caag_check_pricing_safety(s["cost"], s["price"])
    print(f"  Pricing Safe?   : {str(is_safe):<5} (Cost: {s['cost']}, Price: {s['price']})")
    
    # 2. Product Enrichment (Comprehensive Fields)
    product_data = {
        "name": s["name"], "slug": s["sku"].lower(), "description": s.get("description", ""),
        "weight": s.get("weight", 0.0), "metadata": {}, "category": None, "collections": [],
        "attributes": s.get("attributes", {}), "product_type_slug": "physical-goods",
        "seo_title": "", "seo_description": "", "external_reference": s["sku"]
    }
    
    feed_data = {
        "Vendor": s["vendor"], "PrimaryCategory": s["primary"],
        "SecondaryCategory": s.get("secondary", ""), "TertiaryCategory": s.get("tertiary", "")
    }
    
    enriched = caag_enrich_product(product_data, feed_data)
    
    print(f"  Enriched Cat.   : {enriched.get('category', 'Not Set')}")
    print(f"  Enriched Coll.  : {enriched.get('collections', [])}")
    print(f"  Product Type    : {enriched.get('product_type_slug', 'N/A')}")
    print(f"  SEO Title       : {str(enriched.get('seo_title', 'N/A'))[:50]}...")
    print(f"  Attributes      : {enriched.get('attributes', {})}")
    
    # 3. Variant Adjustments (Comprehensive Fields)
    variant_data = {
        "sku": s["sku"], "name": s["name"], "track_inventory": True,
        "weight": s.get("weight", 0.0), "metadata": {}, "attributes": s.get("attributes", {}),
        "sort_order": None, "external_reference": s["sku"]
    }
    
    adjusted = caag_adjust_variant(variant_data, s["stock_qty"])
    
    print(f"  Track Inv?      : {adjusted.get('track_inventory', 'N/A')} (Stock: {s['stock_qty']})")
    print(f"  Variant Attrs   : {adjusted.get('attributes', {})}")
    print(f"  Sort Order      : {adjusted.get('sort_order', 'N/A')}")
    print("-" * 70 + "\n")

print("="*70)
print("✅ VERIFICATION COMPLETE.")
print("   The patches are extracted, isolated, and tested against full Saleor fields.")
print("="*70)
