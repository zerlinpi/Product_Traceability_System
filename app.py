"""Application factory.

``create_app()`` builds the Flask application: configuration (with the startup
guard that refuses the shipped default secrets in production), the database and
authentication layers, the error handlers that render every failure in the one
response envelope, the two non-API routes (``GET /`` serves the built front end,
``GET /favicon.ico``) and the registration of every blueprint.

Every API route lives in a blueprint under ``traceability/api/`` and the domain
logic in ``traceability/*`` (SYSTEM_ARCHITECTURE.md §10). The names imported
with a redundant ``as`` alias near the top are re-exports for callers that still
import them from ``app``.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import timedelta
from pathlib import Path
from typing import Any

from flask import Flask, Response, request
from werkzeug.exceptions import HTTPException

from traceability.api.bluetooth import bluetooth_bp
from traceability.api.product_families import product_families_bp
from traceability.api.part_types import part_types_bp
from traceability.api.machines import machines_bp
from traceability.api.suppliers import suppliers_bp
from traceability.api.product_models import product_models_bp
from traceability.api.production_batches import production_batches_bp
from traceability.api.purchase_orders import purchase_orders_bp
from traceability.api.production_orders import production_orders_bp
from traceability.api.inbound_receipts import inbound_receipts_bp
from traceability.api.scan_gun import scan_gun_bp
from traceability.api.dashboard import dashboard_bp
from traceability.api.settings import settings_bp
from traceability.api.products import products_bp
from traceability.api.code_sets import code_sets_bp
from traceability.api.batch_records import batch_records_bp
from traceability.api.supplier_inventory import supplier_inventory_bp
from traceability.api.part_labels import part_labels_bp
from traceability.api.scan_sessions import scan_sessions_bp
from traceability.api.records import records_bp
from traceability.api.inventory_sync import inventory_sync_bp
from traceability.ble_collector import BluetoothCollectionError
from traceability.db import initialize_database
from traceability.auth import initialize_auth
from traceability.errors import ApiError
from traceability.login_guard import LoginPolicy
from traceability.responses import (
    failure,
    frontend_entry_response,
    secure_response,
)
from traceability.validators import now_iso
from traceability.endpoint_policy import EndpointPolicy
from traceability.lingxing import (
    DEFAULT_API_BASE_URL,
    DEFAULT_INVENTORY_RECEIVE_PATH,
    DEFAULT_PURCHASE_ORDER_PATH,
    DEFAULT_RECEIPT_LIST_PATH,
    DEFAULT_REFRESH_TOKEN_PATH,
    DEFAULT_TOKEN_PATH,
    LingxingError,
)

# Re-exported for callers that still import these names from ``app`` (tests and
# tools). Each one now lives in the module that owns it; the redundant ``as``
# alias marks the import as an intentional re-export rather than an unused one.
from traceability.legacy_scan import (
    LEGACY_ENTRY_DISABLED_MESSAGE as LEGACY_ENTRY_DISABLED_MESSAGE,
)
from traceability.lingxing_writes import LINGXING_TOKEN_CACHE as LINGXING_TOKEN_CACHE
from traceability.products import (
    PRODUCT_ATTRIBUTE_COLUMNS as PRODUCT_ATTRIBUTE_COLUMNS,
    PRODUCT_ATTRIBUTE_DERIVED_COLUMNS as PRODUCT_ATTRIBUTE_DERIVED_COLUMNS,
)
from traceability.settings_service import (
    clean_lingxing_endpoint as clean_lingxing_endpoint,
)


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DATABASE = BASE_DIR / "data" / "traceability.db"
DEFAULT_SECRET_KEY = "pts-local-development-key-change-before-server-deployment"
DEFAULT_BOOTSTRAP_PASSWORD = "Admin@12345"
EXAMPLE_SECRET_KEY = "replace-with-a-random-string-of-at-least-32-characters"
EXAMPLE_BOOTSTRAP_PASSWORD = "replace-with-a-strong-initial-password"


def create_app(test_config: dict[str, Any] | None = None) -> Flask:
    # The UI is a single-page application built from frontend/ into
    # static/dist (served by the GET / route below). It is sent as a file
    # rather than rendered as a Jinja template, so there is no template cache
    # that could serve stale markup against a freshly deployed bundle.
    app = Flask(__name__, static_folder="static")
    app.config.from_mapping(
        DATABASE=str(DEFAULT_DATABASE),
        JSON_AS_ASCII=False,
        SECRET_KEY=os.environ.get("PTS_SECRET_KEY", DEFAULT_SECRET_KEY),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("PTS_HTTPS_ONLY", "0") == "1",
        PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
        AUTH_DISABLED=False,
        BOOTSTRAP_ADMIN_USERNAME=os.environ.get("PTS_BOOTSTRAP_ADMIN_USERNAME", "admin"),
        BOOTSTRAP_ADMIN_PASSWORD=os.environ.get("PTS_BOOTSTRAP_ADMIN_PASSWORD", DEFAULT_BOOTSTRAP_PASSWORD),
        BOOTSTRAP_ADMIN_DISPLAY_NAME=os.environ.get("PTS_BOOTSTRAP_ADMIN_DISPLAY_NAME", "系统管理员"),
        NOW_PROVIDER=now_iso,
        LINGXING_HTTP_CLIENT=None,
        LINGXING_SERVICE_FACTORY=None,
        LINGXING_CLOCK=None,
        LINGXING_SLEEP=None,
        # Login throttle thresholds. See traceability/login_guard.py for the
        # defaults and why the lockout is anchored to the oldest failure.
        LOGIN_POLICY=LoginPolicy.from_environment(),
        LINGXING_API_BASE_URL=os.environ.get(
            "PTS_LINGXING_API_BASE_URL", DEFAULT_API_BASE_URL
        ),
        # Outbound endpoint policy: which hosts the Lingxing feature may contact.
        # Defaults to openapi.lingxing.com only; see traceability/endpoint_policy.py.
        LINGXING_ENDPOINT_POLICY=EndpointPolicy.from_environment(),
        LINGXING_ENDPOINTS={
            "token": os.environ.get("PTS_LINGXING_TOKEN_URL", DEFAULT_TOKEN_PATH),
            "refresh_token": os.environ.get(
                "PTS_LINGXING_REFRESH_TOKEN_URL", DEFAULT_REFRESH_TOKEN_PATH
            ),
            # 采购单下单 (setOrders), 快捷入库 (fastReceive) and 查询收货单列表
            # (getOrderList) all have documented, stable routes, so they ship as
            # defaults. 库存同步 resolves each order's 收货单 via getOrderList and
            # then receives it with fastReceive. Only 供应收货 stays opt-in.
            "purchase_order": os.environ.get(
                "PTS_LINGXING_PURCHASE_ORDER_URL", DEFAULT_PURCHASE_ORDER_PATH
            ),
            "inbound_receipt": os.environ.get("PTS_LINGXING_INBOUND_URL", ""),
            "receipt_list": os.environ.get(
                "PTS_LINGXING_RECEIPT_LIST_URL", DEFAULT_RECEIPT_LIST_PATH
            ),
            "inventory_sync": os.environ.get(
                "PTS_LINGXING_INVENTORY_URL", DEFAULT_INVENTORY_RECEIVE_PATH
            ),
        },
        LINGXING_MAX_RETRIES=3,
        LINGXING_REQUEST_TIMEOUT=30,
        LINGXING_RETRY_INTERVAL=2,
    )
    if test_config:
        app.config.update(test_config)
        if test_config.get("TESTING") and "AUTH_DISABLED" not in test_config:
            app.config["AUTH_DISABLED"] = True
    deployment_mode = os.environ.get("PTS_ENV", "development").strip().lower()
    if not app.config.get("TESTING") and deployment_mode == "production":
        if (
            app.config["SECRET_KEY"] in {DEFAULT_SECRET_KEY, EXAMPLE_SECRET_KEY}
            or len(app.config["SECRET_KEY"]) < 32
        ):
            raise RuntimeError("生产模式必须设置至少 32 位的 PTS_SECRET_KEY")
        if app.config["BOOTSTRAP_ADMIN_PASSWORD"] in {
            DEFAULT_BOOTSTRAP_PASSWORD,
            EXAMPLE_BOOTSTRAP_PASSWORD,
        }:
            raise RuntimeError("生产模式必须修改 PTS_BOOTSTRAP_ADMIN_PASSWORD")
    initialize_database(app)
    initialize_auth(app)

    app.after_request(secure_response)

    @app.errorhandler(ApiError)
    def handle_api_error(error: ApiError):
        return failure(error.message, error.status)

    @app.errorhandler(ValueError)
    def handle_value_error(error: ValueError):
        return failure(str(error), 400)

    @app.errorhandler(BluetoothCollectionError)
    def handle_bluetooth_error(error: BluetoothCollectionError):
        return failure(str(error), 503)

    @app.errorhandler(LingxingError)
    def handle_lingxing_error(error: LingxingError):
        return failure(error.message, error.status)

    @app.errorhandler(sqlite3.IntegrityError)
    def handle_integrity_error(error: sqlite3.IntegrityError):
        app.logger.warning("Database constraint rejected a request: %s", error)
        return failure("数据已存在或已被其他工位使用，请刷新后重试", 409)

    @app.errorhandler(HTTPException)
    def handle_http_error(error: HTTPException):
        if request.path.startswith("/api/"):
            message = "接口不存在" if error.code == 404 else str(error.description)
            return failure(message, error.code)
        return error

    @app.errorhandler(Exception)
    def handle_unexpected_error(error: Exception):
        app.logger.exception("Unhandled traceability error", exc_info=error)
        return failure("系统处理失败，请稍后重试", 500)


    @app.get("/")
    def index():
        return frontend_entry_response("static/dist/index.html")

    @app.get("/favicon.ico")
    def favicon():
        return Response(status=204)

    # Blueprints are registered in one place so the full set of route modules is
    # visible without reading the whole factory. Each is a domain that no longer
    # needs to live inside create_app().
    app.register_blueprint(bluetooth_bp)
    app.register_blueprint(product_families_bp)
    app.register_blueprint(part_types_bp)
    app.register_blueprint(machines_bp)
    app.register_blueprint(suppliers_bp)
    app.register_blueprint(product_models_bp)
    app.register_blueprint(production_batches_bp)
    app.register_blueprint(purchase_orders_bp)
    app.register_blueprint(production_orders_bp)
    app.register_blueprint(inbound_receipts_bp)
    app.register_blueprint(scan_gun_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(settings_bp)
    app.register_blueprint(products_bp)
    app.register_blueprint(code_sets_bp)
    app.register_blueprint(batch_records_bp)
    app.register_blueprint(supplier_inventory_bp)
    app.register_blueprint(part_labels_bp)
    app.register_blueprint(scan_sessions_bp)
    app.register_blueprint(records_bp)
    app.register_blueprint(inventory_sync_bp)

    return app


if __name__ == "__main__":
    application = create_app()
    host = os.environ.get("TRACE_HOST", "0.0.0.0")
    port = int(os.environ.get("TRACE_PORT", "5080"))
    print(f"聚星同创仓库管理系统已启动：http://127.0.0.1:{port}")
    application.run(host=host, port=port, threaded=True, debug=False)
