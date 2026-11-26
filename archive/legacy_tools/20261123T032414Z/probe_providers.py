#!/usr/bin/env python3
"""READ-ONLY: fonts + live SerpAPI/Exa response shapes. No writes, no secrets printed."""
import os, sys, glob, json
sys.path.insert(0, "/app"); sys.path.insert(0, "/tmp")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "saleor.settings")
import django; django.setup()          # MUST precede engine imports

import requests
from imports.importer.engine import image_lookup as lookup

def sec(t): print("\n" + "="*78 + "\n" + t + "\n" + "="*78, flush=True)

sec("1. FONTS / PILLOW (card quality depends on this)")
import PIL
from PIL import ImageFont
print("  Pillow:", PIL.__version__)
try:
    f = ImageFont.load_default(size=40)
    print("  load_default(size=40): OK ->", type(f).__name__)
except TypeError:
    print("  load_default(size=...) NOT supported -> bitmap fallback only")
found = []
for pat in ("/usr/share/fonts/**/*.ttf", "/usr/share/fonts/**/*.otf",
            "/usr/local/share/fonts/**/*.ttf"):
    found += glob.glob(pat, recursive=True)
print("  truetype files:", len(found))
for f in sorted(found)[:15]:
    print("   ", f)

sec("2. CREDENTIAL PRESENCE (names + lengths only)")
cfg = lookup.load_env_file()
print("  env file:", lookup.ENV_FILE, "| exists:", lookup.ENV_FILE.is_file())
def key(k): return os.environ.get(k) or cfg.get(k) or ""
for k in ("SERP_API_KEY", "EXA_API_KEY", "SCRAPPA_API_KEY",
          "GOOGLE_CSE_KEY", "GOOGLE_CSE_CX", "BING_IMAGE_KEY"):
    v = key(k)
    print("  %-16s present=%-5s len=%d" % (k, bool(v), len(v)))

Q = '"Lenovo" "21TD0016AU"'

sec("3. SERPAPI google_images — LIVE")
sk = key("SERP_API_KEY")
if not sk:
    print("  no key")
else:
    try:
        r = requests.get("https://serpapi.com/search",
            params={"engine": "google_images", "q": Q, "api_key": sk,
                    "gl": "au", "hl": "en", "ijn": 0, "safe": "active"},
            timeout=(5, 40))
        print("  HTTP", r.status_code)
        if r.status_code != 200:
            print("  body:", r.text[:700])
        else:
            d = r.json()
            print("  top-level keys:", sorted(d.keys())[:20])
            print("  search_metadata.status:",
                  (d.get("search_metadata") or {}).get("status"))
            arr = d.get("images_results") or d.get("image_results") or []
            print("  results:", len(arr))
            for it in arr[:3]:
                print("   ", json.dumps({k: it.get(k) for k in
                    ("title", "source", "link", "original",
                     "original_width", "original_height",
                     "is_product", "unsafe")}, default=str)[:520])
    except Exception as e:
        print("  ERROR", type(e).__name__, e)

sec("4. EXA /search — LIVE (probe extras param)")
ek = key("EXA_API_KEY")
if not ek:
    print("  no key")
else:
    for body in (
        {"query": "Lenovo 21TD0016AU official product page", "type": "auto",
         "numResults": 3, "contents": {"text": True, "extras": {"imageLinks": 5}}},
        {"query": "Lenovo 21TD0016AU official product page", "type": "auto",
         "numResults": 3, "contents": {"text": True}},
    ):
        try:
            r = requests.post("https://api.exa.ai/search",
                headers={"x-api-key": ek, "Content-Type": "application/json"},
                json=body, timeout=(5, 40))
            print("  contents =", json.dumps(body["contents"]))
            print("  HTTP", r.status_code)
            if r.status_code != 200:
                print("  err:", r.text[:400]); continue
            d = r.json()
            for res in (d.get("results") or [])[:3]:
                ex = res.get("extras") or {}
                print("   ", json.dumps({
                    "title": res.get("title"), "url": res.get("url"),
                    "image": res.get("image"),
                    "imageLinks": ex.get("imageLinks"),
                    "richImageLinks": ex.get("richImageLinks"),
                }, default=str)[:620])
            print("  costDollars:", d.get("costDollars"))
            break
        except Exception as e:
            print("  ERROR", type(e).__name__, e)

sec("5. SCAPPA — NOT probed (endpoint unknown)")
print("  scrappra.co/docs/api could not be extracted.")
print("  Supply base URL + auth style; no endpoint guessed.")
print("\nREAD-ONLY COMPLETE")
