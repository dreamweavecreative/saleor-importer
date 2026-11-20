"""Icecat Product JSON client (channel-partner pull API).

Tokens are read from /opt/saleor-caag/imports/config/icecat.env or env vars.
Bulk server traffic uses live-origin.icecat.biz per the Icecat manual.
Images returned must be downloaded and self-hosted; never hotlinked.
"""
import os, re, json
from pathlib import Path
import requests

ENV_FILE = Path("/opt/saleor-caag/imports/config/icecat.env")
BASE = "https://live-origin.icecat.biz/api"
UA = "CAAG-Saleor-ImagePipeline/1.0"
_URL_RE = re.compile(r"https?://[^\s\"\'<>]+")

NOT_IN_CATALOG = {8, 16, 17}     # definitive miss
RESTRICTED     = {9, 12, 14, 15} # subscription/brand restriction -> retry later
TOKEN_BROKEN   = {18, 19}        # wrong token type / bad UUID -> config error

class IcecatError(Exception):
    def __init__(self, message, status_code=None, transient=False):
        super().__init__(message)
        self.status_code = status_code
        self.transient = transient

class IcecatMiss(IcecatError):
    pass

def load_tokens():
    creds = {}
    if ENV_FILE.is_file():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            creds[k.strip()] = v.strip().strip('"').strip("'")
    for k in ("ICECAT_API_TOKEN", "ICECAT_CONTENT_TOKEN"):
        if os.environ.get(k):
            creds[k] = os.environ[k].strip()
    return creds

def configured():
    return bool(load_tokens().get("ICECAT_API_TOKEN"))

def _headers():
    c = load_tokens()
    h = {"User-Agent": UA, "Accept": "application/json"}
    if c.get("ICECAT_API_TOKEN"):
        h["api-token"] = c["ICECAT_API_TOKEN"]
    if c.get("ICECAT_CONTENT_TOKEN"):
        h["content-token"] = c["ICECAT_CONTENT_TOKEN"]
    return h

def get_datasheet(brand=None, product_code=None, gtin=None, icecat_id=None,
                  lang="EN", content="essentialinfo,title,image,gallery",
                  timeout=(5, 25)):
    params = {"lang": lang}
    if content:
        params["content"] = content
    if icecat_id:
        params["icecat_id"] = str(icecat_id)
    elif brand and product_code:
        params["Brand"] = brand
        params["ProductCode"] = product_code
    elif gtin:
        params["GTIN"] = str(gtin)
    else:
        raise IcecatError("no product identifier supplied")
    try:
        r = requests.get(BASE, params=params, headers=_headers(), timeout=timeout)
    except requests.RequestException as exc:
        raise IcecatError("network: %s" % exc, None, transient=True)
    if r.status_code >= 500:
        raise IcecatError("icecat server error %s" % r.status_code,
                          r.status_code, transient=True)
    try:
        payload = r.json()
    except ValueError:
        raise IcecatError("non-JSON response %s" % r.status_code,
                          r.status_code, transient=True)
    if payload.get("msg") == "OK":
        return payload.get("data") or {}
    code = payload.get("StatusCode")
    msg = payload.get("Message") or "icecat error"
    if code in NOT_IN_CATALOG:
        raise IcecatMiss(msg, code)
    if code in RESTRICTED:
        raise IcecatError(msg, code, transient=True)
    if code in TOKEN_BROKEN:
        raise IcecatError("token rejected (%s): api-token vs content-token mixed up?" % code,
                          code, transient=False)
    raise IcecatError(msg, code, transient=False)

PREFERRED = ("highpicurl", "pic500x500url", "mediumpicurl", "highpic",
             "picurl", "url", "lowpicurl", "thumbpicurl", "orgpicurl")

def _walk(node, out):
    if isinstance(node, dict):
        for k, v in node.items():
            if isinstance(v, str) and _URL_RE.fullmatch(v.strip()):
                out.append((str(k).lower(), v.strip()))
            else:
                _walk(v, out)
    elif isinstance(node, list):
        for v in node:
            _walk(v, out)

def image_urls(data):
    """De-duplicated candidate image URLs from Image + Gallery, largest first."""
    out, seen = [], set()
    for section in ("Image", "Gallery"):
        urls = []
        _walk(data.get(section) or {}, urls)
        urls.sort(key=lambda kv: PREFERRED.index(kv[0])
                  if kv[0] in PREFERRED else len(PREFERRED))
        for _, u in urls:
            if u not in seen:
                seen.add(u)
                out.append(u)
    return out

def identities(data):
    gi = data.get("GeneralInfo") or {}
    ei = data.get("EssentialInfo") or {}
    return {
        "icecat_id": gi.get("IcecatId") or ei.get("IcecatId"),
        "title": gi.get("Title") or data.get("Title") or "",
        "brand": gi.get("Brand") or ei.get("Brand") or "",
        "part_code": gi.get("BrandPartCode") or ei.get("BrandPartCode") or "",
        "gtins": gi.get("GTIN") or ei.get("GTIN") or [],
    }
