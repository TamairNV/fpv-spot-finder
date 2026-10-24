"""Flight photos: re-encoded as JPEG with the EXIF dropped (phone photos carry GPS), plus a thumbnail."""
import io
import secrets

from PIL import Image, ImageOps

try:  # iPhones can send HEIC
    from pillow_heif import register_heif_opener
    register_heif_opener()
except ImportError:  # pragma: no cover
    pass

FULL = 2048
THUMB = 480


def save(raw: bytes, folder) -> dict:
    img = Image.open(io.BytesIO(raw))
    img = ImageOps.exif_transpose(img).convert("RGB")
    pid = secrets.token_urlsafe(16)
    folder.mkdir(parents=True, exist_ok=True)
    full = img.copy()
    full.thumbnail((FULL, FULL), Image.LANCZOS)
    full.save(folder / f"{pid}.jpg", "JPEG", quality=85, optimize=True, progressive=True)
    thumb = img.copy()
    thumb.thumbnail((THUMB, THUMB), Image.LANCZOS)
    thumb.save(folder / f"{pid}_t.jpg", "JPEG", quality=80, optimize=True)
    return {"id": pid, "w": full.width, "h": full.height}


def delete(pid: str, folder) -> None:
    for name in (f"{pid}.jpg", f"{pid}_t.jpg"):
        (folder / name).unlink(missing_ok=True)
