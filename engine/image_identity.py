"""Product identity extraction + candidate confidence scoring.

Evidence-based only. Never persists credentials: Icecat URLs carry
?content_token=... and product.metadata is PUBLIC via GraphQL.
"""
import re
from urllib.parse import urlparse, urlunparse

ACCEPT_THRESHOLD = 0.75

MANUFACTURER_HOSTS = {
    "hp": ("hp.com",), "hpe": ("hpe.com",), "lenovo": ("lenovo.com",),
    "dell": ("dell.com",), "cisco": ("cisco.com",), "ibm": ("ibm.com",),
    "microsoft": ("microsoft.com",), "logitech": ("logitech.com",),
    "jabra": ("jabra.com",), "poly": ("poly.com", "hp.com"),
    "sonicwall": ("sonicwall.com",), "apc": ("apc.com",),
    "eaton": ("eaton.com",), "shure": ("shure.com",),
    "kensington": ("kensington.com",), "targus": ("targus.com",),
    "startech": ("startech.com",), "startech.com": ("startech.com",),
    "dlink": ("dlink.com",), "d-link": ("dlink.com",),
    "tplink": ("tp-link.com",), "tp-link": ("tp-link.com",),
    "ubiquiti": ("ui.com", "ubnt.com"), "netgear": ("netgear.com",),
    "seagate": ("seagate.com"), "qnap": ("qnap.com",),
    "samsung": ("samsung.com",), "epson": ("epson.com",),
    "kyocera": ("kyocera.com",), "asus": ("asus.com",),
    "adata": ("adata.com",), "benq": ("benq.com",),
    "acceltex": ("acceltex.com",), "extremenetworks": ("extremenetworks.com",),
    "viewsonic": ("viewsonic.com",), "yealink": ("yealink.com",),
    "ruckus": ("ruckusnetworks.com", "commscope.com"),
    "watchguard": ("watchguard.com",), "fortinet": ("fortinet.com",),
    "trend micro": ("trendmicro.com",), "trendmicr": ("trendmicro.com",),
}
RETAIL_HOSTS = ("amazon.", "newegg.", "mwave.", "scptech.", "officeworks.",
                "jbhifi.", "centrecom.", "umart.", "bunnings.")

_TOKEN_RE = re.compile(r"\b(?=[A-Z0-9\-]{4,}\b)(?=[A-Z0-9\-]*\d)[A-Z0-9\-]{4,}\b")
_FAMILY_RE = re.compile(
    r"\b(THINKPAD|THINKBOOK|IDEAPAD|ELITEBOOK|PROBOOK|ZBOOK|LATITUDE|OPTIPLEX|"
    r"PRECISION|INSPIRON|TECRA|PORTEGE|DYNABOOK|SURFACE|PAVILION|OMEN|"
    r"CATALYST|NEXUS|FIREFLY|PRODESK|ELITEDESK|THINKCENTRE|THINKSTATION)\b", re.I)


def norm_brand(value):
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def sanitize_url(url):
    """Strip query/fragment/credentials. Safe to store in PUBLIC metadata."""
    try:
        p = urlparse(url)
        return urlunparse((p.scheme, p.hostname or "", p.path, "", "", ""))
    except Exception:
        return ""


def product_identity(product, variant):
    """Return dict with brand, mpn, model_tokens, name. Uses confirmed accessors."""
    md = product.metadata or {}
    pmd = variant.private_metadata or {}
    mpn, mpn_source = "", "none"
    for container in ("supplier_offers", "supplier_data"):
        block = pmd.get(container) or {}
        if isinstance(block, dict):
            for supplier, payload in block.items():
                if isinstance(payload, dict) and payload.get("supplier_sku"):
                    mpn = str(payload["supplier_sku"]).strip()
                    mpn_source = f"{container}.{supplier}.supplier_sku"
                    break
        if mpn:
            break
    if not mpn:
        mpn = (variant.sku or "").strip()
        mpn_source = "variant.sku"

    name = product.name or ""
    # Extract identifiers from compound supplier SKUs such as
    # CTO715_PB16250_AU3Y before considering descriptive name tokens.
    mpn_components = {
        part for part in re.split(r"[^A-Z0-9]+", mpn.upper())
        if len(part) >= 4 and any(ch.isdigit() for ch in part)
    }
    mpn_tokens = set(_TOKEN_RE.findall(mpn.upper())) | mpn_components
    name_tokens = set(_TOKEN_RE.findall(name.upper()))
    family_tokens = {m.group(1).upper() for m in _FAMILY_RE.finditer(name)}

    ordered_tokens = []
    for token in (
        sorted(mpn_tokens, key=lambda t: (-len(t), t))
        + sorted(name_tokens, key=lambda t: (-len(t), t))
        + sorted(family_tokens, key=lambda t: (-len(t), t))
    ):
        if len(token) >= 4 and token not in ordered_tokens:
            ordered_tokens.append(token)

    return {
        "primary_category": (md.get("supplier_category") or {}).get("primary", ""),
        "brand": md.get("brand", ""),
        "brand_norm": norm_brand(md.get("brand")),
        "mpn": mpn,
        "mpn_source": mpn_source,
        "name": name,
        "model_tokens": ordered_tokens[:10],
    }


def host_class(url, brand_norm):
    host = (urlparse(url).hostname or "").lower()
    for key, hosts in MANUFACTURER_HOSTS.items():
        if norm_brand(key) == brand_norm or brand_norm.startswith(norm_brand(key)):
            if any(host == h or host.endswith("." + h) for h in hosts):
                return "manufacturer"
    if any(h in host for h in RETAIL_HOSTS):
        return "retailer"
    if "icecat.biz" in host:
        return "icecat"
    return "unknown"


def token_hits(text, tokens):
    t = re.sub(r"[^A-Z0-9]", "", str(text or "").upper())
    return sum(1 for tok in tokens if re.sub(r"[^A-Z0-9]", "", tok) in t)


def score_icecat(ident, source_brand_norm, tokens):
    """Exact-key lookup succeeded. Brand must agree; title corroborates."""
    returned = norm_brand(ident.get("brand"))
    if returned and source_brand_norm and returned != source_brand_norm:
        return 0.0, "brand_mismatch"
    conf = 0.90
    if returned == source_brand_norm:
        conf += 0.05
    hits = token_hits(ident.get("title"), tokens)
    if hits:
        conf += 0.04
    return min(conf, 0.99), f"exact_key+brand+title_hits={hits}"


def score_web(url, title, brand_norm, tokens):
    """Search results need positive identity evidence, not just availability."""
    cls = host_class(url, brand_norm)
    url_hits = token_hits(url, tokens)
    title_hits = token_hits(title, tokens)
    if url_hits == 0 and title_hits == 0:
        return 0.0, "no_model_evidence"
    conf = 0.50
    if cls == "manufacturer":
        conf += 0.25
    elif cls == "retailer":
        conf += 0.10
    if url_hits:
        conf += 0.20
    elif title_hits:
        conf += 0.12
    return min(conf, 0.94), f"{cls}+url_hits={url_hits}+title_hits={title_hits}"


# Dicker promotional bundle suffixes observed in the live feed.
NON_PHYSICAL = {"SOFTWARE", "WARRANTY", "SERVICES"}


def mpn_candidates(mpn, primary_category=None):
    """Ordered (candidate, derivation) pairs. Exact MPN is ALWAYS first.

    Base derivation is only permitted for physical goods: a support-contract
    SKU must never inherit the base appliance's photograph.
    """
    mpn = (mpn or "").strip()
    if not mpn:
        return []
    out = [(mpn, "exact")]
    if (primary_category or "").strip().upper() in NON_PHYSICAL:
        return out
    cur = mpn
    for _ in range(2):                      # e.g. -MS-MC50 needs two strips
        if "-" not in cur:
            break
        head, sep, tail = cur.rpartition("-")
        if len(head) < 6 or not any(ch.isdigit() for ch in head):
            break
        if not re.fullmatch(r"[A-Za-z0-9]{2,}", tail):
            break
        cur = head
        out.append((cur, "derived_base"))
    return out

# ---------------------------------------------------------------- services
# Support contracts / licences must NEVER receive product photography.
# Category alone is insufficient: Cisco/Aruba service SKUs sit under NETWORKING.
SERVICE_PATTERNS = [
    r"\bSMART\s?NET\b", r"\bCON-(OSP|SNT|SNTP|SAS|S2P)-",
    r"\bSVC\b", r"\bSERVICES?\b", r"\bSUPPORT\b",
    r"\bPARTS\s*(&|AND)\s*LABOU?R\b", r"\bPARTS ONLY\b",
    r"\bNEXT BUSINESS DAY\b", r"\bNBD\b", r"\bONSITE\b",
    r"\bFC\s+NBD\b", r"\bEXCH\b", r"\bWTY\b", r"\bWARRANTY\b",
    r"\bRENEW(AL)?\b", r"\bSUBSCRIPTION\b", r"\bLICEN[CS]E\b",
    r"\bCOMMIT\b", r"\bMTHLY\b", r"\bMONTHLY\b", r"\bPER USER\b",
    r"\bSAAS\b", r"\bNCE\b", r"\bPAYG\b", r"\bSNBT\b",
    r"\b\d+X\d+(X\d+|XNBD)?\b",
]
_SERVICE_RE = [re.compile(p, re.I) for p in SERVICE_PATTERNS]


def is_service_sku(name, mpn, primary_category=None):
    """Return (is_service, reason). Name/MPN based, not category based."""
    if str(primary_category or "").strip().upper() in NON_PHYSICAL:
        return True, "category_non_physical"
    hay = "%s %s" % (name or "", mpn or "")
    for rx in _SERVICE_RE:
        m = rx.search(hay)
        if m:
            return True, "service_pattern:" + m.group(0)[:24]
    return False, ""


# ---------------------------------------------------------------- image hygiene
IMAGE_DENY = (
    "/brands/", "brand-logo", "brand_logo", "logo", "icon", "favicon",
    "sprite", "spacer", "pixel", "1x1", "banner", "badge", "arrow",
    "button", "placeholder", "noimage", "no-image", "no_image",
    "coming-soon", "comingsoon", "default", "generic", "stock-photo",
    "small_image", "thumbnail", "/cache/",
)
AFTERMARKET_HOSTS = ("addonnetworks.com", "add-on", "compatible",
                     "equivalent", "replacementfor")
_NUMERIC_STOCK = re.compile(r"/\d{6,}\.(jpe?g|png|webp|gif)$", re.I)
_AO_SUFFIX = re.compile(r"[-_]ao\b", re.I)


def image_evidence(url, alt, brand_norm, tokens, mpn_pairs):
    """Score what the IMAGE depicts. Page title/URL are NOT evidence.

    mpn_pairs: [(mpn, derivation), ...]  derivation in {exact, derived_base}
    Returns (confidence, reason, matched_mpn, derivation).
    """
    parsed = urlparse(url)
    path, low = parsed.path or "", (parsed.path or "").lower()
    host = (parsed.hostname or "").lower()

    if any(d in low for d in IMAGE_DENY):
        return 0.0, "generic_or_stock_image", None, None
    if _NUMERIC_STOCK.search(low):
        return 0.0, "numeric_stock_id", None, None
    if any(h in host for h in AFTERMARKET_HOSTS) or _AO_SUFFIX.search(low):
        return 0.0, "aftermarket_compatible", None, None

    au, aa = _alnum(url), _alnum(alt)
    for mpn, deriv in mpn_pairs:
        m = _alnum(mpn)
        if m and m in au:
            conf = 0.90 if deriv == "exact" else 0.86
            if host_class(url, brand_norm) == "manufacturer":
                conf += 0.04
            return min(conf, 0.97), "mpn_in_image_url", mpn, deriv
    for mpn, deriv in mpn_pairs:
        m = _alnum(mpn)
        if m and aa and m in aa:
            return 0.84, "mpn_in_alt_text", mpn, deriv
    if host_class(url, brand_norm) == "manufacturer" and token_hits(url, tokens):
        return 0.95, "mfr_direct_verified", None, None
    return 0.0, "no_image_level_evidence", None, None


def _alnum(value):
    """Uppercase alphanumeric-only normalisation for identity matching."""
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())
