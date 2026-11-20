"""CAAG canonical taxonomy + supplier crosswalk.
Supplier categories are NEVER created directly in Saleor."""
import re

# (slug, name, parent_slug, seo_title, seo_description)
CANONICAL_TREE = [
    ("computers", "Computers", None,
     "Computers | CAA Global", "Business laptops, desktops and workstations."),
    ("laptops", "Laptops", "computers",
     "Laptops | CAA Global", "Business laptops from Lenovo, HP, Dell, Dynabook and Microsoft."),
    ("laptops-13-inch", "13-Inch Laptops", "laptops",
     "13-Inch Laptops | CAA Global", "Compact 13-inch business laptops."),
    ("laptops-14-inch", "14-Inch Laptops", "laptops",
     "14-Inch Laptops | CAA Global", "14-inch business laptops."),
    ("laptops-15-inch", "15-Inch Laptops", "laptops",
     "15-Inch Laptops | CAA Global", "15-inch business laptops."),
    ("laptops-16-inch", "16-Inch Laptops", "laptops",
     "16-Inch Laptops | CAA Global", "16-inch business laptops."),
    ("laptops-17-inch", "17-Inch Laptops", "laptops",
     "17-Inch Laptops | CAA Global", "17-inch business laptops."),
    ("accessories", "Accessories", None,
     "Accessories | CAA Global", "Laptop bags, cases and accessories."),
    ("bags-and-cases", "Bags & Cases", "accessories",
     "Laptop Bags & Cases | CAA Global", "Backpacks, sleeves and carry cases."),
]

BRAND_COLLECTIONS = {
    "LENOVO": ("lenovo-laptops", "Lenovo Laptops"),
    "HP": ("hp-laptops", "HP Laptops"),
    "DELL": ("dell-laptops", "Dell Laptops"),
    "DYNABOOK": ("dynabook-laptops", "Dynabook Laptops"),
    "MICROSOFT": ("microsoft-surface", "Microsoft Surface"),
}
BUNDLE_COLLECTION = ("laptop-bundles", "Laptop Bundles")
GENERAL_COLLECTION = ("laptops", "Laptops")
ACCESSORY_COLLECTION = ("bags-and-cases", "Bags & Cases")

LAPTOP_INDICATORS = ("NOTEBOOK", "LAPTOP", "BUNDLE", "THINKPAD", "ELITEBOOK",
                     "PROBOOK", "LATITUDE", "TECRA", "SURFACE", "PAVILION", "INSPIRON")
ACCESSORY_KEYWORDS = ("BACKPACK", "BACK PACK", "SLEEVE", "CARRY CASE", "LAPTOP BAG")

def parse_screen_size(secondary, description):
    for src in (secondary or "", description or ""):
        m = re.search(r'(\d{2}(?:\.\d)?)\s*(?:INCH|")', src, re.I)
        if m:
            size = int(float(m.group(1)))
            if size in (13, 14, 15, 16, 17): return size
    return None

def is_accessory(description):
    d = (description or "").upper()
    if any(k in d for k in LAPTOP_INDICATORS): return False
    return any(k in d for k in ACCESSORY_KEYWORDS)

def map_category(primary, secondary, tertiary, vendor, description):
    """Return (canonical_slug_path, reason). None path = quarantine."""
    p = (primary or "").strip().upper()
    if p == "NOTEBOOKS":
        if is_accessory(description):
            return ["accessories", "bags-and-cases"], "accessory-keyword"
        size = parse_screen_size(secondary, description)
        if size in (13, 14, 15, 16, 17):
            return ["computers", "laptops", f"laptops-{size}-inch"], f"screen-{size}"
        return ["computers", "laptops"], "notebooks-default"
    return None, f"unmapped-primary:{p}"

def collections_for(vendor, product_type, description):
    v = (vendor or "").strip().upper()
    if is_accessory(description):
        cols = [ACCESSORY_COLLECTION]
    else:
        cols = [GENERAL_COLLECTION]
        if (product_type or "").strip() == "BundledItem":
            cols.append(BUNDLE_COLLECTION)
    if v in BRAND_COLLECTIONS: cols.append(BRAND_COLLECTIONS[v])
    return cols
