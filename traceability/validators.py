"""Input validation and normalisation.

These are the functions that turn whatever a client sent into a value the
database can hold, or refuse it with a message the operator can act on. They
were the first block of ``app.py``; they live here now so route modules can use
them without importing the application.

Every function is pure: standard library plus :class:`~traceability.errors.ApiError`.
No Flask, no database, no application configuration — that is what makes them
testable and safe to share.

Import them from here; ``app.py`` only uses ``now_iso`` (the default clock) and
no longer re-exports the rest.
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


# Leading characters that make Excel / WPS / LibreOffice read a cell as a
# formula (CSV / formula injection, CWE-1236). Tab and CR are included because
# some spreadsheet programs strip them before parsing the rest of the cell.
SPREADSHEET_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def looks_like_spreadsheet_formula(value: object) -> bool:
    """Whether a text value would be evaluated if typed into a spreadsheet cell."""
    return isinstance(value, str) and value.startswith(SPREADSHEET_FORMULA_PREFIXES)


def csv_safe_row(values: list[object]) -> list[object]:
    """Neutralise text that a spreadsheet would run as a formula when opening a CSV.

    Names, supplier serials and other typed text end up in the QR-code archive
    manifests, which are usually opened in Excel. Formula-like text gets the
    leading apostrophe OWASP recommends, so ``=HYPERLINK(...)`` in a supplier
    name is shown as text instead of executing. Numbers pass through untouched.
    """
    return [f"'{value}" if looks_like_spreadsheet_formula(value) else value for value in values]


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


# JPEG start-of-frame markers (baseline, progressive, lossless, arithmetic).
# 0xC4 (DHT), 0xC8 (JPG) and 0xCC (DAC) share the range but carry no size.
_JPEG_SOF_MARKERS = frozenset(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}
# Markers without a length field: TEM and RST0-7 (SOI / EOI never appear mid-scan).
_JPEG_STANDALONE_MARKERS = frozenset({0x01, *range(0xD0, 0xD8)})


def _jpeg_dimensions(data: bytes) -> tuple[int, int] | None:
    """Walk the JPEG segments up to the first start-of-frame header."""
    position = 2  # after SOI
    length = len(data)
    while position < length:
        if data[position] != 0xFF:
            return None
        # Any number of 0xFF fill bytes may precede the marker.
        while position < length and data[position] == 0xFF:
            position += 1
        if position >= length:
            return None
        marker = data[position]
        position += 1
        if marker in _JPEG_STANDALONE_MARKERS:
            continue
        if marker in (0xD8, 0xD9, 0xDA):
            # SOI again, EOI or start-of-scan before any frame header.
            return None
        if position + 2 > length:
            return None
        segment_length = int.from_bytes(data[position:position + 2], "big")
        if segment_length < 2:
            return None
        if marker in _JPEG_SOF_MARKERS:
            if position + 7 > length:
                return None
            height = int.from_bytes(data[position + 3:position + 5], "big")
            width = int.from_bytes(data[position + 5:position + 7], "big")
            return width, height
        position += segment_length
    return None


def _webp_dimensions(data: bytes) -> tuple[int, int] | None:
    chunk = data[12:16]
    if chunk == b"VP8X" and len(data) >= 30:
        # Extended format: 24-bit canvas width-1 / height-1.
        width = int.from_bytes(data[24:27], "little") + 1
        height = int.from_bytes(data[27:30], "little") + 1
        return width, height
    if chunk == b"VP8 " and len(data) >= 30 and data[23:26] == b"\x9d\x01\x2a":
        # Lossy: 14-bit sizes after the key-frame start code.
        width = int.from_bytes(data[26:28], "little") & 0x3FFF
        height = int.from_bytes(data[28:30], "little") & 0x3FFF
        return width, height
    if chunk == b"VP8L" and len(data) >= 25 and data[20] == 0x2F:
        # Lossless: 14-bit width-1 and height-1 packed after the signature byte.
        bits = int.from_bytes(data[21:25], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    return None


def image_dimensions(data: bytes, extension: str) -> tuple[int, int]:
    """Return ``(width, height)`` in pixels from the image header.

    Only the header is read, never the pixels, so a small file that would
    decode into an enormous bitmap (a decompression bomb) is caught before it
    is stored and later rendered in every operator's browser. A header this
    parser cannot read is refused: a picture no browser could size is not a
    picture worth storing, and failing closed keeps the size check honest.
    """
    dimensions: tuple[int, int] | None = None
    if extension == "png" and len(data) >= 24 and data[12:16] == b"IHDR":
        dimensions = (
            int.from_bytes(data[16:20], "big"),
            int.from_bytes(data[20:24], "big"),
        )
    elif extension == "gif" and len(data) >= 10:
        dimensions = (
            int.from_bytes(data[6:8], "little"),
            int.from_bytes(data[8:10], "little"),
        )
    elif extension == "jpg":
        dimensions = _jpeg_dimensions(data)
    elif extension == "webp":
        dimensions = _webp_dimensions(data)
    if not dimensions or dimensions[0] <= 0 or dimensions[1] <= 0:
        raise ApiError("图片文件已损坏或不完整，无法识别尺寸")
    return dimensions


def business_id(value: object, label: str) -> int:
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ApiError(f"请选择{label}")
    if isinstance(value, bool):
        raise ApiError(f"{label}无效")
    try:
        result = int(value)
    except (TypeError, ValueError) as error:
        raise ApiError(f"{label}无效") from error
    if result <= 0:
        raise ApiError(f"{label}无效")
    return result


def business_quantity(value: object, label: str) -> int:
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ApiError(f"{label}必须是 1-999999 之间的整数")
    if isinstance(value, bool) or (
        isinstance(value, float) and not value.is_integer()
    ):
        raise ApiError(f"{label}必须是 1-999999 之间的整数")
    try:
        quantity = int(value)
    except (TypeError, ValueError) as error:
        raise ApiError(f"{label}必须是 1-999999 之间的整数") from error
    if not 1 <= quantity <= 999999:
        raise ApiError(f"{label}必须是 1-999999 之间的整数")
    return quantity
