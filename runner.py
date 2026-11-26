#!/usr/bin/env python3
import os
import sys

# Make paths relative to the script's location to work both on host and inside Docker
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
# imports/importer/runner.py -> imports/importer -> imports -> PROJECT_ROOT
PROJECT_ROOT = os.environ.get('IMPORTER_PROJECT_ROOT', os.path.abspath(os.path.join(SCRIPT_DIR, '../../..')))
BACKEND_ROOT = os.environ.get('IMPORTER_BACKEND_ROOT', os.path.join(PROJECT_ROOT, 'backend'))

if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
if BACKEND_ROOT not in sys.path:
    sys.path.insert(0, BACKEND_ROOT)

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'saleor.settings')

import argparse
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def main():
    parser = argparse.ArgumentParser(description="Saleor Multi-Supplier Catalogue Importer")
    parser.add_argument('--file', required=True, help='Path to the CSV import file')
    parser.add_argument('--channel', default='default-channel', help='Saleor channel slug')
    parser.add_argument('--warehouse', default='default-warehouse', help='Saleor warehouse slug')
    parser.add_argument('--product-type', default='default-type', help='Saleor product type slug')
    parser.add_argument(
        '--confirm-write',
        action='store_true',
        help='Explicitly authorise database writes for this catalogue import',
    )

    args = parser.parse_args()

    if not args.confirm_write:
        parser.error(
            "This importer writes to Saleor. Re-run with --confirm-write "
            "only after reviewing the input file and target channel, warehouse "
            "and product type."
        )

    # Do not initialise Django or import database models until the explicit
    # write-confirmation guard has passed.
    import django
    django.setup()

    from saleor.product.models import ProductType
    from saleor.channel.models import Channel
    from saleor.warehouse.models import Warehouse
    from imports.importer.engine.orchestrator import run_import

    try:
        channel = Channel.objects.filter(slug=args.channel).first()
        if not channel:
            logger.error(f"Channel '{args.channel}' not found.")
            sys.exit(1)

        warehouse = Warehouse.objects.filter(slug=args.warehouse).first()
        if not warehouse:
            logger.error(f"Warehouse '{args.warehouse}' not found.")
            sys.exit(1)

        product_type = ProductType.objects.filter(slug=args.product_type).first()
        if not product_type:
            logger.info(f"ProductType '{args.product_type}' not found. Creating default.")
            product_type = ProductType.objects.create(
                name="Default Imported Type",
                slug=args.product_type,
                has_variants=True,
                is_shipping_required=True
            )

        logger.info(f"Starting import: {args.file}")
        logger.info(f"Target -> Channel: {channel.slug}, Warehouse: {warehouse.slug}, Type: {product_type.slug}")

        stats = run_import(
            csv_path=args.file,
            default_channel_id=channel.id,
            default_warehouse_id=str(warehouse.id),
            default_product_type_id=product_type.id
        )

        logger.info("Import finished successfully.")
        logger.info(f"Final Stats: {stats}")

    except Exception as e:
        logger.exception(f"Import failed with error: {e}")
        sys.exit(1)

if __name__ == '__main__':
    main()
