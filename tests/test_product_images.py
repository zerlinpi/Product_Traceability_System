"""主图 upload for product management.

The product profile column 主图 holds a picture rather than typed text, so the
system accepts an upload, validates it by file signature (never by the supplied
name or content type), stores it under a generated name and serves it back.
"""
from __future__ import annotations

import io
import struct
import zlib

import pytest

from capability_helpers import bootstrap_admin, insert_user, login, make_auth_app
from traceability.errors import ApiError
from traceability.validators import image_dimensions


def png_bytes(payload: bytes = b"pixel-data", width: int = 1, height: int = 1) -> bytes:
    """The PNG signature plus a real IHDR chunk declaring ``width``×``height``."""
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    chunk = struct.pack(">I", len(ihdr)) + b"IHDR" + ihdr
    return b"\x89PNG\r\n\x1a\n" + chunk + struct.pack(">I", zlib.crc32(b"IHDR" + ihdr)) + payload


def jpeg_bytes(width: int = 1, height: int = 1, sof_marker: int = 0xC0) -> bytes:
    """SOI, a JFIF APP0 segment, a DQT-like filler segment and a frame header."""
    app0 = b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    filler = b"\x00" * 65
    frame = struct.pack(">BHHB", 8, height, width, 1) + b"\x01\x11\x00"
    return (
        b"\xff\xd8"
        + b"\xff\xe0" + struct.pack(">H", len(app0) + 2) + app0
        # Fill bytes before a marker are legal and must be skipped.
        + b"\xff\xff\xdb" + struct.pack(">H", len(filler) + 2) + filler
        + bytes([0xFF, sof_marker]) + struct.pack(">H", len(frame) + 2) + frame
        + b"\xff\xd9"
    )


def gif_bytes(width: int = 1, height: int = 1) -> bytes:
    return b"GIF89a" + struct.pack("<HH", width, height)


def webp_bytes(width: int = 1, height: int = 1) -> bytes:
    """An extended (VP8X) WEBP header declaring the canvas size."""
    payload = b"\x00\x00\x00\x00" + (width - 1).to_bytes(3, "little") + (height - 1).to_bytes(3, "little")
    chunk = b"VP8X" + struct.pack("<I", len(payload)) + payload
    return b"RIFF" + struct.pack("<I", 4 + len(chunk)) + b"WEBP" + chunk


def webp_lossy_bytes(width: int, height: int) -> bytes:
    frame = b"\x10\x02\x00" + b"\x9d\x01\x2a" + struct.pack("<HH", width, height)
    chunk = b"VP8 " + struct.pack("<I", len(frame)) + frame
    return b"RIFF" + struct.pack("<I", 4 + len(chunk)) + b"WEBP" + chunk


def webp_lossless_bytes(width: int, height: int) -> bytes:
    bits = (width - 1) | ((height - 1) << 14)
    frame = b"\x2f" + struct.pack("<I", bits)
    chunk = b"VP8L" + struct.pack("<I", len(frame)) + frame
    return b"RIFF" + struct.pack("<I", 4 + len(chunk)) + b"WEBP" + chunk


def upload(client, csrf, data: bytes, filename: str = "main.png", field: str = "file"):
    return client.post(
        "/api/product-images",
        data={field: (io.BytesIO(data), filename)},
        content_type="multipart/form-data",
        headers={"X-CSRF-Token": csrf},
    )


def test_uploaded_image_is_stored_and_served_back(tmp_path):
    app, database_path, _fake = make_auth_app(tmp_path)
    admin, csrf = bootstrap_admin(app)
    content = png_bytes(zlib.compress(b"body"))

    response = upload(admin, csrf, content)
    assert response.status_code == 201, response.get_json()
    data = response.get_json()["data"]
    assert data["contentType"] == "image/png"
    assert data["size"] == len(content)
    assert data["url"] == f"/api/product-images/{data['filename']}"
    assert data["filename"].endswith(".png")

    # Stored next to the database, under the generated name only.
    stored = database_path.parent / "product-images" / data["filename"]
    assert stored.is_file()
    assert stored.read_bytes() == content

    served = admin.get(data["url"])
    assert served.status_code == 200
    assert served.mimetype == "image/png"
    assert served.get_data() == content


def test_supported_formats_are_detected_by_signature(tmp_path):
    app, _database_path, _fake = make_auth_app(tmp_path)
    admin, csrf = bootstrap_admin(app)
    for content, expected_type, expected_extension in (
        (png_bytes(), "image/png", ".png"),
        (jpeg_bytes(), "image/jpeg", ".jpg"),
        (gif_bytes(), "image/gif", ".gif"),
        (webp_bytes(), "image/webp", ".webp"),
    ):
        # The submitted filename is deliberately wrong: detection uses the bytes.
        response = upload(admin, csrf, content, filename="whatever.txt")
        assert response.status_code == 201, response.get_json()
        data = response.get_json()["data"]
        assert data["contentType"] == expected_type
        assert data["filename"].endswith(expected_extension)


def test_non_image_content_is_rejected_even_with_an_image_name(tmp_path):
    app, database_path, _fake = make_auth_app(tmp_path)
    admin, csrf = bootstrap_admin(app)
    response = upload(admin, csrf, b"<?php system($_GET['c']); ?>", filename="evil.png")
    assert response.status_code == 400
    assert "仅支持" in response.get_json()["message"]
    # Nothing was written.
    directory = database_path.parent / "product-images"
    assert not directory.exists() or not list(directory.iterdir())


def test_svg_is_rejected_whatever_it_is_called(tmp_path):
    """SVG can carry script, and an image URL opened directly would run it."""
    app, database_path, _fake = make_auth_app(tmp_path)
    admin, csrf = bootstrap_admin(app)
    svg = (
        b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg" width="1" height="1">'
        b"<script>alert(document.cookie)</script></svg>"
    )
    for filename in ("main.svg", "main.png"):
        response = upload(admin, csrf, svg, filename=filename)
        assert response.status_code == 400, filename
        assert "仅支持" in response.get_json()["message"]
    directory = database_path.parent / "product-images"
    assert not directory.exists() or not list(directory.iterdir())


def test_upload_reports_the_pixel_size_and_accepts_the_limits(tmp_path):
    app, _database_path, _fake = make_auth_app(tmp_path)
    admin, csrf = bootstrap_admin(app)
    for content in (
        png_bytes(width=10_000, height=4_000),  # longest side and 40 MP exactly
        jpeg_bytes(width=4_000, height=3_000),
        gif_bytes(width=640, height=480),
        webp_bytes(width=10_000, height=1),
    ):
        response = upload(admin, csrf, content)
        assert response.status_code == 201, response.get_json()
        data = response.get_json()["data"]
        assert (data["width"], data["height"]) == image_dimensions(content, data["filename"].rsplit(".", 1)[1])


def test_decompression_bombs_are_rejected_before_anything_is_stored(tmp_path):
    """A few-KB file declaring a gigantic canvas would exhaust browser memory."""
    app, database_path, _fake = make_auth_app(tmp_path)
    admin, csrf = bootstrap_admin(app)
    for content in (
        png_bytes(width=50_000, height=50_000),
        png_bytes(width=10_001, height=1),
        png_bytes(width=8_000, height=6_000),  # 48 MP: each side fits, total does not
        jpeg_bytes(width=12_000, height=100, sof_marker=0xC2),
        gif_bytes(width=65_535, height=65_535),
        webp_bytes(width=20_000, height=20_000),
    ):
        response = upload(admin, csrf, content)
        assert response.status_code == 400, response.get_json()
        message = response.get_json()["message"]
        assert "图片尺寸过大" in message
        assert "10000" in message
    directory = database_path.parent / "product-images"
    assert not directory.exists() or not list(directory.iterdir())


def test_images_whose_size_cannot_be_read_are_rejected(tmp_path):
    app, database_path, _fake = make_auth_app(tmp_path)
    admin, csrf = bootstrap_admin(app)
    for content in (
        b"\x89PNG\r\n\x1a\n" + b"pixel-data",  # no IHDR chunk
        png_bytes(width=0, height=10),
        b"\xff\xd8\xff\xe0 jpeg body",  # segment length runs past the data
        b"\xff\xd8\xff\xda" + b"\x00" * 16,  # scan data before any frame header
        b"GIF89a\x01",  # truncated logical screen descriptor
        b"RIFF\x00\x00\x00\x00WEBPVP8 ",  # no frame header
    ):
        response = upload(admin, csrf, content)
        assert response.status_code == 400, content
        assert "无法识别尺寸" in response.get_json()["message"]
    directory = database_path.parent / "product-images"
    assert not directory.exists() or not list(directory.iterdir())


@pytest.mark.parametrize(
    ("content", "extension", "expected"),
    [
        (png_bytes(width=123, height=45), "png", (123, 45)),
        (jpeg_bytes(width=800, height=600), "jpg", (800, 600)),
        (jpeg_bytes(width=321, height=654, sof_marker=0xC2), "jpg", (321, 654)),
        (gif_bytes(width=7, height=9), "gif", (7, 9)),
        (webp_bytes(width=1920, height=1080), "webp", (1920, 1080)),
        (webp_lossy_bytes(1024, 768), "webp", (1024, 768)),
        (webp_lossless_bytes(16_384, 3), "webp", (16_384, 3)),
    ],
)
def test_image_dimensions_are_read_from_each_header(content, extension, expected):
    assert image_dimensions(content, extension) == expected


def test_image_dimensions_refuse_unreadable_headers():
    with pytest.raises(ApiError):
        image_dimensions(b"\xff\xd8", "jpg")
    with pytest.raises(ApiError):
        image_dimensions(png_bytes(), "bmp")


def test_empty_and_missing_uploads_are_rejected(tmp_path):
    app, _database_path, _fake = make_auth_app(tmp_path)
    admin, csrf = bootstrap_admin(app)
    empty = upload(admin, csrf, b"")
    assert empty.status_code == 400
    assert "空" in empty.get_json()["message"]

    missing = admin.post(
        "/api/product-images",
        data={},
        content_type="multipart/form-data",
        headers={"X-CSRF-Token": csrf},
    )
    assert missing.status_code == 400
    assert "请选择要上传的图片" in missing.get_json()["message"]


def test_oversized_image_is_rejected(tmp_path):
    app, _database_path, _fake = make_auth_app(tmp_path)
    admin, csrf = bootstrap_admin(app)
    too_big = png_bytes(b"x" * (5 * 1024 * 1024))
    response = upload(admin, csrf, too_big)
    assert response.status_code == 400
    assert "5 MB" in response.get_json()["message"]


def test_image_requests_are_authenticated_and_reject_traversal(tmp_path):
    app, _database_path, _fake = make_auth_app(tmp_path)
    admin, csrf = bootstrap_admin(app)
    uploaded = upload(admin, csrf, png_bytes()).get_json()["data"]

    # Anonymous access is refused by the auth layer.
    anonymous = app.test_client()
    assert anonymous.get(uploaded["url"]).status_code == 401
    assert anonymous.post("/api/product-images").status_code == 401

    # Only generated names resolve; traversal and unknown names are 404.
    for candidate in (
        "/api/product-images/../traceability.db",
        "/api/product-images/..%2Ftraceability.db",
        "/api/product-images/traceability.db",
        "/api/product-images/20260101-0123456789abcdef0123.png",
    ):
        assert admin.get(candidate).status_code == 404, candidate


def test_operations_can_upload_and_save_the_picture_on_their_product(tmp_path):
    app, database_path, _fake = make_auth_app(tmp_path)
    bootstrap_admin(app)
    insert_user(database_path, "ops.alice", "OPERATIONS")
    alice, alice_csrf = login(app, "ops.alice")

    uploaded = upload(alice, alice_csrf, png_bytes()).get_json()["data"]
    created = alice.post(
        "/api/products",
        json={"name": "带主图的产品", "attributes": {"主图": uploaded["url"]}},
        headers={"X-CSRF-Token": alice_csrf},
    )
    assert created.status_code == 201, created.get_json()
    product = created.get_json()["data"]
    assert product["attributes"]["主图"] == uploaded["url"]

    # The picture survives a later partial edit and stays reachable.
    updated = alice.put(
        f"/api/products/{product['id']}",
        json={"attributes": {"品牌": "聚星"}},
        headers={"X-CSRF-Token": alice_csrf},
    )
    assert updated.get_json()["data"]["attributes"]["主图"] == uploaded["url"]
    assert alice.get(uploaded["url"]).status_code == 200


def test_attribute_metadata_marks_the_image_column(tmp_path):
    app, _database_path, _fake = make_auth_app(tmp_path)
    admin, _csrf = bootstrap_admin(app)
    meta = admin.get("/api/product-attribute-columns").get_json()["data"]
    assert meta["imageColumns"] == ["主图"]
    # The image column is part of the template and is operator-editable.
    assert "主图" in meta["columns"]
    assert "主图" not in meta["derived"]
