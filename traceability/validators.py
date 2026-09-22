"""Input validation and normalisation.

These are the functions that turn whatever a client sent into a value the
database can hold, or refuse it with a message the operator can act on. They
were the first block of ``app.py``; they live here now so route modules can use
them without importing the application.

Every function is pure: standard library plus :class:`~traceability.errors.ApiError`.
No Flask, no database, no application configuration — that is what makes them
testable and safe to share.

``app.py`` re-exports all of them, so existing imports keep working.
"""

from __future__ import annotations

import re
from datetime import datetime

from traceability.errors import ApiError

# Magic-byte signatures for the image types the product form accepts.
PRODUCT_IMAGE_SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", "png", "image/png"),
    (b"\xff\xd8\xff", "jpg", "image/jpeg"),
    (b"GIF87a", "gif", "image/gif"),
    (b"GIF89a", "gif", "image/gif"),
)


def now_iso() -> str:
    """The current local time in the ISO 8601 form this system stores."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def clean_text(value: object, label: str, *, required: bool = False, max_length: int = 100) -> str:
    text = str(value or "").strip()
    if required and not text:
        raise ApiError(f"请输入{label}")
    if len(text) > max_length:
        raise ApiError(f"{label}不能超过 {max_length} 个字符")
    return text


def parse_bool(value: object, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def coerce_export_number(value: object) -> int | float | None:
    """Convert a numeric-looking string into int/float for numeric export cells.

    Returns ``None`` when the value is not a plain number so callers can keep
    the original text.
    """
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        if re.fullmatch(r"-?\d+", text):
            return int(text)
        return float(text)
    except ValueError:
        return None


def safe_archive_name(value: object, fallback: str = "item") -> str:
    """A filename that is safe on both Windows and Linux.

    Windows forbids ``<>:"/\\|?*`` and control characters; a name ending in a dot
    or a space is also unusable there. Both platforms are supported, so the
    strictest rule wins.
    """
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", str(value or "").strip())
    name = name.strip(". ")
    return name[:100] or fallback


def clean_ble_uuid(value: object, label: str, *, required: bool = False) -> str:
    """Accept either a 16-bit short UUID (``fdab``) or a full 128-bit one."""
    uuid = clean_text(value, label, required=required, max_length=36).lower()
    if uuid and not re.fullmatch(
        r"(?:[0-9a-f]{4}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
        uuid,
    ):
        raise ApiError(f"{label}格式无效")
    return uuid


def detect_product_image_type(data: bytes) -> tuple[str, str]:
    """Return ``(extension, mimetype)`` for supported image bytes.

    Detection is by file signature, so the stored file really is a picture
    regardless of the uploaded filename or the declared content type.
    """
    for signature, extension, mimetype in PRODUCT_IMAGE_SIGNATURES:
        if data.startswith(signature):
            return extension, mimetype
    # WEBP: "RIFF" .... "WEBP"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp", "image/webp"
    raise ApiError("仅支持 PNG、JPG、GIF 或 WEBP 图片")
