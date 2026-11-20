"""Saleor product integration with CAAG enrichment."""
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
    m = re.search(r'(\d{2}(?:\.\d)?)\s*(?:inch|")', d, re.I)
    if m:
        specs["screen_size"] = m.group(1) + '"'
    toks = re.findall(r'\b(\d+(?:\.\d+)?)\s*(GB|TB)\b', d, re.I)
    vals = sorted({(float(v) * (1024 if u.upper() == "TB" else 1), v + u.upper()) for v, u in toks})
    if len(vals) >= 2:
        specs["ram"] = vals[0][1]
        specs["storage"] = vals[-1][1]
    elif len(vals) == 1:
        specs["storage"] = vals[0][1]
    m = re.search(r'\b(\d)\s*Y(?:R|EAR)', d, re.I)
    if m:
        specs["warranty"] = m.group(1) + " Years"
    return specs
