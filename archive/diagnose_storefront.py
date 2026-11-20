import os, sys
sys.path.insert(0, "/app"); sys.path.insert(0, "/tmp")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "saleor.settings")
import django; django.setup()
from django.db.models import Count, Q, F
from saleor.channel.models import Channel
from saleor.product.models import (Product, ProductVariant, Category, Collection,
    ProductChannelListing, ProductVariantChannelListing, CollectionProduct)
from saleor.warehouse.models import Stock
from saleor.menu.models import Menu, MenuItem

CH = Channel.objects.filter(is_active=True).first()
def sec(t): print("\n" + "="*80 + "\n" + t + "\n" + "="*80)

sec("1. CATEGORY VISIBILITY (what the storefront can show)")
roots = Category.objects.filter(parent__isnull=True).annotate(
    n=Count("products")).order_by("-n")
for r in roots:
    pub = Product.objects.filter(category__in=Category.objects.filter(
        Q(id=r.id) | Q(parent=r) | Q(parent__parent=r)),
        channel_listings__channel=CH, channel_listings__is_published=True).distinct().count()
    print("  root %-26s products=%-6d published_in_channel=%d" % (r.name, r.n, pub))
print("  menu items:", MenuItem.objects.count(),
      "| menus:", list(Menu.objects.values_list("slug", flat=True)))
for mi in MenuItem.objects.filter(category__isnull=False).select_related("category")[:20]:
    print("    menu %-22s -> category %s" % (mi.name, mi.category.name))

sec("2. PUBLICATION / 404 CANDIDATES")
pl = ProductChannelListing.objects.filter(channel=CH)
print("  listings:", pl.count())
print("  is_published=False      :", pl.filter(is_published=False).count())
print("  visible_in_listings=False:", pl.filter(visible_in_listings=False).count())
print("  available_for_purchase_at in future:",
      pl.filter(available_for_purchase_at__gt=pl.model.objects.none().count and __import__('django.utils.timezone', fromlist=['now']).now()).count())
print("  products with NO listing at all:",
      Product.objects.exclude(id__in=pl.values_list("product_id", flat=True)).count())

sec("3. STRUCK-THROUGH VARIANT CAUSE (unavailable at checkout)")
vl = ProductVariantChannelListing.objects.filter(channel=CH)
v = ProductVariant.objects.all()
no_stock = v.filter(track_inventory=True).exclude(
    id__in=Stock.objects.filter(quantity__gt=0).values_list("product_variant_id", flat=True))
print("  variants track_inventory=True with ZERO stock:", no_stock.count())
print("  variant listings is_available_for_purchase=False:", vl.filter(is_available_for_purchase=False).count())
print("  variants track_inventory=False (always purchasable):", v.filter(track_inventory=False).count())
dup = v.values("product_id", "name").annotate(n=Count("id")).filter(n__gt=1)
print("  products with duplicate variant display names:", dup.count())
for d in dup[:5]:
    print("    product %s name=%r x%d" % (d["product_id"], d["name"][:60], d["n"]))

sec("4. PRICE / SENTINEL RESIDUE")
print("  stock rows == 999999999 :", Stock.objects.filter(quantity=999999999).count())
print("  stock rows == 0         :", Stock.objects.filter(quantity=0).count())
print("  variant price == 0      :", vl.filter(price_amount=0).count())
print("  cost > price            :", vl.filter(cost_price_amount__gt=F("price_amount")).count())

sec("5. SEARCH / DISCOUNTED DENORMALISATION")
print("  search_vector populated :", Product.objects.exclude(search_vector=None).count())
print("  search_index_dirty      :", Product.objects.filter(search_index_dirty=True).count())
print("  product discounted NULL :", pl.filter(discounted_price_amount__isnull=True).count())
