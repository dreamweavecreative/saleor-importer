import requests, hashlib
from PIL import Image
import io

def verify_image(url: str, expected_brand: str = "", expected_mpn: str = "") -> tuple:
    try:
        head = requests.head(url, timeout=5, allow_redirects=True)
        if head.status_code != 200 or not head.headers.get('Content-Type', '').startswith('image/'):
            return False, "", b"", ""
        resp = requests.get(url, timeout=10, stream=True)
        resp.raise_for_status()
        chunks, total_size = [], 0
        for chunk in resp.iter_content(chunk_size=8192):
            total_size += len(chunk)
            if total_size > 10 * 1024 * 1024: return False, "", b"", ""
            chunks.append(chunk)
        image_data = b"".join(chunks)
        sha256 = hashlib.sha256(image_data).hexdigest()
        try:
            img = Image.open(io.BytesIO(image_data))
            img.verify()
            img = Image.open(io.BytesIO(image_data))
            if img.width < 200 or img.height < 200: return False, "", b"", ""
        except Exception:
            return False, "", b"", ""
        return True, "verified", image_data, sha256
    except Exception:
        return False, "", b"", ""
