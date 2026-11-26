from PIL import Image, ImageDraw, ImageFont
import io, hashlib

def generate_placeholder(brand: str, sku: str, mpn: str = "") -> tuple:
    width, height = 800, 600
    img = Image.new('RGB', (width, height), color=(245, 245, 245))
    draw = ImageDraw.Draw(img)
    try:
        font_large = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 40)
        font_small = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 24)
    except IOError:
        font_large = ImageFont.load_default()
        font_small = ImageFont.load_default()

    draw.text((width//2, height//2 - 40), "IMAGE PENDING", fill=(100, 100, 100), font=font_large, anchor="mm")
    draw.text((width//2, height//2 + 10), f"SKU: {sku}", fill=(150, 150, 150), font=font_small, anchor="mm")
    if brand:
        draw.text((width//2, height//2 + 40), f"Brand: {brand}", fill=(150, 150, 150), font=font_small, anchor="mm")

    img_bytes = io.BytesIO()
    img.save(img_bytes, format="PNG")
    img_bytes.seek(0)
    return img_bytes.getvalue(), "png", hashlib.sha256(img_bytes.getvalue()).hexdigest()
