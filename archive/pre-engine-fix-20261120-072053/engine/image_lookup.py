"""Product image resolution: local index -> cache -> Google CSE / Bing.

API keys come from environment or a chmod-600 env file. Never hard-coded.
Only public http(s) hosts are fetched; images are validated and re-encoded.
"""
import hashlib, ipaddress, json, os, re, socket, time
from datetime import datetime, timezone, timedelta
from io import BytesIO
from pathlib import Path
from urllib.parse import urlparse

import requests
from PIL import Image

Image.MAX_IMAGE_PIXELS = 50_000_000

ROOT = Path("/opt/saleor-caag/imports")
INDEX_DIR = ROOT / "media_index"
CACHE_FILE = ROOT / "cache/image_lookup.json"
ENV_FILE = ROOT / "config/image_api.env"

MAX_BYTES = 8 * 1024 * 1024
MIN_SIDE = 400
TARGET_SIDE = 1600
NEGATIVE_TTL_DAYS = 7
UA = "CAAG-CatalogImageBot/1.0 (+https://caa-global.com)"


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def load_env_file():
    out = {}
    if ENV_FILE.is_file():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def api_config():
    f = load_env_file()
    def get(k):
        return os.environ.get(k) or f.get(k) or ""
    return {
        "google_key": get("GOOGLE_CSE_KEY"),
        "google_cx": get("GOOGLE_CSE_CX"),
        "bing_key": get("BING_IMAGE_KEY"),
    }


def providers_available(cfg):
    p = []
    if cfg["google_key"] and cfg["google_cx"]:
        p.append("google_cse")
    if cfg["bing_key"]:
        p.append("bing")
    return p


def load_index():
    """CSV files in media_index/. Columns: key,image_url (or sku/mpn,image_url)."""
    import csv
    idx = {}
    for path in sorted(INDEX_DIR.glob("*.csv")):
        with path.open(newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            if not reader.fieldnames:
                continue
            cols = {c.strip().lower(): c for c in reader.fieldnames}
            url_col = cols.get("image_url") or cols.get("url")
            if not url_col:
                continue
            key_cols = [cols[c] for c in ("key", "sku", "mpn") if c in cols]
            for row in reader:
                url = (row.get(url_col) or "").strip()
                if not url:
                    continue
                for kc in key_cols:
                    k = (row.get(kc) or "").strip().upper()
                    if k:
                        idx.setdefault(k, url)
    return idx


def load_cache():
    if CACHE_FILE.is_file():
        try:
            data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "entries" in data:
                return data
        except Exception:
            pass
    return {"version": 1, "entries": {}}


def save_cache(cache):
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CACHE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache, indent=2), encoding="utf-8")
    tmp.replace(CACHE_FILE)


def cache_lookup(cache, key):
    e = cache["entries"].get(key)
    if not e:
        return None
    if e.get("status") == "found":
        return e
    checked = e.get("checked_at")
    try:
        dt = datetime.fromisoformat(checked)
    except Exception:
        return None
    if datetime.now(timezone.utc) - dt < timedelta(days=NEGATIVE_TTL_DAYS):
        return e
    return None


def is_public_url(url):
    try:
        p = urlparse(url)
    except Exception:
        return False
    if p.scheme not in ("http", "https") or not p.hostname:
        return False
    if p.username or p.password:
        return False
    if p.port and p.port not in (80, 443):
        return False
    try:
        infos = socket.getaddrinfo(p.hostname, None)
    except Exception:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            return False
    return True


class SearchBudget:
    def __init__(self, limit):
        self.limit = limit
        self.used = 0

    def take(self):
        if self.used >= self.limit:
            return False
        self.used += 1
        return True


def search_google(query, cfg, num=5):
    r = requests.get(
        "https://www.googleapis.com/customsearch/v1",
        params={"key": cfg["google_key"], "cx": cfg["google_cx"], "q": query,
                "searchType": "image", "num": num, "safe": "active"},
        headers={"User-Agent": UA}, timeout=(5, 20))
    r.raise_for_status()
    return [i.get("link") for i in r.json().get("items", []) if i.get("link")]


def search_bing(query, cfg, num=5):
    r = requests.get(
        "https://api.bing.microsoft.com/v7.0/images/search",
        params={"q": query, "count": num, "safeSearch": "Moderate",
                "imageType": "Photo"},
        headers={"Ocp-Apim-Subscription-Key": cfg["bing_key"],
                 "User-Agent": UA}, timeout=(5, 20))
    r.raise_for_status()
    return [i.get("contentUrl") for i in r.json().get("value", [])
            if i.get("contentUrl")]


def fetch(url, max_redirects=3):
    for _ in range(max_redirects + 1):
        if not is_public_url(url):
            raise ValueError("blocked non-public url")
        r = requests.get(url, headers={"User-Agent": UA}, stream=True,
                         timeout=(5, 25), allow_redirects=False)
        if r.status_code in (301, 302, 303, 307, 308):
            nxt = r.headers.get("Location")
            r.close()
            if not nxt:
                raise ValueError("redirect without location")
            url = nxt
            continue
        r.raise_for_status()
        ctype = (r.headers.get("Content-Type") or "").split(";")[0].lower()
        if not ctype.startswith("image/"):
            r.close()
            raise ValueError("not an image content-type: " + ctype)
        buf = BytesIO()
        for chunk in r.iter_content(65536):
            buf.write(chunk)
            if buf.tell() > MAX_BYTES:
                r.close()
                raise ValueError("image exceeds size limit")
        r.close()
        return buf.getvalue(), url
    raise ValueError("too many redirects")


def validate_normalize(raw):
    probe = Image.open(BytesIO(raw))
    probe.verify()
    img = Image.open(BytesIO(raw))
    img.load()
    fmt = (img.format or "").upper()
    if fmt not in {"JPEG", "PNG", "WEBP", "GIF", "BMP", "TIFF"}:
        raise ValueError("unsupported format: " + fmt)
    w, h = img.size
    if min(w, h) < MIN_SIDE:
        raise ValueError("image too small: %dx%d" % (w, h))
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        ext = "png"
    else:
        img = img.convert("RGB")
        ext = "jpg"
    img.thumbnail((TARGET_SIDE, TARGET_SIDE))
    out = BytesIO()
    if ext == "png":
        img.save(out, "PNG", optimize=True)
    else:
        img.save(out, "JPEG", quality=88, optimize=True)
    data = out.getvalue()
    return data, ext, img.size[0], img.size[1], hashlib.sha256(data).hexdigest()


def candidates_for(key, mpn, query, cfg, index, cache, budget, allow_search, sleep_s):
    """Return (url, source) or (None, reason). Updates cache for negatives."""
    for k in (key, (mpn or "").upper()):
        if k and k in index:
            return index[k], "index"
    hit = cache_lookup(cache, key)
    if hit:
        if hit.get("status") == "found":
            return hit.get("url"), "cache"
        return None, "cache_negative"
    if not allow_search:
        return None, "needs_search"
    for provider in providers_available(cfg):
        if not budget.take():
            return None, "search_budget_exhausted"
        try:
            if provider == "google_cse":
                urls = search_google(query, cfg)
            else:
                urls = search_bing(query, cfg)
        except Exception as exc:
            cache["entries"][key] = {"status": "error", "checked_at": utc_now(),
                                     "source": provider, "error": str(exc)[:300]}
            return None, "search_error:" + provider
        time.sleep(sleep_s)
        for u in urls:
            if u and is_public_url(u):
                return u, provider
    cache["entries"][key] = {"status": "not_found", "checked_at": utc_now(),
                             "source": "search"}
    return None, "not_found"
