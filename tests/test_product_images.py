"""主图 upload for product management.

The product profile column 主图 holds a picture rather than typed text, so the
system accepts an upload, validates it by file signature (never by the supplied
name or content type), stores it under a generated name and serves it back.
"""
from __future__ import annotations

import io
import zlib

from capability_helpers import bootstrap_admin, insert_user, login, make_auth_app


def png_bytes(payload: bytes = b"pixel-data") -> bytes:
    """A byte string starting with the real PNG signature."""
    return b"\x89PNG\r\n\x1a\n" + payload


def gif_bytes() -> bytes:
    return b"GIF89a" + b"\x01\x00\x01\x00"


def webp_bytes() -> bytes:
    return b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"VP8 "


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
        (b"\xff\xd8\xff\xe0 jpeg body", "image/jpeg", ".jpg"),
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
