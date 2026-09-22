"""The application's error type.

Extracted from ``app.py`` so that route modules (and eventually blueprints) can
raise the same error the application already knows how to render, without
importing the application itself. ``app.py`` re-exports it, so
``from app import ApiError`` keeps working.

``AuthError`` lives in ``traceability/auth.py`` for the same reason; both are
rendered by error handlers registered in ``create_app``.
"""

from __future__ import annotations


class ApiError(Exception):
    """An error carrying the HTTP status and message to show the user.

    The message is user-facing Chinese text written for the operator at the
    workstation, not for a developer reading a log — keep it actionable.
    """

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status
