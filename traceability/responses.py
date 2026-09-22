"""Response construction and the security headers applied to every response.

Extracted from ``app.py``. These were nested functions inside ``create_app``, so
every route reached them through a closure; a blueprint cannot. Moving them to
module level is the prerequisite for splitting the routes out.

``success`` and ``failure`` exist so the envelope (``{"ok": ..., "data"/
"message": ...}``) is written once. Every endpoint returns it, and the front end
depends on its exact shape.
"""

from __future__ import annotations

from typing import Any

from flask import Response, jsonify, request

# The policy the SPA actually needs: its own origin for scripts, styles and
# XHR, plus data: URIs for inline images (the QR preview). No inline scripts.
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; img-src 'self' data:; style-src 'self'; "
    "script-src 'self'; connect-src 'self'"
)


def success(data: Any = None, status: int = 200):
    """The success envelope: ``{"ok": true, "data": ...}``."""
    return jsonify({"ok": True, "data": data}), status


def failure(message: str, status: int = 400):
    """The failure envelope: ``{"ok": false, "message": ...}``."""
    return jsonify({"ok": False, "message": message}), status


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
    response.headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
    return response
