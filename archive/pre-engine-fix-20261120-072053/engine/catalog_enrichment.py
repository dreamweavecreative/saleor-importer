"""Phase 3 enrichment: SEO + specs + metadata. Preserves manual content."""
import re
BRAND_TITLE = {"LENOVO":"Lenovo","HP":"HP","DELL":"Dell","DYNABOOK":"Dynabook",
               "MICROSOFT":"Microsoft","KENSINGTON":"Kensington"}
OS_MAP = {"W11P":"Windows 11 Pro","W11H":"Windows 11 Home",
          "W11DG":"Windows 11 Pro Education","W11":"Windows 11"}

def parse_specs(desc):
    d = desc or ""; s = {}
    m = re.search(r'(\d{2}(?:\.\d)?)\s*(?:inch|")', d, re.I)
    if m: s["screen_size"] = m.group(1) + '"'
    for code, label in OS_MAP.items():
        if re.search(r'\b' + code, d): s["os"] = label; break
    m = re.search(r'\b(\d)\s*Y(?:R|EAR)', d, re.I)
    if m: s["warranty"] = m.group(1) + " Years"
    toks = re.findall(r'\b(\d+(?:\.\d+)?)\s*(GB|TB)\b', d, re.I)
    vals = sorted({(float(v)*(1024 if u.upper()=="TB" else 1), v+u.upper()) for v,u in toks})
    if len(vals) >= 2: s["ram"] = vals[0][1]; s["storage"] = vals[-1][1]
    elif len(vals) == 1: s["storage"] = vals[0][1]
    m = re.search(r'\b((?:U\d|R\d|i\d)[- ]?\d{3,4}[A-Z]{0,2})\b', d)
    if m: s["cpu"] = m.group(1)
    return s

def _brand(row): return BRAND_TITLE.get((row.get("Vendor") or "").upper(), row.get("Vendor") or "")
def _series(row):
    s = (row.get("TertiaryCategory") or "").strip()
    return "" if s.upper() == "NOTEBOOKS" else s

def enrich_seo(product, row):
    out = {}
    if not (product.seo_title or "").strip():
        series = _series(row)
        base = f"{_brand(row)} {series.title()} Laptop" if series else (row.get("StockDescription") or "")[:60]
        out["seo_title"] = base[:70]
    if not (product.seo_description or "").strip():
        specs = parse_specs(row.get("StockDescription"))
        bits = [_brand(row)] + ([_series(row).title()] if _series(row) else [])
        desc = "Buy " + " ".join(b for b in bits if b)
        extra = ", ".join(specs[k] for k in ("screen_size","ram","storage","os") if specs.get(k))
        if extra: desc += " — " + extra
        desc += ". Australian stock & fast delivery from CAA Global."
        out["seo_description"] = desc[:300]
    return out

def build_metadata(product, row):
    desired = {"brand": _brand(row), "series": _series(row),
               "supplier_category": {"primary": (row.get("PrimaryCategory") or "").strip(),
                                      "secondary": (row.get("SecondaryCategory") or "").strip(),
                                      "tertiary": (row.get("TertiaryCategory") or "").strip()}}
    specs = parse_specs(row.get("StockDescription"))
    if specs: desired["specs"] = specs
    existing = product.metadata or {}
    if all(existing.get(k) == v for k, v in desired.items()): return {}
    return desired
