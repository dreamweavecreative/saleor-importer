import os, sys, io, argparse
from pathlib import Path
os.environ.setdefault("DJANGO_SETTINGS_MODULE","saleor.settings")
sys.path.insert(0,"/app"); sys.path.insert(0,"/opt/saleor-caag/imports")
import django; django.setup()
from django.db import transaction
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from saleor.product.models import Product, ProductMedia

ap = argparse.ArgumentParser(); ap.add_argument("--apply", action="store_true")
a = ap.parse_args()
REL = "products/caag-placeholder.png"

def make_png():
    from PIL import Image, ImageDraw, ImageFont
    im = Image.new("RGB", (1000,1000), (245,246,248))
    d = ImageDraw.Draw(im)
    d.rectangle([250,330,750,610], outline=(190,196,204), width=6)   # screen
    d.rectangle([210,620,790,650], outline=(190,196,204), width=6)   # base
    font = None
    for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
              "/usr/share/fonts/dejavu/DejaVuSans.ttf"):
        if Path(p).exists():
            font = ImageFont.truetype(p, 34); break
    if font is None: font = ImageFont.load_default()
    txt = "Product image coming soon"
    box = d.textbbox((0,0), txt, font=font)
    d.text(((1000-(box[2]-box[0]))/2, 720), txt, fill=(130,138,148), font=font)
    buf = io.BytesIO(); im.save(buf, format="PNG", optimize=True)
    return buf.getvalue()

targets = Product.objects.filter(media__isnull=True).distinct()
print("products without media:", targets.count())
print("placeholder exists:", default_storage.exists(REL))
if not a.apply:
    print("dry-run; re-run with --apply"); raise SystemExit(0)

if not default_storage.exists(REL):
    default_storage.save(REL, ContentFile(make_png()))
    print("created placeholder:", REL)

created = 0
with transaction.atomic():
    for p in targets.iterator():
        if p.media.exists(): continue
        m = ProductMedia(product=p, alt=(p.name or "")[:250], sort_order=0)
        m.image = REL                      # reuse one file, no per-product upload
        m.metadata = {"placeholder": True}
        m.save()
        created += 1
print("media rows created:", created)
print("products still without media:", Product.objects.filter(media__isnull=True).distinct().count())
