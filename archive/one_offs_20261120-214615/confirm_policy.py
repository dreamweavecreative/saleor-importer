#!/usr/bin/env python3
"""Interactive pricing/scope/publication confirmation with real feed samples."""
import os, sys, json
from pathlib import Path
from decimal import Decimal as D, ROUND_HALF_UP
from datetime import datetime, timezone

ROOT = Path("/opt/saleor-caag/imports")
sys.path.insert(0, "/app")
sys.path.insert(1, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "saleor.settings")
import django; django.setup()
from django.db import connection, transaction
from importer.engine.readers import read_supplier_file
from importer.engine.production_safety import analyse_pricing

BLOCK = {"INVALID_PRICE", "ZERO_RRP", "NEGATIVE_MARGIN", "ZERO_MARGIN"}
GST = D("1.10")
def money(x): return D(x).quantize(D("0.01"), rounding=ROUND_HALF_UP)
def dec(v):
    try: return D(str(v or "0").replace(",", "").strip() or "0")
    except Exception: return D("0")

print("=" * 78)
print("POLICY CONFIRMATION — REAL SAMPLES FROM DickerData.csv")
print("=" * 78)
print("Scanning feed (75,315 rows) ...", flush=True)

buckets = {"import": [], "low": [], "quarantine": []}
counts = {"import": 0, "low": 0, "quarantine": 0}
reasons = {}
for rn, rec in read_supplier_file(ROOT / "incoming/DickerData.csv"):
    if (rec.get("Status") or "").strip() != "A":
        continue
    try: pr = analyse_pricing(rec)
    except TypeError: pr = analyse_pricing(rec, None)
    w = pr.get("warning")
    b = "quarantine" if w in BLOCK else ("low" if w == "LOW_MARGIN" else "import")
    counts[b] += 1
    if b == "quarantine": reasons[w] = reasons.get(w, 0) + 1
    if len(buckets[b]) < 3:
        rrp = dec(rec.get("RRPEx"))
        cost = dec(rec.get("DealerEx"))
        margin = ((rrp - cost) / rrp * 100) if rrp > 0 else D("0")
        buckets[b].append({
            "sku": rec.get("StockCode"), "cat": rec.get("PrimaryCategory"),
            "desc": (rec.get("StockDescription") or "")[:58],
            "rrp": rrp, "cost": cost, "sell": money(rrp * GST),
            "margin": margin.quantize(D("0.01")), "warn": w or "OK"})

total = sum(counts.values())
def show(rows):
    print(f"    {'SKU':22} {'CAT':14} {'RRP ex':>9} {'Cost ex':>9} {'Sell inc':>10} {'Margin':>8}  DESC")
    for r in rows:
        print(f"    {r['sku'][:22]:22} {(r['cat'] or '')[:14]:14} {r['rrp']:>9} {r['cost']:>9} "
              f"{r['sell']:>10} {str(r['margin'])+'%':>8}  {r['desc']}")

print(f"\nACTIVE ROWS: {total:,}")
print(f"  IMPORT      : {counts['import']:,}")
print(f"  LOW-MARGIN  : {counts['low']:,}  (imported with warning)")
print(f"  QUARANTINE  : {counts['quarantine']:,}  {reasons}")

decisions = {}

print("\n" + "-" * 78)
print("POLICY 1/3 — SELECTION SCOPE")
print(f"  Import {counts['import'] + counts['low']:,} viable rows; exclude {counts['quarantine']:,} blocking rows.")
print("  No category/vendor filter. Require stock: NO (supplier-backed).")
print("\n  SAMPLE — WOULD IMPORT:")
show(buckets["import"])
print("\n  SAMPLE — WOULD QUARANTINE (not imported, reported):")
show(buckets["quarantine"])
decisions["selection"] = input("\n  Approve SELECTION scope? [yes/no]: ").strip().lower()

print("\n" + "-" * 78)
print("POLICY 2/3 — PRICING")
print("  Sell price = RRPEx x 1.10 (AU GST). Channel enters prices WITH tax.")
print("  Cost price = DealerEx (ex GST, private).")
print("  Blocking margins -> quarantine. Low margins -> import with warning.")
print("\n  SAMPLE — LOW-MARGIN (imported with warning):")
show(buckets["low"])
decisions["pricing"] = input("\n  Approve PRICING policy? [yes/no]: ").strip().lower()

print("\n" + "-" * 78)
print("POLICY 3/3 — PUBLICATION & INVENTORY")
print("  publish_on_import        = True  (live in default-channel immediately)")
print("  available_for_purchase   = True")
print("  track_inventory          = False (supplier-backed; no warehouse stock needed)")
print(f"  Effect: {counts['import'] + counts['low']:,} products become buyable on next import.")
print("  WARNING: weight is NULL on nearly all products -> orders fall in 0-2kg band.")
decisions["publication"] = input("\n  Approve PUBLICATION policy? [yes/no]: ").strip().lower()

approved = [k for k, v in decisions.items() if v in ("y", "yes")]
declined = [k for k, v in decisions.items() if k not in approved]

if approved:
    with transaction.atomic():
        with connection.cursor() as cur:
            for k in approved:
                cur.execute("""UPDATE catalogue.import_policy
                               SET requires_confirmation = FALSE, updated_at = NOW()
                               WHERE policy_key = %s""", [k])
    print(f"\nCONFIRMED: {approved}")
if declined:
    print(f"NOT CONFIRMED (still blocked): {declined}")
    print("  Tell me what to change and I'll amend the policy rows.")

with connection.cursor() as cur:
    cur.execute("SELECT policy_key, requires_confirmation FROM catalogue.import_policy ORDER BY 1")
    print("\nPOLICY STATE:")
    for k, rc in cur.fetchall():
        print(f"    {k:14} {'BLOCKED' if rc else 'CONFIRMED'}")
    cur.execute("SELECT COUNT(*) FROM catalogue.shipping_policy WHERE rate_aud IS NULL")
    print(f"\nShipping bands awaiting rates: {cur.fetchone()[0]}")

out = ROOT / "logs" / f"policy-confirmation-{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"
out.write_text(json.dumps({"decisions": decisions, "counts": counts,
                           "quarantine_reasons": reasons}, indent=2, default=str))
print(f"REPORT: {out}")
print("NO PRODUCTS IMPORTED BY THIS SCRIPT")
print("=" * 78)
