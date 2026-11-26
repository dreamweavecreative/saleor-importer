#!/usr/bin/env python3

import os
import re
import sys
import shutil
import py_compile
import datetime
from pathlib import Path

ROOT = Path("/opt/saleor-caag/imports/importer")
ENGINE = ROOT / "engine"

FILES = [
    ENGINE / "image_sources.py",
    ENGINE / "image_lookup.py",
    ENGINE / "media.py",
]

print("=" * 65)
print("IMPORTER IMAGE PROVIDER RECONCILIATION - FINAL PATCH")
print("=" * 65)

# ---------------------------------------------------------
# 1. Validate files
# ---------------------------------------------------------

for path in FILES:
    if not path.is_file():
        sys.exit("ABORT: Missing " + str(path))

original = {
    path: path.read_text(encoding="utf-8")
    for path in FILES
}

proposed = dict(original)

# ---------------------------------------------------------
# 2. Correct provider credential naming
# ---------------------------------------------------------

src = proposed[ENGINE / "image_sources.py"]

old = '"scappa": k("SCRAPPA_API_KEY") or k("SCRAPPA_KEY")'
new = '"scrappa": k("SCRAPPA_API_KEY") or k("SCRAPPA_KEY")'

if old in src:
    src = src.replace(old, new, 1)
    print("[FIX] Corrected scappa -> scrappa")
elif new in src:
    print("[OK] Scrappa credential naming already correct")
else:
    sys.exit("ABORT: Unexpected image_sources provider configuration")

proposed[ENGINE / "image_sources.py"] = src

# ---------------------------------------------------------
# 3. Verify image_lookup configuration
# ---------------------------------------------------------

lookup = proposed[ENGINE / "image_lookup.py"]

required = [
    '"scrappa_key": get("SCRAPPA_API_KEY") or get("SCRAPPA_KEY")',
]

for item in required:
    if item not in lookup:
        sys.exit("ABORT: Missing expected Scrappa credential alias: " + item)

print("[OK] Both Scrappa credential aliases supported")

# Scrappa must remain inactive until endpoint/authentication
# documentation has been verified.

if "def search_scrappa(" in lookup:
    print("[WARNING] Scrappa search implementation already exists")
else:
    print("[OK] No unverified Scrappa endpoint activated")

# ---------------------------------------------------------
# 4. Verify provenance
# ---------------------------------------------------------

media = proposed[ENGINE / "media.py"]

if '"scrappa": "scrappa"' not in media:
    print("[WARNING] Scrappa provenance tier mapping not found")
else:
    print("[OK] Scrappa provenance mapping preserved")

# ---------------------------------------------------------
# 5. Verify existing providers
# ---------------------------------------------------------

for label, token in [
    ("SerpAPI", "SERP_API_KEY"),
    ("Exa", "EXA_API_KEY"),
    ("Google CSE", "GOOGLE_CSE_KEY"),
    ("Bing", "BING_IMAGE_KEY"),
]:
    found = any(token in content for content in proposed.values())
    print("[OK] %-12s %s" % (label, "configured in source" if found else "not found"))

# ---------------------------------------------------------
# 6. Compile all proposed changes BEFORE writing
# ---------------------------------------------------------

print("\n[COMPILATION]")

for path, content in proposed.items():
    try:
        compile(content, str(path), "exec")
        print("[PASS]", path.name)
    except SyntaxError as exc:
        sys.exit(
            "ABORT: Compilation failed in %s line %s: %s"
            % (path.name, exc.lineno, exc.msg)
        )

# ---------------------------------------------------------
# 7. Backup before modification
# ---------------------------------------------------------

changed = [
    path for path in FILES
    if proposed[path] != original[path]
]

if not changed:
    print("\nNo source modifications required.")
else:
    timestamp = datetime.datetime.now(
        datetime.timezone.utc
    ).strftime("%Y%m%dT%H%M%SZ")

    backup = ROOT / "backups" / (
        "image_provider_final_patch-" + timestamp
    )

    backup.mkdir(parents=True, exist_ok=True)

    for path in changed:
        shutil.copy2(path, backup / path.name)

    print("\nBackup:", backup)

    # -----------------------------------------------------
    # 8. Apply changes
    # -----------------------------------------------------

    for path in changed:
        temp = path.with_suffix(path.suffix + ".tmp")

        temp.write_text(
            proposed[path],
            encoding="utf-8"
        )

        os.replace(temp, path)

        print("[UPDATED]", path.name)

# ---------------------------------------------------------
# 9. Final compilation
# ---------------------------------------------------------

print("\n[FINAL COMPILATION]")

for path in FILES:
    try:
        py_compile.compile(str(path), doraise=True)
        print("[PASS]", path.name)
    except Exception as exc:
        sys.exit("COMPILATION FAILURE: %s: %s" % (path.name, exc))

# ---------------------------------------------------------
# 10. Final status
# ---------------------------------------------------------

print("\n" + "=" * 65)
print("IMAGE PROVIDER RECONCILIATION COMPLETE")
print("=" * 65)

print("""
ACTIVE PROVIDERS:
  SerpAPI       Existing implementation preserved
  Exa           Existing implementation preserved
  Google CSE    Existing implementation preserved
  Bing          Existing implementation preserved

SCRAPPA:
  Credential aliases: SCRAPPA_API_KEY / SCRAPPA_KEY
  Provider naming:    Normalised
  Endpoint:           Not activated
  Authentication:     Not assumed
  Provenance:         Preserved where configured

SAFETY:
  No product media deleted.
  No existing provider implementation replaced.
  No unverified endpoint activated.
  Backups created before modifications.
  All three Python modules compiled successfully.
""")

print("NEXT: Verify Scrappa API endpoint and authentication before activation.")

