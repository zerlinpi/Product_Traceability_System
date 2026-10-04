"""Response construction and the security headers applied to every response.

Extracted from ``app.py``. These were nested functions inside ``create_app``, so
every route reached them through a closure; a blueprint cannot. Moving them to
module level is the prerequisite for splitting the routes out.

``success`` and ``failure`` exist so the envelope (``{"ok": ..., "data"/
"message": ...}``) is written once. Every endpoint returns it, and the front end
depends on its exact shape.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from flask import Response, current_app, jsonify, request, send_file

# reka-ui's ScrollArea (used by the navigation sidebars and the tab bar)
# renders one fixed <style> element that hides the native scrollbar underneath
# its custom one. Allowing exactly that text by hash keeps the policy free of
# 'unsafe-inline'. tests/test_frontend_build.py recomputes the hash from the
# committed bundle, so a library upgrade that changes the text fails the test
# suite instead of silently bringing the double scrollbar back.
REKA_SCROLL_AREA_STYLE_HASH = "'sha256-6kJHBrMeYPjuL963zk84BmND3Z4cszEoZMFiRK2nkPY='"

# The policy the SPA actually needs: its own origin for scripts, styles and
# XHR, plus data: URIs for images (QR previews and the bundled icon masks).
# No inline scripts, no inline <style> beyond the hash above, no third-party
# origin, no plugins, no foreign <base>, no framing by other sites. The Vite
# build fails if index.html would violate it (frontend/apps/web/vite/
# plugins.ts, "pts-csp-guard").
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; img-src 'self' data:; "
    f"style-src 'self' {REKA_SCROLL_AREA_STYLE_HASH}; "
    "script-src 'self'; connect-src 'self'; object-src 'none'; "
    "base-uri 'self'; form-action 'self'; frame-ancestors 'self'"
)

# Vite writes content-hashed file names here, so a given URL never changes
# content: browsers may keep them for a year and never revalidate.
HASHED_ASSET_PREFIX = "/static/dist/assets/"
HASHED_ASSET_CACHE_CONTROL = "public, max-age=31536000, immutable"

FRONTEND_NOT_BUILT_MESSAGE = (
    "前端页面尚未构建。请在 frontend 目录执行 pnpm install 与 pnpm build，"
    "或使用随程序发布的 static/dist 目录。"
)

#: Suffixes worth compressing, and the ones tools/build_frontend.py generates
#: ``.gz`` siblings for. Kept here so the build and the server cannot disagree
#: about which files have a compressed copy — a file the server looks for and the
#: build never wrote is a silent miss, not an error.
COMPRESSIBLE_SUFFIXES = frozenset({".js", ".mjs", ".css", ".html", ".svg", ".json"})


def precompressed_path(asset: Path) -> Path | None:
    """The ``.gz`` sibling of ``asset``, when it exists and is not stale.

    The freshness check matters because the bundle is committed: a build that
    regenerates the assets but not their compressed copies would otherwise serve
    the previous build's bytes under ``Content-Encoding: gzip``, which the browser
    cannot detect — it would decode to garbage rather than fail.
    """
    if asset.suffix.lower() not in COMPRESSIBLE_SUFFIXES:
        return None
    compressed = asset.with_name(asset.name + ".gz")
    if not compressed.is_file():
        return None
    if compressed.stat().st_mtime < asset.stat().st_mtime:
        return None
    return compressed


def compress_response(response: Response) -> Response:
    """Serve the pre-compressed copy of a hashed asset when the client accepts it.

    The bundle is about 2.4 MB of JavaScript and CSS and is sent by the
    application itself — on Windows the deployment is Waitress with no reverse
    proxy in front, so there is nothing else that could compress it. gzip takes
    it to about 0.7 MB, which is the difference between a workstation picking up
    a new deployment in a moment and waiting on it.

    Compression happens at build time, not here: this only picks a file. That
    keeps per-request CPU out of the serving path and means the same bytes are
    sent to every client.

    Scoped to ``/static/dist/assets/`` deliberately. Those names are
    content-hashed and cached immutably for a year, so no client ever revalidates
    them and the ETag — which Werkzeug computed from the uncompressed file — is
    never compared. Outside that prefix the ETag would have to be made
    variant-aware, and the remaining files are small enough not to be worth it.
    """
    if response.status_code != 200:
        return response
    if not request.path.startswith(HASHED_ASSET_PREFIX):
        return response
    if "gzip" not in request.accept_encodings:
        return response
    if response.headers.get("Content-Encoding"):
        return response

    relative = request.path[len("/static/"):]
    asset = (Path(current_app.root_path) / "static" / relative).resolve()
    compressed = precompressed_path(asset)
    if compressed is None:
        return response

    body = compressed.read_bytes()
    response.direct_passthrough = False
    response.set_data(body)
    response.headers["Content-Encoding"] = "gzip"
    response.headers["Content-Length"] = str(len(body))
    # Without this a shared cache could hand the compressed body to a client that
    # did not ask for it.
    response.headers.add("Vary", "Accept-Encoding")
    return response


def success(data: Any = None, status: int = 200):
    """The success envelope: ``{"ok": true, "data": ...}``."""
    return jsonify({"ok": True, "data": data}), status


def failure(message: str, status: int = 400):
    """The failure envelope: ``{"ok": false, "message": ...}``."""
    return jsonify({"ok": False, "message": message}), status


def frontend_entry_response(relative_path: str) -> Response:
    """Serve the built single-page application entry (``index.html``).

    The file is the output of the Vite build, not a Jinja template: it is sent
    as-is, so brace sequences in the minified markup can never be interpreted.
    ``no-cache`` makes the browser revalidate it on every load — the entry is
    what points at the current hashed bundles, so a deployment is picked up by
    the next page load without anyone clearing a cache.
    """
    entry = (Path(current_app.root_path) / relative_path).resolve()
    if not entry.is_file():
        return Response(
            FRONTEND_NOT_BUILT_MESSAGE, status=503, mimetype="text/plain"
        )
    response = send_file(entry, mimetype="text/html", conditional=True, max_age=0)
    response.headers["Cache-Control"] = "no-cache"
    return response


def secure_response(response: Response) -> Response:
    """Apply the baseline security headers to every outgoing response.

    ``setdefault`` throughout so a handler that sets its own value (for example
    a cacheable static asset) wins.
    """
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    if request.path.startswith("/api/"):
        # API responses carry credentials and live data; never let a proxy or
        # the browser hold on to them.
        response.headers.setdefault("Cache-Control", "no-store")
    elif request.path.startswith(HASHED_ASSET_PREFIX) and response.status_code in {200, 304}:
        response.headers["Cache-Control"] = HASHED_ASSET_CACHE_CONTROL
    response.headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
    return response
