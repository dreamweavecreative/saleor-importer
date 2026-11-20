# CAAG Saleor Importer - Reference Documentation

## 1. System Architecture Overview

```text
+---------------------+      +-------------------------+      +-----------------------+
|  Supplier Feeds     |      |  CAAG Importer Engine   |      |  Saleor Backend       |
|  (CSV, XLSX, ODS)   | ---> |  (Python / Django ORM)  | ---> |  (PostgreSQL DB)      |
+---------------------+      +-------------------------+      +-----------------------+
          |                           |                                |
          v                           v                                v
+---------------------+      +-------------------------+      +-----------------------+
|  /incoming/         |      |  - normalizers.py       |      |  - product_product    |
|  - DickerData.csv   | ---> |  - category_resolver.py | ---> |  - product_variant    |
|  - test_feed.csv    |      |  - saleor_writer.py     |      |  - warehouse_stock    |
+---------------------+      |  - post_import.py       |      |  - channel_channel    |
                             +-------------------------+      +-----------------------+
                                      |
                                      v
                             +-------------------------+
                             |  post_import.py         |
                             |  (Search, Discounts,    |
                             |   Cleanup, Orphans)     |
                             +-------------------------+
```

## 2. Directory Structure

```text
/opt/saleor-caag/imports/
├── incoming/               # Incoming supplier feeds (CSV, XLSX, ODS)
├── processed/              # Successfully processed feeds (archived)
├── failed/                 # Failed imports with error logs (JSONL)
├── logs/                   # Execution logs and JSON reports
│   └── executions/         # Detailed execution manifests per run
├── backups/                # Database and code backups (auto-cleaned)
├── cache/                  # Local caches (e.g., image_lookup.json)
├── media_index/            # SKU to Image URL mapping files
├── templates/              # Template CSVs for manual imports
└── importer/               # The core importer application
    ├── runner.py           # Main CLI entry point
    ├── config/             # Configuration (suppliers.py, taxonomy.py)
    └── engine/             # Core processing engine
        ├── normalizers.py          # Row validation and normalization
        ├── canonical/              # Identity resolution & reconciliation
        │   ├── identity.py         # SKU/Product matching logic
        │   └── reconciliation.py   # Canonical record reconciliation
        ├── category_resolver.py    # Maps supplier categories to Saleor taxonomy
        ├── saleor_writer.py        # Writes normalized data to Saleor ORM
        ├── post_import.py          # Post-import cleanup (search, discounts, orphans)
        ├── pricing.py              # Pricing safety checks
        ├── products.py             # Product enrichment helpers
        ├── variants.py             # Variant adjustment helpers
        └── search_index.py         # Search vector rebuilding logic
```

## 3. Execution Flow

```text
[START] runner.py
   │
   ├─> 1. Load & Validate Feed (normalizers.py)
   │      └─> Validates headers, data types, and required fields.
   │
   ├─> 2. Identity Resolution (canonical/identity.py)
   │      └─> Checks if SKU exists. If yes -> UPDATE. If no -> CREATE.
   │
   ├─> 3. Reconciliation (canonical/reconciliation.py)
   │      └─> Maps supplier data to canonical product/variant structure.
   │
   ├─> 4. Write to Database (saleor_writer.py)
   │      ├─> Resolves Category (category_resolver.py)
   │      ├─> Enriches Product (products.py)
   │      ├─> Adjusts Variant (variants.py)
   │      └─> Commits Product, Variant, Pricing, and Metadata.
   │
   ├─> 5. Post-Import Cleanup (post_import.py)
   │      ├─> Rebuilds search vectors (search_index.py)
   │      ├─> Recalculates discounted prices
   │      ├─> Cleans up orphaned products
   │      └─> Fixes fabricated weights & inventory flags
   │
   └─> [END] Generates execution manifest in /logs/executions/
```

## 4. Key Files & Responsibilities

| File | Responsibility |
|------|----------------|
| `runner.py` | CLI entry point, orchestrates the import pipeline, handles execution locking. |
| `engine/normalizers.py` | Parses raw CSV rows, validates data types, enforces schema constraints. |
| `engine/canonical/identity.py` | Determines if a row is a new product or an update to an existing one based on SKU. |
| `engine/saleor_writer.py` | The core writer. Creates/updates Products, Variants, Channel Listings, and Metadata. |
| `engine/post_import.py` | Runs after successful write: updates search indexes, cleans orphans, fixes pricing anomalies. |
| `config/taxonomy.py` | Defines the mapping from supplier categories to canonical Saleor categories. |

## 5. Maintenance & Cleanup Commands

To clean up old backups and keep only the 3 most recent:
```bash
cd /opt/saleor-caag/imports/backups
ls -t | tail -n +4 | xargs -d '\n' rm -rf
```

To view the latest execution manifest:
```bash
cat /opt/saleor-caag/imports/logs/executions/*.json | tail -n 50
```
