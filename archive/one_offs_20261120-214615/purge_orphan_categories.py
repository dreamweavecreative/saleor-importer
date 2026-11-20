import os, sys, json, argparse
from pathlib import Path
from datetime import datetime, timezone
os.environ.setdefault("DJANGO_SETTINGS_MODULE","saleor.settings")
sys.path.insert(0,"/app"); sys.path.insert(0,"/opt/saleor-caag/imports")
import django; django.setup()
from django.db import transaction
from saleor.product.models import Category

ap = argparse.ArgumentParser(); ap.add_argument("--confirm", default=""); a = ap.parse_args()
CANON = {"computers","laptops","laptops-13-inch","laptops-14-inch","laptops-15-inch",
         "laptops-16-inch","laptops-17-inch","accessories","bags-and-cases"}
orphans = [c for c in Category.objects.exclude(slug__in=CANON).order_by("-level")
           if c.products.count() == 0]
print("empty supplier categories:", len(orphans))
if a.confirm != "PURGE":
    print("dry-run; re-run with --confirm PURGE"); raise SystemExit(0)
ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
bak = Path(f"/opt/saleor-caag/imports/backups/orphan-categories-{ts}.json")
bak.write_text(json.dumps([{"id":c.id,"slug":c.slug,"name":c.name} for c in orphans], indent=2))
with transaction.atomic():
    for c in orphans:
        if c.products.count() == 0:   # re-check at delete time
            c.delete()
print("purged:", len(orphans), "| backup:", bak)
print("remaining categories:", Category.objects.count())
