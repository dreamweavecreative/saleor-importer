import os, django
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "saleor.settings")
django.setup()

from saleor.channel.models import Channel
from saleor.menu.models import Menu, MenuItem
from saleor.product.models import Category, Product

ch = Channel.objects.filter(is_active=True).first()
menu = Menu.objects.filter(slug="navbar").first()
print(f"channel={ch.slug} menu={menu.slug if menu else None}")
for r in Category.objects.filter(parent__isnull=True).order_by("name"):
    subtree = [r.id] + [c.id for c in r.get_descendants()]
    pub = Product.objects.filter(category_id__in=subtree,
                                 channel_listings__channel=ch,
                                 channel_listings__is_published=True).count()
    in_menu = MenuItem.objects.filter(menu=menu, category=r).exists() if menu else False
    print(f"{r.name:<28} direct={r.products.count():<7} published_subtree={pub:<7} navbar={in_menu}")

# menu inventory — which menu feeds the header vs the dropdown
for m in Menu.objects.all():
    items = list(MenuItem.objects.filter(menu=m, category__isnull=False)
                 .values_list("category__name", flat=True))
    print(f"MENU {m.slug}: {len(items)} category items -> {items}")
