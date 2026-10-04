"""The bundle is served compressed, and the compressed copy is the right one.

The front end is about 2.4 MB of JavaScript and CSS, sent by the application
itself — on Windows the deployment is Waitress with no reverse proxy, so nothing
else can compress it. gzip takes it to about 0.7 MB.

The failure mode this guards against is quiet. A response carrying
``Content-Encoding: gzip`` whose body is not gzip decodes to garbage; the browser
reports a syntax error in a file whose text it can see is fine on disk. So the
tests below decompress what the server sent and compare it to the file, rather
than asserting the header is present.
"""

from __future__ import annotations

import gzip
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

from app import create_app  # noqa: E402
from traceability.responses import (  # noqa: E402
    COMPRESSIBLE_SUFFIXES,
    HASHED_ASSET_PREFIX,
    precompressed_path,
)


@pytest.fixture()
def app(tmp_path):
    return create_app({"TESTING": True, "DATABASE": str(tmp_path / "t.db")})


def _biggest_asset(client) -> tuple[str, bytes]:
    """The largest asset the entry page actually references."""
    html = client.get("/").get_data(as_text=True)
    referenced = re.findall(
        r'(?:src|href)="(/static/dist/assets/[^"]+\.(?:js|css))"', html
    )
    assert referenced, "入口页没有引用任何资源，测试前提失效"
    target = max(referenced, key=lambda url: (ROOT / url.lstrip("/")).stat().st_size)
    return target, (ROOT / target.lstrip("/")).read_bytes()


def test_a_hashed_asset_is_served_compressed_and_decodes_to_the_file(app):
    client = app.test_client()
    url, original = _biggest_asset(client)

    response = client.get(url, headers={"Accept-Encoding": "gzip"})

    assert response.status_code == 200
    assert response.headers.get("Content-Encoding") == "gzip"
    body = response.get_data()
    assert len(body) < len(original), "压缩后反而更大，说明发错了文件"
    assert gzip.decompress(body) == original, "解压结果与磁盘上的文件不一致"


def test_the_compressed_response_declares_vary(app):
    """Without Vary a shared cache could hand gzip to a client that did not ask."""
    client = app.test_client()
    url, _ = _biggest_asset(client)

    response = client.get(url, headers={"Accept-Encoding": "gzip"})

    assert "Accept-Encoding" in (response.headers.get("Vary") or "")


def test_a_client_that_cannot_take_gzip_gets_the_original(app):
    client = app.test_client()
    url, original = _biggest_asset(client)

    response = client.get(url, headers={"Accept-Encoding": "identity"})

    assert response.headers.get("Content-Encoding") is None
    assert response.get_data() == original


def test_the_entry_page_is_not_compressed(app):
    """It is small, and its ETag is the mechanism that picks up a deployment."""
    client = app.test_client()

    response = client.get("/", headers={"Accept-Encoding": "gzip"})

    assert response.headers.get("Content-Encoding") is None


def test_api_responses_are_not_touched(app):
    client = app.test_client()

    response = client.get("/api/health", headers={"Accept-Encoding": "gzip"})

    assert response.headers.get("Content-Encoding") is None


def test_a_stale_compressed_copy_is_not_served(tmp_path):
    """A .gz older than its asset is from a previous build.

    Serving it would be undetectable: the header says gzip, the bytes are valid
    gzip, and they decode to the previous build's file.
    """
    asset = tmp_path / "app.js"
    asset.write_bytes(b"console.log('current');\n" * 50)
    stale = tmp_path / "app.js.gz"
    stale.write_bytes(gzip.compress(b"console.log('previous');\n" * 50))
    older = asset.stat().st_mtime - 60
    import os

    os.utime(stale, (older, older))

    assert precompressed_path(asset) is None


def test_a_missing_compressed_copy_is_not_an_error(tmp_path):
    asset = tmp_path / "app.js"
    asset.write_bytes(b"x" * 100)

    assert precompressed_path(asset) is None


def test_suffixes_that_are_not_worth_compressing_are_skipped(tmp_path):
    for suffix in (".png", ".woff2", ".ico"):
        asset = tmp_path / f"image{suffix}"
        asset.write_bytes(b"binary" * 100)
        asset.with_name(asset.name + ".gz").write_bytes(gzip.compress(b"binary" * 100))
        assert precompressed_path(asset) is None, suffix


def test_every_compressible_asset_in_the_bundle_has_a_compressed_copy():
    """Catches a build that skipped the compression step.

    Without this, a bundle whose .gz files were never generated still serves
    correctly — just uncompressed — and the regression would be invisible until
    someone measured the transfer size.
    """
    bundle = ROOT / "static" / "dist"
    assert bundle.is_dir(), "没有已提交的产物，测试前提失效"

    missing = []
    for asset in sorted(bundle.rglob("*")):
        if not asset.is_file() or asset.suffix.lower() not in COMPRESSIBLE_SUFFIXES:
            continue
        # Tiny files are deliberately skipped: a compressed copy would be larger.
        if len(asset.read_bytes()) < 1024:
            continue
        if not asset.with_name(asset.name + ".gz").is_file():
            missing.append(asset.relative_to(ROOT).as_posix())

    assert not missing, "以下资源缺少 .gz（是否忘了运行 tools/build_frontend.py？）: " + ", ".join(
        missing[:10]
    )


def test_the_hashed_prefix_is_the_one_the_cache_header_uses():
    """The compression scope and the immutable-cache scope must be the same path.

    Compressing outside it would require a variant-aware ETag, which is why the
    scope is narrow in the first place.
    """
    assert HASHED_ASSET_PREFIX == "/static/dist/assets/"
