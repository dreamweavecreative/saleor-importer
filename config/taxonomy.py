"""CAAG canonical taxonomy + supplier crosswalk.

Supplier categories are never created directly as Saleor categories.
They are mapped into a canonical CAAG tree.
"""
import re

# (slug, name, parent_slug, seo_title, seo_description)
CANONICAL_TREE = [
    ("computers", "Computers", None,
     "Computers | CAA Global", "Business laptops, desktops, workstations and servers."),
    ("laptops", "Laptops", "computers",
     "Laptops | CAA Global", "Business laptops from leading manufacturers."),
    ("desktops", "Desktops", "computers",
     "Desktops | CAA Global", "Business desktops and small-form-factor PCs."),
    ("workstations", "Workstations", "computers",
     "Workstations | CAA Global", "Professional workstations for demanding workloads."),
    ("servers", "Servers", "computers",
     "Servers | CAA Global", "Rack, tower and enterprise server hardware."),
    ("tablets", "Tablets", "computers",
     "Tablets | CAA Global", "Business tablets and convertible devices."),
    ("components", "Components", "computers",
     "Computer Components | CAA Global", "Internal components and upgrade parts."),

    ("software", "Software", None,
     "Software | CAA Global", "Business software, licences and subscriptions."),
    ("software-licences", "Software Licences", "software",
     "Software Licences | CAA Global", "Commercial software licences and subscriptions."),

    ("services-warranty", "Services & Warranty", None,
     "Services & Warranty | CAA Global", "Support, warranty and professional services."),
    ("warranty", "Warranty", "services-warranty",
     "Warranty | CAA Global", "Warranty extensions and support uplifts."),
    ("services", "Services", "services-warranty",
     "Services | CAA Global", "Professional and managed services."),

    ("networking", "Networking", None,
     "Networking | CAA Global", "Switches, routers, wireless and network hardware."),
    ("networking-hardware", "Networking Hardware", "networking",
     "Networking Hardware | CAA Global", "Network hardware and infrastructure."),

    ("communications", "Communications", None,
     "Communications | CAA Global", "Voice, video and collaboration hardware."),
    ("communication-devices", "Communication Devices", "communications",
     "Communication Devices | CAA Global", "Phones, headsets and communication devices."),
    ("video-conferencing", "Video Conferencing", "communications",
     "Video Conferencing | CAA Global", "Video conferencing bars, cameras and room systems."),

    ("displays", "Displays", None,
     "Displays | CAA Global", "Monitors and professional displays."),
    ("monitors", "Monitors", "displays",
     "Monitors | CAA Global", "Business monitors and professional displays."),

    ("printers", "Printers", None,
     "Printers | CAA Global", "Printers, MFPs, scanners and consumables."),
    ("printers-mfps", "Printers & MFPs", "printers",
     "Printers & MFPs | CAA Global", "Printers and multifunction devices."),
    ("printer-consumables", "Printer Consumables", "printers",
     "Printer Consumables | CAA Global", "Ink, toner and printer consumables."),
    ("scanners", "Scanners", "printers",
     "Scanners | CAA Global", "Document and image scanners."),

    ("power-ups", "Power & UPS", None,
     "Power & UPS | CAA Global", "UPS, power protection and racks."),
    ("ups", "UPS", "power-ups",
     "UPS | CAA Global", "Uninterruptible power supplies and power protection."),
    ("racks", "Racks", "power-ups",
     "Racks | CAA Global", "Racks and rack infrastructure."),

    ("security-surveillance", "Security & Surveillance", None,
     "Security & Surveillance | CAA Global", "Surveillance, access control and security."),
    ("surveillance", "Surveillance", "security-surveillance",
     "Surveillance | CAA Global", "Cameras and surveillance systems."),
    ("access-control", "Access Control", "security-surveillance",
     "Access Control | CAA Global", "Access control credentials and readers."),
    ("intrusion-detection", "Intrusion Detection", "security-surveillance",
     "Intrusion Detection | CAA Global", "Intrusion detection and alarm systems."),
    ("security", "Security", "security-surveillance",
     "Security | CAA Global", "Security products and solutions."),

    ("storage", "Storage", None,
     "Storage | CAA Global", "Storage devices and media."),
    ("storage-devices", "Storage Devices", "storage",
     "Storage Devices | CAA Global", "SSDs, HDDs and enterprise storage."),
    ("storage-media", "Storage Media", "storage",
     "Storage Media | CAA Global", "Tape, optical and removable storage media."),

    ("peripherals", "Peripherals", None,
     "Peripherals | CAA Global", "Peripherals and accessories."),
    ("peripherals-accessories", "Peripherals & Accessories", "peripherals",
     "Peripherals & Accessories | CAA Global", "Keyboards, mice, docks and accessories."),

    ("smart-home", "Smart Home", None,
     "Smart Home | CAA Global", "Smart home and smart building devices."),
    ("smart-home-devices", "Smart Home Devices", "smart-home",
     "Smart Home Devices | CAA Global", "Smart home devices and automation."),

    ("accessories", "Accessories", None,
     "Accessories | CAA Global", "Bags, cases and general accessories."),
    ("bags-and-cases", "Bags & Cases", "accessories",
     "Bags & Cases | CAA Global", "Backpacks, sleeves and carry cases."),

    ("other", "Other", None,
     "Other | CAA Global", "Other products."),
    ("uncategorised", "Uncategorised", "other",
     "Uncategorised | CAA Global", "Products pending category review."),
]

TREE_BY_SLUG = {slug: (name, parent, seo_title, seo_desc)
                for slug, name, parent, seo_title, seo_desc in CANONICAL_TREE}

PRIMARY_TO_LEAF = {
    "NOTEBOOKS": "laptops",
    "LAPTOPS": "laptops",
    "DESKTOPS": "desktops",
    "WORKSTATIONS": "workstations",
    "SERVERS": "servers",
    "TABLETS": "tablets",
    "COMPONENTS": "components",

    "SOFTWARE": "software-licences",
    "WARRANTY": "warranty",
    "SERVICES": "services",

    "NETWORKING": "networking-hardware",
    "COMMUNICATIONS": "communication-devices",
    "VIDEO CONFERENCING": "video-conferencing",

    "DISPLAYS": "monitors",
    "PRINTERS": "printers-mfps",
    "CONSUMABLES": "printer-consumables",
    "SCANNERS": "scanners",

    "UPS": "ups",
    "RACKS": "racks",

    "SURVEILLANCE": "surveillance",
    "ACCESS CONTROL": "access-control",
    "INTRUSION": "intrusion-detection",
    "SECURITY": "security",

    "STORAGE": "storage-devices",
    "STORAGE MEDIA": "storage-media",
    "PERIPHERALS": "peripherals-accessories",
    "SMART HOME": "smart-home-devices",
    "BAGS AND CASES": "bags-and-cases",
}

PRIMARY_TO_COLLECTION = {
    "NOTEBOOKS": ("laptops", "Laptops"),
    "LAPTOPS": ("laptops", "Laptops"),
    "DESKTOPS": ("computers", "Computers"),
    "WORKSTATIONS": ("computers", "Computers"),
    "SERVERS": ("computers", "Computers"),
    "TABLETS": ("computers", "Computers"),
    "COMPONENTS": ("computers", "Computers"),

    "SOFTWARE": ("software", "Software"),
    "WARRANTY": ("services-warranty", "Services & Warranty"),
    "SERVICES": ("services-warranty", "Services & Warranty"),

    "NETWORKING": ("networking", "Networking"),
    "COMMUNICATIONS": ("communications", "Communications"),
    "VIDEO CONFERENCING": ("communications", "Communications"),

    "DISPLAYS": ("displays", "Displays"),
    "PRINTERS": ("printers", "Printers"),
    "CONSUMABLES": ("printers", "Printers"),
    "SCANNERS": ("printers", "Printers"),

    "UPS": ("power-ups", "Power & UPS"),
    "RACKS": ("power-ups", "Power & UPS"),

    "SURVEILLANCE": ("security-surveillance", "Security & Surveillance"),
    "ACCESS CONTROL": ("security-surveillance", "Security & Surveillance"),
    "INTRUSION": ("security-surveillance", "Security & Surveillance"),
    "SECURITY": ("security-surveillance", "Security & Surveillance"),

    "STORAGE": ("storage", "Storage"),
    "STORAGE MEDIA": ("storage", "Storage"),
    "PERIPHERALS": ("peripherals", "Peripherals"),
    "SMART HOME": ("smart-home", "Smart Home"),
    "BAGS AND CASES": ("bags-and-cases", "Bags & Cases"),
}

# Legacy curated brand collections preserved for continuity.
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

LAPTOP_INDICATORS = (
    "NOTEBOOK", "LAPTOP", "BUNDLE", "THINKPAD", "ELITEBOOK",
    "PROBOOK", "LATITUDE", "TECRA", "SURFACE", "PAVILION", "INSPIRON",
)
ACCESSORY_KEYWORDS = ("BACKPACK", "BACK PACK", "SLEEVE", "CARRY CASE", "LAPTOP BAG")


def parse_screen_size(secondary, description):
    for src in (secondary or "", description or ""):
        m = re.search(r'(\d{2}(?:\.\d)?)\s*(?:INCH|")', src, re.I)
        if m:
            size = int(float(m.group(1)))
            if size in (13, 14, 15, 16, 17):
                return size
    return None


def is_accessory(description):
    d = (description or "").upper()
    if any(k in d for k in LAPTOP_INDICATORS):
        return False
    return any(k in d for k in ACCESSORY_KEYWORDS)


def leaf_slug_for_primary(primary):
    p = (primary or "").strip().upper()
    return PRIMARY_TO_LEAF.get(p, "uncategorised")


def path_for_slug(slug):
    path = []
    current = slug
    while current:
        path.append(current)
        entry = TREE_BY_SLUG.get(current)
        current = entry[1] if entry else None
    return list(reversed(path))


def map_category(primary, secondary, tertiary, vendor, description):
    """Return canonical slug path & reason. Screen size is not a category."""
    p = (primary or "").strip().upper()
    if p == "NOTEBOOKS" and is_accessory(description):
        return ["accessories", "bags-and-cases"], "accessory-keyword"
    leaf = leaf_slug_for_primary(p)
    return path_for_slug(leaf), f"primary:{p or 'blank'}"


def all_collections():
    seen = {}
    for item in PRIMARY_TO_COLLECTION.values():
        seen[item[0]] = item[1]
    for item in BRAND_COLLECTIONS.values():
        seen[item[0]] = item[1]
    for item in (BUNDLE_COLLECTION, GENERAL_COLLECTION, ACCESSORY_COLLECTION):
        seen[item[0]] = item[1]
    return sorted(seen.items())


def collections_for(vendor, product_type, description,
                    primary_category=None, secondary_category=None,
                    tertiary_category=None):
    """Return list of (slug, name) collections for a product."""
    cols = []
    p = (primary_category or "").strip().upper()

    if p in PRIMARY_TO_COLLECTION:
        cols.append(PRIMARY_TO_COLLECTION[p])
    elif is_accessory(description):
        cols.append(ACCESSORY_COLLECTION)

    if (product_type or "").strip() == "BundledItem":
        cols.append(BUNDLE_COLLECTION)

    v = (vendor or "").strip().upper()
    if v in BRAND_COLLECTIONS:
        cols.append(BRAND_COLLECTIONS[v])

    # Deduplicate while preserving order.
    out = []
    seen = set()
    for slug, name in cols:
        if slug not in seen:
            seen.add(slug)
            out.append((slug, name))
    return out
