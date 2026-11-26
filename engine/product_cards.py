"""Two-tier catalogue cards.

Tier A (physical, no verified source): 'IMAGE PENDING' card. Retry-eligible.
Tier B (digital/services): permanent branded licence/service card. NOT retry-eligible
        — a software subscription has no product photograph.
"""
import glob
import hashlib
import io
from PIL import Image, ImageDraw, ImageFont

W, H = 1200, 900
BG      = (255, 255, 255)
PANEL   = (247, 249, 252)
INK     = (17, 24, 39)
MUTED   = (107, 114, 128)
ACCENT  = (37, 99, 235)
AMBER   = (180, 83, 9)
LINE    = (226, 232, 240)

_FONT_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/local/share/fonts/DejaVuSans.ttf",
]
_CACHE = {}


def _font(size, bold=False):
    ck = (size, bold)
    if ck in _CACHE:
        return _CACHE[ck]
    cands = [p for p in _FONT_PATHS if ("Bold" in p) == bold] or _FONT_PATHS
    cands += sorted(glob.glob("/usr/share/fonts/**/*.ttf", recursive=True))
    for p in cands:
        try:
            f = ImageFont.truetype(p, size)
            _CACHE[ck] = f
            return f
        except Exception:
            continue
    try:
        f = ImageFont.load_default(size=size)
    except TypeError:
        f = ImageFont.load_default()
    _CACHE[ck] = f
    return f


def _wrap(draw, text, font, max_w, max_lines):
    words, lines, cur = (text or "").split(), [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if draw.textlength(trial, font=font) <= max_w:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = w
            if len(lines) == max_lines:
                break
    if cur and len(lines) < max_lines:
        lines.append(cur)
    if len(lines) == max_lines and words:
        joined = " ".join(lines)
        if len(joined) < len(" ".join(words)):
            lines[-1] = lines[-1][:max(0, len(lines[-1]) - 1)] + "…"
    return lines or [""]


KIND_LABEL = {
    "licence":  "SOFTWARE LICENCE",
    "warranty": "WARRANTY & SUPPORT",
    "service":  "PROFESSIONAL SERVICE",
    "pending":  None,
}


def generate_card(brand, name, sku, kind="pending"):
    """Return (png_bytes, 'png', sha256). Deterministic, no network."""
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, 12], fill=ACCENT)
    d.rectangle([60, 150, W - 60, H - 150], fill=PANEL, outline=LINE, width=2)

    d.text((80, 60), (brand or "CAA GLOBAL").upper()[:34],
           font=_font(46, True), fill=ACCENT)

    label = KIND_LABEL.get(kind)
    if label:
        d.text((W - 80, 72), label, font=_font(26, True),
               fill=MUTED, anchor="ra")
    else:
        d.rectangle([W - 330, 58, W - 80, 104], outline=AMBER, width=3)
        d.text((W - 205, 81), "IMAGE PENDING", font=_font(24, True),
               fill=AMBER, anchor="mm")

    nf = _font(52, True)
    lines = _wrap(d, name or "", nf, W - 220, 4)
    y = 300
    for ln in lines:
        d.text((W // 2, y), ln, font=nf, fill=INK, anchor="mm")
        y += 68

    d.line([180, H - 250, W - 180, H - 250], fill=LINE, width=2)
    d.text((W // 2, H - 205), f"SKU  {sku}", font=_font(30), fill=MUTED, anchor="mm")
    d.text((W // 2, H - 100), "CAA Global · Genuine Australian Stock",
           font=_font(26), fill=MUTED, anchor="mm")

    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    data = buf.getvalue()
    return data, "png", hashlib.sha256(data).hexdigest()
