"""Catalogue enrichment helpers: specs, SEO, metadata. No DB writes."""
import re

BRAND_TITLE = {
    "LENOVO": "Lenovo",
    "HP": "HP",
    "HPE": "HPE",
    "DELL": "Dell",
    "DYNABOOK": "Dynabook",
    "MICROSOFT": "Microsoft",
    "CISCO": "Cisco",
    "KENSINGTON": "Kensington",
    "TREND MICR": "Trend Micro",
    "POWERSHIEL": "PowerShield",
    "COOLERMAST": "Cooler Master",
    "CONNECTWIS": "ConnectWise",
    "CHECKPOINT": "Check Point",
    "TPLINK": "TP-Link",
    "DLINK": "D-Link",
    "STARTECH": "StarTech.com",
    "EXTREME": "Extreme Networks",
    "TUNGSTEN": "Tungsten Automation",
    "AUDIOCODES": "AudioCodes",
    "WATCHGUARD": "WatchGuard",
    "YEALINK": "Yealink",
    "SONICWALL": "SonicWall",
    "JABRA": "Jabra",
    "EPSON": "Epson",
    "ADOBE": "Adobe",
    "EATON": "Eaton",
    "SHURE": "Shure",
    "SAMSUNG": "Samsung",
    "COMMBOX": "CommBox",
    "LOGITECH": "Logitech",
    "HIKVISION": "Hikvision",
    "NEAT": "Neat",
    "NVIDIA": "NVIDIA",
    "UBIQUITI": "Ubiquiti",
    "KYOCERA": "Kyocera",
    "QNAP": "QNAP",
    "AVEPOINT": "AvePoint",
    "ASUS": "ASUS",
    "BAMBU": "Bambu Lab",
    "TARGUS": "Targus",
    "ATDEC": "ATDEC",
    "INSTA360": "Insta360",
    "SHARP": "Sharp",
    "REYEE": "Reyee",
    "RUCKUS": "Ruckus",
    "BRIEFCAM": "BriefCam",
    "BELKIN": "Belkin",
    "BARCO": "Barco",
    "H3C": "H3C",
    "ATEN": "ATEN",
    "IDEMIA": "IDEMIA",
    "BTICINO": "BTicino",
    "ACCELTEX": "Acceltex",
    "NETGEAR": "NETGEAR",
    "HUNTRESS": "Huntress",
    "HUDDLY": "Huddly",
    "ADATA": "ADATA",
    "SEAGATE": "Seagate",
    "ASUSTOR": "ASUSTOR",
    "PHILIPS": "Philips",
    "NEDAP": "Nedap",
    "TRUSTWAVE": "Trustwave",
    "COMSOL": "COMSOL",
    "LUMIFY": "Lumify",
    "POLY": "Poly",
}

OS_MAP = {
    "W11P": "Windows 11 Pro",
    "W11H": "Windows 11 Home",
    "W11DG": "Windows 11 Pro Education",
    "W11": "Windows 11",
}

NONPHYSICAL_PRIMARIES = {"SOFTWARE", "WARRANTY", "SERVICES"}


def display_brand(vendor):
    v = (vendor or "").strip()
    return BRAND_TITLE.get(v.upper(), v)


def parse_specs(desc):
    d = desc or ""
    s = {}
    m = re.search(r'(\d{2}(?:\.\d)?)\s*(?:inch|")', d, re.I)
    if m:
        s["screen_size"] = m.group(1) + '"'
    for code, label in OS_MAP.items():
        if re.search(r'\b' + code, d):
            s["os"] = label
            break
    m = re.search(r'\b(\d)\s*Y(?:R|EAR)', d, re.I)
    if m:
        s["warranty"] = m.group(1) + " Years"
    toks = re.findall(r'\b(\d+(?:\.\d+)?)\s*(GB|TB)\b', d, re.I)
    vals = sorted({(float(v) * (1024 if u.upper() == "TB" else 1), v + u.upper())
                   for v, u in toks})
    if len(vals) >= 2:
        s["ram"] = vals[0][1]
        s["storage"] = vals[-1][1]
    elif len(vals) == 1:
        s["storage"] = vals[0][1]
    m = re.search(r'\b((?:U\d|R\d|i\d)[- ]?\d{3,4}[A-Z]{0,2})\b', d)
    if m:
        s["cpu"] = m.group(1)
    return s


def _series(row):
    s = (row.get("TertiaryCategory") or "").strip()
    return "" if s.upper() == "NOTEBOOKS" else s


def enrich_seo(product, row):
    out = {}
    brand = display_brand(row.get("Vendor"))
    primary = (row.get("PrimaryCategory") or "").strip().upper()
    name = row.get("StockDescription") or getattr(product, "name", "")

    if not (getattr(product, "seo_title", "") or "").strip():
        if primary in NONPHYSICAL_PRIMARIES:
            base = f"{brand} {name}".strip()
        else:
            series = _series(row)
            base = f"{brand} {series.title()} {name}".strip() if series else f"{brand} {name}".strip()
        out["seo_title"] = base[:70]

    if not (getattr(product, "seo_description", "") or "").strip():
        specs = parse_specs(name)
        extra = ", ".join(specs[k] for k in ("screen_size", "ram", "storage", "os") if specs.get(k))
        desc = f"Buy {name} at CAA Global."
        if extra:
            desc += " " + extra + "."
        desc += " Australian stock & fast delivery."
        out["seo_description"] = desc[:300]

    return out


def build_metadata(product, row):
    brand = display_brand(row.get("Vendor"))
    desired = {
        "brand": brand,
        "series": _series(row),
        "supplier_category": {
            "primary": (row.get("PrimaryCategory") or "").strip(),
            "secondary": (row.get("SecondaryCategory") or "").strip(),
            "tertiary": (row.get("TertiaryCategory") or "").strip(),
        },
        "source_product_type": (row.get("Type") or "").strip(),
    }
    specs = parse_specs(row.get("StockDescription"))
    if specs:
        desired["specs"] = specs

    existing = getattr(product, "metadata", {}) or {}
    if all(existing.get(k) == v for k, v in desired.items()):
        return {}
    return desired
