"""Build canonical categories + collections. Dry-run by default."""
import os, sys, json, argparse
sys.path.insert(0, "/app"); sys.path.insert(0, "/opt/saleor-caag/imports")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "saleor.settings")
import django; django.setup()
from importer.engine.categories import ensure_canonical_tree
from importer.engine.catalog_collections import ensure_collections
p = argparse.ArgumentParser(); p.add_argument("--apply", action="store_true")
a = p.parse_args(); dry = not a.apply
print("MODE:", "APPLY" if a.apply else "DRY RUN")
print("CATEGORIES:", json.dumps(ensure_canonical_tree(dry), indent=2))
print("COLLECTIONS:", json.dumps(ensure_collections(dry), indent=2))
print("PRODUCT WRITES: NONE")
