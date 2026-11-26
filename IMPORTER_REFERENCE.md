CAAG SALEOR IMPORTER
COMPLETE ARCHITECTURE AND OPERATIONS REFERENCE
================================================

Last updated: 2026-11-23
Project root: /opt/saleor-caag
Importer root: /opt/saleor-caag/imports/importer
Saleor backend: /opt/saleor-caag/backend

PURPOSE
=======

The CAAG importer exists to transform supplier catalogue data into accurate,
usable Saleor catalogue records. The intended end-to-end workflow includes:

  1. Supplier feed ingestion and validation.
  2. Product and variant identity resolution.
  3. Category, product type, attribute, and brand mapping.
  4. Product and variant creation or update in Saleor.
  5. Channel publication and pricing.
  6. Warehouse and stock handling where supported and approved.
  7. Product metadata and supplier provenance.
  8. Product image discovery, identity verification, and media attachment.
  9. Post-import validation and search/index maintenance.
 10. An execution report with row-level outcomes and recoverable errors.

A successful supplier reconciliation is not the same as a completed Saleor
catalogue import. These are separate stages and must be reported separately.

This document describes the actual inspected components, the Saleor data
relationships they are intended to work with, and the controls required to
complete the full workflow safely.

1. SYSTEM ARCHITECTURE - END TO END
===================================

                         SUPPLIER DATA SOURCES
              +--------------------------------------+
              | DickerData / Other Supplier Feeds    |
              | CSV / XLSX / ODS / JSON / XML        |
              +-------------------+------------------+
                                  |
                                  v
              +--------------------------------------+
              | /imports/incoming/                   |
              | Original feed and source fingerprint |
              +-------------------+------------------+
                                  |
                                  v
              +--------------------------------------+
              | IMPORTER INGESTION AND VALIDATION    |
              | normalizers.py                       |
              | validators.py                        |
              | config/suppliers.py                   |
              +-------------------+------------------+
                                  |
                                  v
              +--------------------------------------+
              | IDENTITY AND CANONICAL MAPPING       |
              | canonical/identity.py                |
              | canonical/reconciliation.py          |
              | canonical/supplier_import.py         |
              | canonical/supplier_offers.py         |
              | dickerdata_adapter.py                |
              +-------------------+------------------+
                                  |
                   +--------------+--------------+
                   |                             |
                   v                             v
       +-------------------------+   +---------------------------+
       | EXISTING SALEOR MATCH   |   | NEW PRODUCT / VARIANT     |
       | Exact SKU / safe key    |   | Candidate creation path   |
       | Conflict detection      |   | Requires writer approval  |
       +------------+------------+   +-------------+-------------+
                    |                              |
                    +---------------+--------------+
                                    |
                                    v
              +--------------------------------------+
              | SALEOR CATALOGUE WRITER              |
              | runner.py                            |
              | engine/orchestrator.py                |
              | engine/saleor_writer.py               |
              +-------------------+------------------+
                                  |
           +----------------------+----------------------+
           |             |              |                |
           v             v              v                v
      +---------+   +----------+   +-----------+   +------------+
      | Product |   | Variants |   | Categories|   | Attributes |
      +----+----+   +----+-----+   +-----------+   +------------+
           |             |
           |             +-------------------+
           |                                 |
           v                                 v
      +----------------+             +------------------+
      | Product Media  |             | Channel Listings |
      | Images         |             | Prices / Publish |
      +----------------+             +--------+---------+
                                               |
                                               v
                                      +------------------+
                                      | Channel          |
                                      | Currency / Sales |
                                      +------------------+

                         INVENTORY RELATIONSHIPS
              +------------------+       +------------------+
              | ProductVariant   |------>| Stock            |
              +------------------+       +--------+---------+
                                                  |
                                                  v
                                         +------------------+
                                         | Warehouse        |
                                         +------------------+

                         IMAGE SOURCING PIPELINE
              +--------------------------------------+
              | Product identity                     |
              | Manufacturer / Model / MPN / SKU     |
              +-------------------+------------------+
                                  |
                                  v
              +--------------------------------------+
              | Image candidate discovery            |
              | media_index / Icecat / Scrappa       |
              | SerpAPI / Exa / optional Google CSE  |
              +-------------------+------------------+
                                  |
                                  v
              +--------------------------------------+
              | Image identity and URL verification  |
              | image_identity.py                    |
              | image_verification.py                |
              | image_provenance.py                  |
              +-------------------+------------------+
                                  |
                                  v
              +--------------------------------------+
              | Download / validate / normalize      |
              | media_tasks.py                       |
              +-------------------+------------------+
                                  |
                                  v
              +--------------------------------------+
              | Saleor ProductMedia attachment       |
              | engine/media.py                      |
              +-------------------+------------------+
                                  |
                                  v
              +--------------------------------------+
              | Saleor Product                       |
              | ProductMedia relationship            |
              +--------------------------------------+

                                  |
                                  v
              +--------------------------------------+
              | POST-IMPORT VALIDATION                |
              | post_import.py                       |
              | search_index.py                      |
              | execution manifest and audit         |
              +--------------------------------------+

The diagram is a logical architecture map. Individual operations are
implemented by separate modules and must be checked against the deployed
Saleor version before assuming a particular model field or API operation.

2. THE THREE IMPORTER WORKFLOWS
===============================

The importer currently contains three distinct workflows.

A. Canonical supplier staging and reconciliation
--------------------------------------------------

Entry point:
  imports/importer/supplier_cli.py

Responsibilities:
  - Validate supplier feeds.
  - Preview supplier-to-Saleor identity mappings.
  - Stage supplier batch rows.
  - Reconcile supplier rows to canonical product and variant records.
  - Maintain supplier links, offers, and reconciliation history.

This workflow can associate supplier data with existing Saleor variants.
It does not, by itself, create Saleor products or variants.

B. Saleor catalogue writer
--------------------------

Entry point:
  imports/importer/runner.py

Main components:
  engine/orchestrator.py
  engine/saleor_writer.py
  engine/normalizers.py
  engine/validators.py
  engine/products.py
  engine/variants.py
  engine/pricing.py
  engine/post_import.py

This is the legacy catalogue import path inspected in the deployed code.
It is write-capable and can affect Saleor catalogue records.

The writer path must be the controlled destination for the full product
creation/update workflow. Its current behaviour must be validated before
using it for a new production feed.

C. Image discovery and attachment
---------------------------------

Discovery:
  engine/image_sources.py
  engine/image_lookup.py

Identity and provenance:
  engine/image_identity.py
  engine/image_provenance.py
  engine/image_verification.py

Attachment and media processing:
  engine/media_tasks.py
  engine/media.py
  engine/placeholder_image.py
  engine/product_cards.py

Image discovery and image attachment are different operations. Finding a
candidate does not mean that it has been verified or attached to Saleor.

3. SALEOR CATALOGUE MODEL RELATIONSHIPS
======================================

This section is the conceptual Saleor relationship map for the importer.
Exact model fields and constraints depend on the deployed Saleor version.
Inspect the installed backend models and migrations before implementing
or changing ORM writes.

3.1 Product type and product category
-------------------------------------

                  +----------------------+
                  | ProductType          |
                  |----------------------|
                  | Name / slug          |
                  | Variant configuration|
                  | Product attributes   |
                  | Variant attributes   |
                  +----------+-----------+
                             |
                             | classifies
                             v
                  +----------------------+
                  | Product              |
                  +----------------------+

                  +----------------------+
                  | ProductCategory      |
                  |----------------------|
                  | Name / slug          |
                  | Parent category      |
                  | Description          |
                  +----------+-----------+
                             |
                             | categorises
                             v
                  +----------------------+
                  | Product              |
                  +----------------------+

Importer responsibilities:
  - Resolve supplier category values to the Saleor category hierarchy.
  - Select the correct Saleor ProductType.
  - Ensure required product and variant attributes are compatible with
    that ProductType.
  - Avoid creating duplicate categories or product types.
  - Do not silently assign a product to a generic category when mapping
    is ambiguous.

ProductType controls the product's structural configuration, including
which product and variant attributes are available or required.

ProductCategory classifies the product in the storefront taxonomy. It is
not interchangeable with ProductType.

3.2 Product and product variants
--------------------------------

                  +----------------------+
                  | Product              |
                  |----------------------|
                  | Name                 |
                  | Slug                 |
                  | Description          |
                  | ProductType          |
                  | Category             |
                  | Metadata             |
                  +----------+-----------+
                             |
                             | one product
                             | has one or more variants
                             v
                  +----------------------+
                  | ProductVariant       |
                  |----------------------|
                  | SKU                  |
                  | Barcode              |
                  | Attributes           |
                  | Weight / dimensions  |
                  | Track inventory      |
                  | Metadata             |
                  +----------------------+

A Product is the catalogue-level item. A ProductVariant is a specific
purchasable configuration, usually identified by a SKU.

Importer identity rules:
  - A supplier SKU must not be treated as a product identity without
    considering whether it identifies a variant.
  - Exact SKU matching is a strong variant lookup when the source contract
    guarantees SKU uniqueness.
  - MPN alone is not a safe universal identity key.
  - Product-level fields must not be overwritten by conflicting
    variant-level supplier values.
  - A product with multiple variants must retain the correct parent-child
    relationship.

For DickerData, the inspected feed supports exact StockCode-to-Saleor SKU
matching for the existing variant set. That result does not prove that
every future supplier feed has the same identity guarantees.

3.3 Product and variant attributes
----------------------------------

                  +----------------------+
                  | Attribute            |
                  +----------+-----------+
                             |
                             v
                  +----------------------+
                  | AttributeValue       |
                  +----------+-----------+
                             |
               +-------------+-------------+
               |                           |
               v                           v
      +------------------+        +-------------------+
      | Product assigned |        | Variant assigned  |
      | attributes       |        | attributes        |
      +--------+---------+        +---------+---------+
               |                            |
               v                            v
          +---------+                  +-----------+
          | Product |                  | Variant   |
          +---------+                  +-----------+

Importer responsibilities:
  - Map supplier specifications to the correct Saleor attributes.
  - Distinguish product attributes from variant attributes.
  - Reuse existing attribute definitions and values where appropriate.
  - Avoid duplicate attribute values caused by inconsistent formatting.
  - Preserve meaningful units and normalized values.
  - Do not put model identifiers, colour, size, or other variant-defining
    data into the wrong level of the catalogue.

3.4 Channel and product channel listings
----------------------------------------

                  +----------------------+
                  | Channel              |
                  |----------------------|
                  | Slug                 |
                  | Currency             |
                  | Active status        |
                  +----------+-----------+
                             |
               +-------------+-------------+
               |                           |
               v                           v
      +----------------------+    +------------------------+
      | ProductChannelListing|    | VariantChannelListing  |
      |----------------------|    |------------------------|
      | Publication state    |    | Variant price          |
      | Availability dates   |    | Cost / pricing data    |
      | Visibility settings  |    | Channel-specific data  |
      +----------+-----------+    +-----------+------------+
                 |                            |
                 v                            v
             +---------+                  +-----------+
             | Product |                  | Variant   |
             +---------+                  +-----------+

A Saleor product existing in the database does not automatically mean it
is published or purchasable in every channel.

The importer must deliberately handle:
  - Which channels receive the product.
  - Product publication and visibility.
  - Channel-specific currency.
  - Variant channel price.
  - Pricing and tax treatment.
  - Availability and publication dates.

Do not assume that a supplier's cost price is a storefront selling price.
Pricing policy, currency conversion, tax treatment, and margin rules must
be explicit and independently validated.

3.5 Warehouse, stock, and variants
----------------------------------

                  +----------------------+
                  | ProductVariant       |
                  +----------+-----------+
                             |
                             | variant stock
                             v
                  +----------------------+
                  | Stock                |
                  |----------------------|
                  | Quantity             |
                  | Allocated quantity   |
                  | Warehouse reference  |
                  +----------+-----------+
                             |
                             v
                  +----------------------+
                  | Warehouse            |
                  |----------------------|
                  | Name                 |
                  | Slug                 |
                  | Address / settings   |
                  +----------------------+

Stock is associated with a variant and a warehouse. It is not simply a
free-standing quantity on the product.

Importer responsibilities:
  - Map supplier stock semantics to the correct Saleor inventory policy.
  - Distinguish physical stock from unlimited or unknown stock.
  - Avoid treating a supplier's sentinel value as a literal quantity.
  - Avoid overwriting warehouse stock with data that belongs to a different
    warehouse or fulfilment source.
  - Respect allocations and existing inventory controls.
  - Use an explicit policy for multiple suppliers reporting the same SKU.

The DickerData sentinel value 999999999 was mapped to unlimited/null stock
in its supplier offer representation. It must not be copied blindly into
a Saleor warehouse quantity.

3.6 Product media and images
----------------------------

                  +----------------------+
                  | Product              |
                  +----------+-----------+
                             |
                             | has media
                             v
                  +----------------------+
                  | ProductMedia         |
                  |----------------------|
                  | Image file / storage |
                  | Alt text             |
                  | Sort order           |
                  | Product relationship |
                  +----------------------+

A product image must ultimately be associated with the correct Saleor
Product through Saleor's media relationship.

The image importer must preserve:
  - The target Saleor product identity.
  - The image file and storage reference.
  - Useful alt text.
  - Display ordering.
  - Source URL and source provenance where appropriate.
  - Verification evidence and processing outcome.

Do not assume that an image URL in a supplier feed is already a valid
Saleor media file. The media workflow must validate and process the image
before creating the Saleor media relationship.

3.7 Collections, translations, and metadata
-------------------------------------------

                  +----------------------+
                  | Collection           |
                  +----------+-----------+
                             |
                             | groups products
                             v
                  +----------------------+
                  | Product              |
                  +----------------------+

                  +----------------------+
                  | Product              |
                  +----------+-----------+
                             |
                  +----------+-----------+
                  |                      |
                  v                      v
         +----------------+     +-------------------+
         | Translation    |     | Metadata          |
         | Name / text    |     | Key-value data    |
         +----------------+     +-------------------+

Products and variants may have translations and metadata. Metadata can
carry integration information such as supplier provenance, external
identifiers, or controlled importer state.

Importer responsibilities:
  - Do not overwrite unrelated metadata.
  - Namespace importer-owned metadata keys.
  - Keep secrets and provider credentials out of Saleor metadata.
  - Preserve existing translations unless an explicit update is approved.
  - Treat collection assignment as a separate merchandising decision.

3.8 Relationship summary
------------------------

Saleor conceptual relationship map:

  ProductType
      |
      +---- defines product / variant attribute configuration
      |
      v
  Product <-------------------- ProductCategory
      |
      +---- has ProductVariant records
      |
      +---- has ProductMedia records
      |
      +---- has ProductChannelListing records
      |
      +---- has Product attributes
      |
      +---- has translations and metadata
      |
      +---- can belong to Collections
      |
      +---- has ProductVariant records
                  |
                  +---- has Variant attributes
                  |
                  +---- has VariantChannelListing records
                  |
                  +---- has Stock records
                               |
                               +---- belongs to Warehouse

  Channel
      |
      +---- ProductChannelListing
      |
      +---- VariantChannelListing

This is a conceptual map, not a substitute for the deployed Saleor schema.
Before writing to any relation, confirm the actual model, foreign key,
unique constraints, required fields, and transaction behaviour in the
installed backend version.

4. ACTUAL CATALOGUE IMPORT EXECUTION FLOW
=========================================

Inspected legacy execution path:

  runner.py
      |
      v
  engine/orchestrator.py
      |
      +---- feed parsing and normalization
      |
      +---- identity and mapping logic
      |
      +---- category and product enrichment
      |
      +---- engine/saleor_writer.py
      |         |
      |         +---- Product writes
      |         +---- ProductVariant writes
      |         +---- related catalogue writes
      |
      +---- engine/post_import.py
                |
                +---- search/index maintenance
                +---- post-import checks
                +---- other configured post-import actions

Important:
  - runner.py is a write-capable legacy catalogue importer.
  - It can create a ProductType when the requested type is absent.
  - The orchestrator invokes the Saleor writer and post-import processing.
  - This path must not be treated as a read-only preview.
  - Its complete production behaviour must be tested before a broad run.

The actual order and exact write set must be confirmed from the deployed
source before every production release. Do not infer that every relationship
in the conceptual Saleor map is automatically populated by the current
writer.

5. CANONICAL SUPPLIER STAGING AND SALEOR LINKAGE
================================================

Entry point:
  supplier_cli.py

Commands:

  validate
      Read-only feed validation.

  preview
      Read-only supplier-to-Saleor variant mapping preview.

  stage
      Writes a supplier batch and staged rows.
      Requires --confirm-write.

  reconcile
      Writes supplier linkage, offers, and history.
      Requires --confirm-write.

  status
      Read-only batch status.

The supplier CLI deliberately does not invoke the legacy Saleor catalogue
writer or create products and variants.

The canonical reconciliation layer links supplier identities to Saleor
variants where safe matching is possible. This is a separate data layer
used to support supplier offers and future catalogue operations.

DICKERDATA PRODUCTION RESULT
----------------------------

Batch:
  78044adb-dff1-40ec-aff4-627fab329e1c

Feed:
  /opt/saleor-caag/imports/incoming/DickerData.csv

Verified results:
  Source rows:             75,315
  Eligible rows:           72,513
  Excluded rows:            2,802
  Staged rows:             72,513
  Matched rows:            72,513
  Unmatched rows:                0
  Conflict rows:                 0
  Invalid rows:                  0
  Pending rows:                  0
  Final status:           COMPLETED

An independent audit passed for canonical bridges, Saleor variant mapping,
supplier links, supplier offers, prices, currency, tax flags, stock mapping,
and reconciliation history.

DickerData identity rules:
  - StockCode is the authoritative supplier identity.
  - Matching to Saleor uses exact SKU equality.
  - MPN is not used as a fallback identity.
  - Vendor maps to brand, not manufacturer.
  - DealerEx is AUD ex-GST.
  - StockAvailable sentinel 999999999 maps to unlimited/null stock.

This completed reconciliation linked supplier data to existing Saleor
variants. It did not create products, variants, channel listings, warehouse
stock, or ProductMedia records.

Do not rerun this completed batch as a routine maintenance operation.

6. IMAGE SOURCING AND SALEOR MEDIA IMPORT
=========================================

6.1 Candidate discovery
-----------------------

Primary source:
  engine/image_sources.py

Lookup and provider configuration:
  engine/image_lookup.py

Active candidate waterfall:

  Tier 0: Curated media_index
  Tier 1: Icecat exact MPN
  Tier 2: Scrappa
  Tier 3: SerpAPI Google Images
  Tier 4: Exa manufacturer pages
  Tier 5: Optional Google CSE, disabled unless enabled

The candidate waterfall returns candidate records. It does not, by itself,
prove that an image is correct or attach it to Saleor.

6.2 Image identity
------------------

Image identity must be checked against the target product, not just a broad
search phrase or supplier category.

Preferred identity evidence:
  - Manufacturer.
  - Exact model or MPN.
  - Product family and model-specific tokens.
  - Supplier SKU where it can be reliably associated with the model.
  - Source page content.
  - Image dimensions and image content.

Reject:
  - Family-only images when the exact model cannot be established.
  - Images of a different model or revision.
  - Accessories when the target is the main product.
  - Packaging-only images when a product image is required.
  - Duplicate images where a distinct view is required.
  - Unverifiable or unsafe URLs.

Relevant modules:
  engine/image_identity.py
  engine/image_verification.py
  engine/image_provenance.py

6.3 Scrappa integration
-----------------------

Endpoint:
  GET https://scrappa.co/api/images

Query parameter:
  q

Authentication:
  X-API-KEY header

Configuration:
  SCRAPPA_API_KEY or SCRAPPA_KEY
  SCRAPPA_API_BASE_URL

Default base URL:
  https://scrappa.co/api

Configuration file:
  /opt/saleor-caag/imports/config/image_api.env

The implementation uses targeted model/MPN queries, public URL checks,
source-page identity evidence, image dimension checks, and ranking.

Verified status:
  - Scrappa source implementation is installed.
  - Mocked contract test passed.
  - Active source wiring was inspected.
  - A live Scrappa production request has not yet been verified.
  - The mocked test does not establish live provider availability or image
    quality.

Never print provider credential values.

6.4 From candidate to Saleor ProductMedia
-----------------------------------------

The complete image workflow should be:

  Saleor Product
      |
      v
  Resolve exact product identity
      |
      v
  Discover image candidates
      |
      v
  Verify manufacturer and model identity
      |
      v
  Validate source URL and source page
      |
      v
  Download candidate image
      |
      v
  Validate bytes, content type, and dimensions
      |
      v
  Normalize image if required
      |
      v
  Preserve provenance and verification result
      |
      v
  Create or update Saleor ProductMedia
      |
      v
  Verify media is attached to the intended Product
      |
      v
  Record success or a recoverable failure

Relevant attachment modules:
  engine/media_tasks.py
  engine/media.py

The media workflow can download images, create ProductMedia records, update
product metadata, remove placeholder media, and generate fallback or
service-card images.

process_product_media_sync is write-capable.

A sourcing-only test may make live provider requests but should not create
ProductMedia records. A media attachment test must use a small approved
product allowlist and explicit write approval.

7. REQUIRED END-TO-END IMPORTER COMPLETION
==========================================

The following is the target operational sequence for a seamless import.
It is not a claim that every step has already been production-validated.

PHASE 1 - SOURCE INGESTION
--------------------------

  - Identify supplier profile and feed schema.
  - Preserve original feed.
  - Calculate feed fingerprint.
  - Validate headers, types, required fields, and row counts.
  - Separate eligible, excluded, and invalid rows.
  - Produce a read-only report.

PHASE 2 - PRODUCT IDENTITY AND MAPPING
--------------------------------------

  - Resolve existing Saleor products and variants.
  - Use exact SKU or another explicitly approved identity rule.
  - Resolve product parent and variant relationships.
  - Detect duplicate SKU, ambiguous MPN, and conflicting parent mappings.
  - Resolve ProductType and ProductCategory.
  - Map product and variant attributes.
  - Produce a read-only mapping preview.

PHASE 3 - CATALOGUE WRITES
--------------------------

  - Create or update Product records.
  - Create or update ProductVariant records.
  - Preserve product-to-variant relationships.
  - Apply approved category and ProductType mappings.
  - Apply approved attributes and metadata.
  - Apply channel publication and listing rules.
  - Apply approved pricing and tax rules.
  - Apply inventory only under an explicit warehouse and stock policy.
  - Use bounded batches, transactions, and resumable checkpoints.

PHASE 4 - IMAGE SOURCING
------------------------

  - Build an image identity request from the resolved Saleor product.
  - Discover candidates through the configured waterfall.
  - Verify exact product identity.
  - Record candidate URLs, source, and confidence.
  - Produce a dry-run candidate report.
  - Reject ambiguous and unsafe candidates.

PHASE 5 - IMAGE ATTACHMENT
--------------------------

  - Download approved image candidates.
  - Validate image content and dimensions.
  - Normalize only when required.
  - Attach images to the correct Saleor Product.
  - Preserve provenance.
  - Verify ProductMedia records and file references.
  - Avoid duplicate or incorrect media attachments.

PHASE 6 - POST-IMPORT VERIFICATION
----------------------------------

  - Verify Product and ProductVariant counts.
  - Verify SKU uniqueness and parent relationships.
  - Verify ProductType and ProductCategory assignments.
  - Verify attributes and metadata.
  - Verify channel listings, publication, and prices.
  - Verify warehouse stock only where the import was authorized to write it.
  - Verify ProductMedia belongs to the intended Product.
  - Verify image provenance and duplicate handling.
  - Verify search/index state.
  - Write an execution manifest and row-level error report.

PHASE 7 - COMPLETION REPORT
---------------------------

Report each area separately:

  Feed validation:
  Identity resolution:
  Product writes:
  Variant writes:
  Category and attribute writes:
  Channel listing writes:
  Pricing writes:
  Inventory writes:
  Image discovery:
  Image verification:
  Image attachment:
  Post-import checks:

A workflow must not report "complete" merely because the feed was parsed
or supplier rows were reconciled.

8. SAFETY, IDEMPOTENCY, AND RECOVERY
===================================

Production requirements:

  - Preserve original supplier feeds.
  - Fingerprint feeds before and after long-running operations.
  - Use deterministic identity rules.
  - Never silently resolve identity conflicts.
  - Make operations idempotent where possible.
  - Use database transactions and bounded batches.
  - Use row-level error reporting.
  - Support safe resume after interruption.
  - Keep backups before source changes.
  - Maintain a rollback plan for database writes.
  - Do not run broad catalogue or media jobs as tests.
  - Require explicit approval for product, variant, pricing, inventory,
    channel listing, metadata, or media writes.

Image-specific requirements:

  - Exact product identity must be verified before attachment.
  - Preserve source provenance.
  - Do not expose API credentials in logs or public metadata.
  - Do not attach family-level or ambiguous matches.
  - Do not replace existing verified media without a defined policy.
  - Use an explicit product allowlist for initial production attachment.

API memory:
  - The API container previously experienced an OOM event.
  - Maintain at least 250 MiB memory headroom before memory-heavy checks.
  - Avoid concurrent memory-heavy Python processes.

9. SOURCE TREE AND RESPONSIBILITIES
===================================

/opt/saleor-caag/imports/
|
+-- incoming/                     Supplier feeds
+-- processed/                    Successfully processed feeds
+-- failed/                       Failed feeds and error records
+-- logs/                          Logs and execution reports
+-- cache/                         Local caches
+-- media_index/                   Curated SKU-to-image mappings
+-- config/image_api.env           Private image provider configuration
|
+-- importer/
    |
    +-- runner.py                  Legacy Saleor catalogue entry point
    +-- supplier_cli.py            Canonical supplier workflow CLI
    +-- IMPORTER_REFERENCE.md      This document
    |
    +-- config/
    |   +-- suppliers.py           Supplier profiles
    |   +-- taxonomy.py            Taxonomy configuration, if present
    |
    +-- engine/
        |
        +-- normalizers.py         Supplier data normalization
        +-- validators.py          Feed and row validation
        +-- orchestrator.py        Catalogue workflow orchestration
        +-- saleor_writer.py       Saleor catalogue writes
        +-- products.py             Product enrichment
        +-- variants.py             Variant helpers
        +-- pricing.py              Pricing rules and checks
        +-- post_import.py          Post-import processing
        +-- search_index.py         Search/index operations
        |
        +-- canonical/
        |   +-- identity.py         Canonical identity
        |   +-- reconciliation.py   Reconciliation decisions
        |   +-- supplier_import.py  Supplier batch processing
        |   +-- supplier_offers.py  Supplier offer persistence
        |
        +-- image_sources.py        Image candidate providers
        +-- image_lookup.py         Image lookup/configuration
        +-- image_identity.py       Product/image identity checks
        +-- image_provenance.py     Image source provenance
        +-- image_verification.py   Candidate verification
        +-- media_tasks.py          Media processing workflow
        +-- media.py                Saleor media operations
        +-- placeholder_image.py    Placeholder handling
        +-- product_cards.py        Product/service card generation

The exact deployed file list can evolve. Confirm file existence and current
imports before relying on an optional module or configuration file.

10. SALEOR BACKEND AND SCHEMA REFERENCES
========================================

Backend root:
  /opt/saleor-caag/backend

Django settings module:
  saleor.settings

The importer uses Django ORM integration for its Saleor database operations.
Django setup in the API container has been inspected using:

  DJANGO_SETTINGS_MODULE=saleor.settings

The deployed Saleor backend source is the authoritative reference for:
  - Product and ProductVariant model definitions.
  - ProductType and ProductCategory relationships.
  - Attribute and AttributeValue assignments.
  - ProductMedia and media storage configuration.
  - Channel and channel listing models.
  - Warehouse and Stock models.
  - Collection relationships.
  - Translation and metadata behaviour.
  - Unique constraints and transaction requirements.

Before implementing an ORM write, inspect the installed backend source and
migrations. Do not rely on model field names copied from another Saleor
version.

Saleor GraphQL/API considerations:
  - Product and variant mutations may have validation rules beyond direct
    ORM writes.
  - Channel publication and availability are distinct from product
    existence.
  - Variant pricing is channel-specific.
  - Inventory is warehouse-specific.
  - Media must be attached to the intended product.
  - Permissions and business rules may differ between API mutations and
    direct ORM operations.

Where the existing importer uses direct ORM writes, document and test the
resulting Saleor invariants explicitly. Do not assume that bypassing the
GraphQL API also bypasses Saleor's business requirements.

11. DEPLOYMENT AND SOURCE CHANGE CONTROL
========================================

The imports source is mounted read-only inside the API container.
Edit importer source on the host:

  /opt/saleor-caag/imports/

Do not attempt to edit the mounted source from inside the container.

For syntax checks, compile source without writing bytecode:

  python3 -B - <<'PY'
  from pathlib import Path
  p = Path("/opt/saleor-caag/imports/importer/engine/image_sources.py")
  compile(p.read_text(encoding="utf-8"), str(p), "exec")
  print("PASS", p)
  PY

Before changing production source:

  1. Inspect current source and Git status.
  2. Create a timestamped backup.
  3. Use exact source anchors and expected match counts.
  4. Make the smallest necessary change.
  5. Compile changed files.
  6. Run focused tests.
  7. Inspect the resulting diff.
  8. Preserve rollback copies.

Do not use git reset, git checkout, git clean, or broad recursive deletion
as a cleanup shortcut. Preserve unrelated uncommitted work.

Backups:
  /opt/saleor-caag/imports/importer/backups/

Historical archive:
  /opt/saleor-caag/imports/importer/archive/

Do not automatically delete backups or archive contents.

12. CURRENT VERIFIED STATUS
===========================

Completed and independently verified:
  - DickerData canonical staging and reconciliation.
  - 72,513 eligible supplier rows matched to existing Saleor variants.
  - Zero unmatched, conflict, invalid, or pending rows in that batch.
  - Supplier links, offers, prices, stock mapping, and history audit.
  - Scrappa source implementation.
  - Mocked Scrappa contract test.
  - Image-source waterfall inspection.
  - ASCII importer reference update.
  - Quarantine of six superseded operational scripts.

Not yet established as production-complete:
  - Full new-product creation from supplier feeds.
  - Full new-variant creation and relationship handling.
  - End-to-end category and attribute mapping for all supplier profiles.
  - Channel publication and listing validation for all import paths.
  - Production pricing and warehouse inventory policy for every supplier.
  - Live Scrappa API validation.
  - End-to-end image candidate quality validation.
  - Approved image download and ProductMedia attachment.
  - Full end-to-end product-plus-image import test.

The DickerData reconciliation result must not be represented as a completed
product import or image import.

13. NEXT IMPLEMENTATION PRIORITIES
==================================

The remaining work should proceed in this order:

  1. Audit the deployed Saleor model relationships and current writer.
  2. Map each importer output field to its exact Saleor destination.
  3. Identify missing Product, Variant, Category, Attribute, Channel,
     Pricing, and Inventory write behaviour.
  4. Define deterministic create/update and duplicate-handling rules.
  5. Add focused tests for each Saleor relationship.
  6. Run a read-only product import preview.
  7. Validate image discovery separately from media attachment.
  8. Run a bounded, approved image attachment test.
  9. Verify the resulting Saleor records independently.
 10. Expand only after the bounded end-to-end test passes.

The objective is one coherent, auditable workflow from supplier feed to
Saleor catalogue and verified product media, not disconnected scripts that
each report partial success.

END OF REFERENCE
