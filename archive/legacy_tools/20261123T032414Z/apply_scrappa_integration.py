#!/usr/bin/env python3
"""Apply Scrappa image-source integration to the importer engine.

Idempotent (marker: SCRAPPA-INTEGRATION-V1). Two-phase: every anchor must
match exactly once across all targets before any file is written.
Backs up each target to backups/apply_scrappa_integration-<utc>/ first.
"""
import argparse
import datetime
import difflib
import py_compile
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ENGINE = ROOT / "engine"
MARKER = "SCRAPPA-INTEGRATION-V1"

IL = ENGINE / "image_lookup.py"
MD = ENGINE / "media.py"

PATCHES = {
    IL: [
        # 1. module marker
        ('"""\nimport hashlib, ipaddress, json, os, re, socket, time',
         '"""\n# ' + MARKER + '\nimport hashlib, ipaddress, json, os, re, socket, time'),
        # 2. config keys
        ('        "bing_key": get("BING_IMAGE_KEY"),\n    }',
         '        "bing_key": get("BING_IMAGE_KEY"),\n'
         '        "scrappa_url": get("SCRAPPA_URL"),\n'
         '        "scrappa_key": get("SCRAPPA_KEY"),\n    }'),
        # 3. provider chain: scrappa first (self-hosted, no quota)
        ('    p = []\n    if cfg["google_key"] and cfg["google_cx"]:\n'
         '        p.append("google_cse")\n    if cfg["bing_key"]:\n'
         '        p.append("bing")\n    return p',
         '    p = []\n    if cfg.get("scrappa_url") and cfg.get("scrappa_key"):\n'
         '        p.append("scrappa")\n    if cfg["google_key"] and cfg["google_cx"]:\n'
         '        p.append("google_cse")\n    if cfg["bing_key"]:\n'
         '        p.append("bing")\n    return p'),
        # 4. scrappa search implementation
        ('    return [i.get("contentUrl") for i in r.json().get("value", [])\n'
         '            if i.get("contentUrl")]',
         '    return [i.get("contentUrl") for i in r.json().get("value", [])\n'
         '            if i.get("contentUrl")]\n'
         '\n\n'
         'def search_scrappa(query, cfg, num=5):\n'
         '    """Scrappa (self-hosted) image search. Tolerant JSON shapes."""\n'
         '    base = cfg["scrappa_url"].rstrip("/")\n'
         '    r = requests.get(\n'
         '        base + "/search",\n'
         '        params={"q": query, "count": num, "safe": "strict"},\n'
         '        headers={"Authorization": "Bearer " + cfg["scrappa_key"],\n'
         '                 "User-Agent": UA},\n'
         '        timeout=(5, 20))\n'
         '    r.raise_for_status()\n'
         '    payload = r.json()\n'
         '    if isinstance(payload, dict):\n'
         '        for key in ("results", "items", "images", "value", "urls"):\n'
         '            if isinstance(payload.get(key), list):\n'
         '                payload = payload[key]\n'
         '                break\n'
         '        else:\n'
         '            payload = []\n'
         '    urls = []\n'
         '    for item in payload:\n'
         '        if isinstance(item, str):\n'
         '            u = item\n'
         '        elif isinstance(item, dict):\n'
         '            u = (item.get("url") or item.get("link")\n'
         '                 or item.get("image_url") or item.get("contentUrl"))\n'
         '        else:\n'
         '            u = None\n'
         '        if u:\n'
         '            urls.append(u)\n'
         '    return urls[:num]'),
        # 5. dispatch in candidates_for
        ('            if provider == "google_cse":\n'
         '                urls = search_google(query, cfg)\n'
         '            else:\n'
         '                urls = search_bing(query, cfg)',
         '            if provider == "google_cse":\n'
         '                urls = search_google(query, cfg)\n'
         '            elif provider == "scrappa":\n'
         '                urls = search_scrappa(query, cfg)\n'
         '            else:\n'
         '                urls = search_bing(query, cfg)'),
    ],
    MD: [
        # 1. module marker
        ('"""\nimport logging',
         '"""\n# ' + MARKER + '\nimport logging'),
        # 2. provenance tier map incl. scrappa
        ('logger = logging.getLogger(__name__)',
         'logger = logging.getLogger(__name__)\n'
         '\n'
         'TIER_BY_SOURCE = {\n'
         '    "index": "index",\n'
         '    "cache": "cache",\n'
         '    "scrappa": "scrappa",\n'
         '    "google_cse": "search",\n'
         '    "bing": "search",\n'
         '}'),
        # 3. idempotent attach: same sha256 already attached -> no-op
        ('    try:\n        _drop_placeholders(product)',
         '    try:\n'
         '        for existing in product.media.all():\n'
         '            if ((existing.metadata or {}).get("image_provenance", {})\n'
         '                    .get("sha256") == sha):\n'
         '                return True\n'
         '        _drop_placeholders(product)'),
        # 4. default source_tier from source name
        ('        prov = dict(prov)\n        prov.update({"status": status, "sha256": sha,',
         '        prov = dict(prov)\n'
         '        prov.setdefault("source_tier",\n'
         '                        TIER_BY_SOURCE.get(prov.get("source", ""), "search"))\n'
         '        prov.update({"status": status, "sha256": sha,'),
    ],
}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="print unified diffs, write nothing")
    args = ap.parse_args()

    # ---- phase 1: validate ----
    prepared = {}
    for path, chunks in PATCHES.items():
        if not path.is_file():
            sys.exit("ABORT: missing target %s" % path)
        src = path.read_text(encoding="utf-8")
        if MARKER in src:
            prepared[path] = None  # already patched
            continue
        new = src
        for anchor, repl in chunks:
            hits = new.count(anchor)
            if hits != 1:
                sys.exit("ABORT: %s anchor matched %d times (expected 1): %r"
                         % (path.name, hits, anchor[:70]))
            new = new.replace(anchor, repl)
        if MARKER not in new:
            sys.exit("ABORT: marker not present after patching %s" % path.name)
        prepared[path] = new

    # ---- phase 1b: compile ALL proposed files in memory ----
    # Nothing has been written to production at this point.
    for path, new in prepared.items():
        if new is None:
            continue
        try:
            compile(new, str(path), "exec")
            print("%-16s proposed source compiled successfully" % path.name)
        except SyntaxError as exc:
            sys.exit(
                "ABORT: proposed syntax error in %s at line %s: %s"
                % (path.name, exc.lineno, exc.msg)
            )
        except Exception as exc:
            sys.exit(
                "ABORT: could not validate %s: %s"
                % (path.name, exc)
            )

    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_dir = ROOT / "backups" / ("apply_scrappa_integration-" + ts)

    # ---- phase 2: diff / write ----
    for path, new in prepared.items():
        if new is None:
            print("%-16s already patched - skipped" % path.name)
            continue
        src = path.read_text(encoding="utf-8")
        diff = "".join(difflib.unified_diff(
            src.splitlines(True), new.splitlines(True),
            fromfile=str(path) + " (current)",
            tofile=str(path) + " (patched)"))
        if args.dry_run:
            print(diff)
            print("%-16s dry-run diff printed" % path.name)
            continue
        backup_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, backup_dir / path.name)
        path.write_text(new, encoding="utf-8")
        py_compile.compile(str(path), doraise=True)
        print("%-16s patched + compiled (backup: %s)"
              % (path.name, backup_dir / path.name))

    if args.dry_run:
        print("DRY RUN - no files modified")
    else:
        print("DONE - Scrappa integration applied (marker %s)." % MARKER)


if __name__ == "__main__":
    main()
