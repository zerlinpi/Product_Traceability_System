from __future__ import annotations

import json
import re
import secrets
import sqlite3
from datetime import timedelta
from typing import Any

from flask import Flask, current_app, g, jsonify, request, session
from werkzeug.security import check_password_hash, generate_password_hash

from traceability import login_guard, passwords
from traceability.audit_chain import link_event
from traceability.capabilities import (
    ROLE_ADMIN,
    ROLE_OPERATIONS,
    ROLE_WAREHOUSE,
    VALID_ROLES,
    Capability,
    missing_capabilities,
)
from traceability.codes import new_event_id
from traceability.db import get_db


USERNAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,49}")


class AuthError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def _user_scope(database: sqlite3.Connection, user_id: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    product_models = [
        {"id": row["id"], "modelCode": row["model_code"], "name": row["name"]}
        for row in database.execute(
            """
            SELECT pm.id, pm.model_code, pm.name
            FROM user_product_model_permissions permission
            JOIN product_models pm ON pm.id = permission.product_model_id
            WHERE permission.user_id = ?
            ORDER BY pm.model_code COLLATE NOCASE
            """,
            (user_id,),
        ).fetchall()
    ]
    suppliers = [
        {"id": row["id"], "supplierCode": row["supplier_code"], "name": row["name"]}
        for row in database.execute(
            """
            SELECT s.id, s.supplier_code, s.name
            FROM user_supplier_permissions permission
            JOIN suppliers s ON s.id = permission.supplier_id
            WHERE permission.user_id = ?
            ORDER BY s.supplier_code COLLATE NOCASE
            """,
            (user_id,),
        ).fetchall()
    ]
    return product_models, suppliers


def user_dict(
    row: sqlite3.Row | dict[str, Any],
    *,
    include_csrf: bool = False,
    database: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    result = {
        "id": row["id"],
        "username": row["username"],
        "displayName": row["display_name"],
        "role": row["role"],
        "active": bool(row["active"]),
        "mustChangePassword": bool(row["must_change_password"]),
        "lastLoginAt": row["last_login_at"],
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }
    if database is not None and row["id"] is not None:
        product_models, suppliers = _user_scope(database, int(row["id"]))
        result["productModels"] = product_models
        result["productModelIds"] = [item["id"] for item in product_models]
        result["suppliers"] = suppliers
        result["supplierIds"] = [item["id"] for item in suppliers]
    else:
        result["productModels"] = []
        result["productModelIds"] = []
        result["suppliers"] = []
        result["supplierIds"] = []
    if include_csrf:
        result["csrfToken"] = session.get("csrf_token", "")
    return result


def current_user() -> sqlite3.Row | dict[str, Any] | None:
    return getattr(g, "current_user", None)


def current_actor_id() -> int | None:
    user = current_user()
    return int(user["id"]) if user and user["id"] is not None else None


def current_actor_name(fallback: str = "") -> str:
    user = current_user()
    return str(user["display_name"]) if user else fallback


def require_capability(*capabilities: Capability) -> None:
    """Authorise the current user for one or more capabilities.

    This is the preferred guard for new code. The policy it consults lives in
    ``traceability/capabilities.py``, so "which role may do this" is reviewable
    in one place instead of being spread across the route handlers.

    Passing several capabilities requires **all** of them.
    """
    user = current_user()
    if not user:
        raise AuthError("登录状态已失效，请重新登录", 401)
    missing = missing_capabilities(str(user["role"]), capabilities)
    if missing:
        names = "、".join(item.value for item in missing)
        raise AuthError(f"当前角色无权执行此操作（缺少权限：{names}）", 403)


def require_admin() -> None:
    user = current_user()
    if not user or user["role"] != ROLE_ADMIN:
        raise AuthError("仅管理员可以执行此操作", 403)


def require_warehouse() -> None:
    """ADMIN or WAREHOUSE.

    Note the name: there is **no** "warehouse only, excluding admin" level in
    this system. ADMIN is a superuser and passes every role guard.
    """
    user = current_user()
    if not user or user["role"] not in {ROLE_ADMIN, ROLE_WAREHOUSE}:
        raise AuthError("仅管理员或仓管可以执行此操作", 403)


# Historical alias. ``require_admin_or_warehouse`` and ``require_warehouse`` were
# two separate implementations that happened to be identical; they are now one
# function under two names so they can never drift apart. New code should prefer
# ``require_capability(Capability.X)``.
require_admin_or_warehouse = require_warehouse


def require_operations() -> None:
    user = current_user()
    if not user or user["role"] not in {ROLE_ADMIN, ROLE_OPERATIONS}:
        raise AuthError("仅管理员或运营可以执行此操作", 403)


def current_operator_id() -> int | None:
    # Product/supplier scoping is now disabled: warehouse operators handle every
    # product (batch generation, field registration, scan-gun inbound, trace),
    # not a hand-picked subset. Returning ``None`` for all roles makes every
    # scope check (require_product_model_access / require_supplier_access) and
    # every scoped list query treat the caller as unrestricted, exactly like an
    # administrator. Operations product privacy is handled separately via
    # ``created_by_user_id``. The per-user scope tables are retained for API
    # backward compatibility but no longer gate access.
    return None


def require_product_model_access(product_model_id: int) -> None:
    user_id = current_operator_id()
    if user_id is None:
        return
    allowed = get_db().execute(
        """
        SELECT 1 FROM user_product_model_permissions
        WHERE user_id = ? AND product_model_id = ?
        """,
        (user_id, product_model_id),
    ).fetchone()
    if not allowed:
        raise AuthError("管理员未授权你操作该产品型号", 403)


def require_supplier_access(supplier_id: int) -> None:
    user_id = current_operator_id()
    if user_id is None:
        return
    allowed = get_db().execute(
        """
        SELECT 1 FROM user_supplier_permissions
        WHERE user_id = ? AND supplier_id = ?
        """,
        (user_id, supplier_id),
    ).fetchone()
    if not allowed:
        raise AuthError("管理员未授权你操作该供应商", 403)


def _clean_text(value: object, label: str, *, max_length: int = 80) -> str:
    text = str(value or "").strip()
    if not text:
        raise AuthError(f"请输入{label}")
    if len(text) > max_length:
        raise AuthError(f"{label}不能超过 {max_length} 个字符")
    return text


def _validate_username(value: object) -> str:
    username = _clean_text(value, "账号", max_length=50)
    if not USERNAME_PATTERN.fullmatch(username):
        raise AuthError("账号需为 1-50 位字母、数字、点、下划线或短横线")
    return username


def _validate_password(value: object, *, username: object = "") -> str:
    """Reject a password the policy refuses.

    The username is passed so the policy can refuse a password equal to the
    account name; see ``traceability/passwords.py`` for what is and is not
    checked, and why the list is deliberately short.
    """
    password = str(value or "")
    problem = passwords.password_policy_error(password, username=username)
    if problem:
        raise AuthError(problem)
    return password


def _parse_scope_ids(value: object, label: str) -> list[int]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise AuthError(f"{label}授权格式无效")
    result: list[int] = []
    for item in value:
        try:
            item_id = int(item)
        except (TypeError, ValueError) as error:
            raise AuthError(f"{label}授权格式无效") from error
        if item_id <= 0:
            raise AuthError(f"{label}授权格式无效")
        if item_id not in result:
            result.append(item_id)
    return result


def _validate_scope_records(
    database: sqlite3.Connection,
    product_model_ids: list[int],
    supplier_ids: list[int],
) -> None:
    if product_model_ids:
        placeholders = ",".join("?" for _ in product_model_ids)
        valid = {
            row["id"]
            for row in database.execute(
                f"SELECT id FROM product_models WHERE active = 1 AND id IN ({placeholders})",
                product_model_ids,
            ).fetchall()
        }
        if valid != set(product_model_ids):
            raise AuthError("产品型号授权中包含不存在或已停用的型号")
    if supplier_ids:
        placeholders = ",".join("?" for _ in supplier_ids)
        valid = {
            row["id"]
            for row in database.execute(
                f"SELECT id FROM suppliers WHERE id IN ({placeholders})",
                supplier_ids,
            ).fetchall()
        }
        if valid != set(supplier_ids):
            raise AuthError("供应商授权中包含不存在的供应商")


def _supplier_ids_for_products(
    database: sqlite3.Connection,
    product_model_ids: list[int],
) -> list[int]:
    """Keep the operator form product-centric while preserving supplier checks."""
    if not product_model_ids:
        return []
    placeholders = ",".join("?" for _ in product_model_ids)
    return [
        int(row["supplier_id"])
        for row in database.execute(
            f"""
            SELECT DISTINCT pt.supplier_id
            FROM trace_plans tp
            JOIN trace_plan_slots slot ON slot.trace_plan_id = tp.id
            JOIN part_types pt ON pt.id = slot.part_type_id
            WHERE tp.product_model_id IN ({placeholders})
            ORDER BY pt.supplier_id
            """,
            product_model_ids,
        ).fetchall()
    ]


def _replace_user_scope(
    database: sqlite3.Connection,
    user_id: int,
    role: str,
    product_model_ids: list[int],
    supplier_ids: list[int],
) -> None:
    database.execute("DELETE FROM user_product_model_permissions WHERE user_id = ?", (user_id,))
    database.execute("DELETE FROM user_supplier_permissions WHERE user_id = ?", (user_id,))
    if role != ROLE_WAREHOUSE:
        return
    database.executemany(
        "INSERT INTO user_product_model_permissions(user_id, product_model_id) VALUES (?, ?)",
        [(user_id, item_id) for item_id in product_model_ids],
    )
    database.executemany(
        "INSERT INTO user_supplier_permissions(user_id, supplier_id) VALUES (?, ?)",
        [(user_id, item_id) for item_id in supplier_ids],
    )


def _new_csrf_token() -> str:
    token = secrets.token_urlsafe(32)
    session["csrf_token"] = token
    return token


def _record_security_event(
    database: sqlite3.Connection,
    event_type: str,
    target: sqlite3.Row | dict[str, Any],
    *,
    actor: sqlite3.Row | dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    event_actor = actor or current_user() or target
    actor_id = event_actor["id"] if event_actor and event_actor["id"] is not None else None
    operator_name = str(event_actor["display_name"]) if event_actor else ""
    event_id = new_event_id()
    object_code = str(target["username"])
    payload_json = json.dumps(payload or {}, ensure_ascii=False, separators=(",", ":"))
    occurred_at = str(current_app.config["NOW_PROVIDER"]())
    # Security events go into the same append-only ledger as business events, so
    # they are covered by the hash chain too. ``link_event`` opens a short write
    # transaction when this call is not already inside one (login/logout are not).
    prev_hash, event_hash = link_event(
        database,
        {
            "event_id": event_id,
            "event_type": event_type,
            "object_type": "USER",
            "object_code": object_code,
            "related_object_code": "",
            "station_id": "",
            "station_name": "",
            "operator_name": operator_name,
            "actor_user_id": actor_id,
            "reason": "",
            "payload_json": payload_json,
            "occurred_at": occurred_at,
        },
    )
    database.execute(
        """
        INSERT INTO audit_events(
            event_id, event_type, object_type, object_code, operator_name,
            actor_user_id, payload_json, occurred_at, prev_hash, event_hash
        ) VALUES (?, ?, 'USER', ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_id,
            event_type,
            object_code,
            operator_name,
            actor_id,
            payload_json,
            occurred_at,
            prev_hash,
            event_hash,
        ),
    )


def initialize_auth(app: Flask) -> None:
    app.config.setdefault("AUTH_DISABLED", bool(app.config.get("TESTING")))
    app.config.setdefault("PERMANENT_SESSION_LIFETIME", timedelta(hours=12))

    if not app.config["AUTH_DISABLED"]:
        with app.app_context():
            database = get_db()
            existing = database.execute("SELECT id FROM users LIMIT 1").fetchone()
            if not existing:
                timestamp = app.config["NOW_PROVIDER"]()
                username = app.config["BOOTSTRAP_ADMIN_USERNAME"]
                password = app.config["BOOTSTRAP_ADMIN_PASSWORD"]
                database.execute(
                    """
                    INSERT INTO users(
                        username, display_name, password_hash, role, active,
                        must_change_password, created_at, updated_at
                    ) VALUES (?, ?, ?, 'ADMIN', 1, 1, ?, ?)
                    """,
                    (
                        username,
                        app.config["BOOTSTRAP_ADMIN_DISPLAY_NAME"],
                        generate_password_hash(password),
                        timestamp,
                        timestamp,
                    ),
                )
                app.logger.warning(
                    "Created bootstrap administrator '%s'; change its password after first login.",
                    username,
                )

    @app.errorhandler(AuthError)
    def handle_auth_error(error: AuthError):
        return jsonify({"ok": False, "message": error.message}), error.status

    @app.before_request
    def load_authenticated_user():
        if not request.path.startswith("/api/"):
            return
        if request.path in {"/api/health", "/api/auth/login"}:
            return

        if app.config["AUTH_DISABLED"]:
            g.current_user = {
                "id": None,
                "username": "test-admin",
                "display_name": "测试管理员",
                "role": ROLE_ADMIN,
                "active": 1,
                "must_change_password": 0,
                "last_login_at": None,
                "created_at": "",
                "updated_at": "",
            }
            return

        user_id = session.get("user_id")
        user = None
        if user_id:
            user = get_db().execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if not user or not user["active"]:
            session.clear()
            raise AuthError("登录状态已失效，请重新登录", 401)
        if session.get("auth_version") != user["session_version"]:
            session.clear()
            raise AuthError("账号凭据已更新，请重新登录", 401)
        g.current_user = user

        password_change_paths = {
            "/api/auth/me",
            "/api/auth/logout",
            "/api/auth/change-password",
        }
        if user["must_change_password"] and request.path not in password_change_paths:
            raise AuthError("首次登录必须先修改密码", 428)

        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            expected = session.get("csrf_token", "")
            supplied = request.headers.get("X-CSRF-Token", "")
            if not expected or not supplied or not secrets.compare_digest(expected, supplied):
                raise AuthError("页面安全令牌已失效，请刷新后重试", 403)
        return

    @app.post("/api/auth/login")
    def login():
        if app.config["AUTH_DISABLED"]:
            return jsonify({"ok": True, "data": user_dict({
                "id": None,
                "username": "test-admin",
                "display_name": "测试管理员",
                "role": ROLE_ADMIN,
                "active": 1,
                "must_change_password": 0,
                "last_login_at": None,
                "created_at": "",
                "updated_at": "",
            })})

        payload = request.get_json(silent=True) or {}
        username = str(payload.get("username") or "").strip()
        password = str(payload.get("password") or "")
        database = get_db()
        timestamp = app.config["NOW_PROVIDER"]()
        now_epoch = login_guard.to_epoch(timestamp)
        client_ip = request.remote_addr or ""
        policy = app.config.get("LOGIN_POLICY") or login_guard.LoginPolicy()

        # Throttle before verifying anything. The rejection is identical whether
        # the account exists or not, so the response cannot enumerate accounts.
        status = login_guard.check_lockout(
            database, username=username, ip=client_ip, now_epoch=now_epoch, policy=policy
        )
        if status.locked:
            _record_security_event(
                database,
                "USER_LOGIN_BLOCKED",
                {"id": None, "username": username or "(空)", "display_name": ""},
                payload={"reason": status.reason, "retryAfterSeconds": status.retry_after_seconds},
            )
            raise AuthError(status.message(), 429)

        row = database.execute(
            "SELECT * FROM users WHERE username = ? COLLATE NOCASE", (username,)
        ).fetchone()
        verified = bool(
            row and row["active"] and check_password_hash(row["password_hash"], password)
        )
        login_guard.record_attempt(
            database,
            username=username,
            ip=client_ip,
            succeeded=verified,
            occurred_at=timestamp,
            now_epoch=now_epoch,
        )
        if not verified:
            _record_security_event(
                database,
                "USER_LOGIN_FAILED",
                row or {"id": None, "username": username or "(空)", "display_name": ""},
                payload={"reason": "账号或密码错误"},
            )
            # Keep the table bounded without a separate maintenance job.
            login_guard.prune_attempts(database, now_epoch=now_epoch, policy=policy)
            raise AuthError("账号或密码错误", 401)

        # A verified login clears this account's failure budget so a forgotten
        # password does not linger. The per-IP budget deliberately survives.
        login_guard.clear_username_failures(database, username=username)
        database.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (timestamp, row["id"]))
        _record_security_event(database, "USER_LOGIN", row, actor=row)
        # A fresh session id on login: the pre-login session must not be reusable.
        session.clear()
        session.permanent = True
        session["user_id"] = row["id"]
        session["auth_version"] = row["session_version"]
        _new_csrf_token()
        row = database.execute("SELECT * FROM users WHERE id = ?", (row["id"],)).fetchone()
        return jsonify({"ok": True, "data": user_dict(row, include_csrf=True, database=database)})

    @app.get("/api/auth/me")
    def auth_me():
        user = current_user()
        if app.config["AUTH_DISABLED"] and "csrf_token" not in session:
            _new_csrf_token()
        return jsonify({"ok": True, "data": user_dict(user, include_csrf=True, database=get_db())})

    @app.post("/api/auth/logout")
    def logout():
        user = current_user()
        if user and not app.config["AUTH_DISABLED"]:
            _record_security_event(get_db(), "USER_LOGOUT", user)
        session.clear()
        return jsonify({"ok": True, "data": None})

    @app.post("/api/auth/change-password")
    def change_password():
        payload = request.get_json(silent=True) or {}
        current_password = str(payload.get("currentPassword") or "")
        user = current_user()
        new_password = _validate_password(payload.get("newPassword"), username=user["username"])
        if app.config["AUTH_DISABLED"]:
            return jsonify({"ok": True, "data": user_dict(user, include_csrf=True, database=get_db())})
        if not check_password_hash(user["password_hash"], current_password):
            raise AuthError("当前密码不正确", 400)
        if check_password_hash(user["password_hash"], new_password):
            raise AuthError("新密码不能与当前密码相同", 400)
        timestamp = app.config["NOW_PROVIDER"]()
        get_db().execute(
            """
            UPDATE users SET password_hash = ?, must_change_password = 0,
                session_version = session_version + 1, updated_at = ?
            WHERE id = ?
            """,
            (generate_password_hash(new_password), timestamp, user["id"]),
        )
        row = get_db().execute("SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone()
        _record_security_event(get_db(), "USER_PASSWORD_CHANGED", row, actor=row)
        session["auth_version"] = row["session_version"]
        _new_csrf_token()
        return jsonify({"ok": True, "data": user_dict(row, include_csrf=True, database=get_db())})

    @app.get("/api/users")
    def list_users():
        require_admin()
        database = get_db()
        rows = database.execute(
            "SELECT * FROM users ORDER BY active DESC, role, username COLLATE NOCASE"
        ).fetchall()
        return jsonify({"ok": True, "data": [user_dict(row, database=database) for row in rows]})

    @app.post("/api/users")
    def create_user():
        require_admin()
        payload = request.get_json(silent=True) or {}
        username = _validate_username(payload.get("username"))
        display_name = _clean_text(
            payload.get("displayName") or username, "姓名", max_length=60
        )
        password = _validate_password(payload.get("password"), username=username)
        role = str(payload.get("role") or "").upper()
        if role not in VALID_ROLES:
            raise AuthError("角色只能是管理员、仓管或运营")
        product_model_ids = _parse_scope_ids(payload.get("productModelIds"), "产品型号")
        database = get_db()
        if "supplierIds" in payload:
            supplier_ids = _parse_scope_ids(payload.get("supplierIds"), "供应商")
        else:
            supplier_ids = _supplier_ids_for_products(database, product_model_ids)
        _validate_scope_records(database, product_model_ids, supplier_ids)
        timestamp = app.config["NOW_PROVIDER"]()
        try:
            database.execute("BEGIN IMMEDIATE")
            cursor = database.execute(
                """
                INSERT INTO users(
                    username, display_name, password_hash, role, active,
                    must_change_password, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 1, 1, ?, ?)
                """,
                (username, display_name, generate_password_hash(password), role, timestamp, timestamp),
            )
            _replace_user_scope(
                database, cursor.lastrowid, role, product_model_ids, supplier_ids
            )
            row = database.execute("SELECT * FROM users WHERE id = ?", (cursor.lastrowid,)).fetchone()
            _record_security_event(
                database,
                "USER_CREATED",
                row,
                payload={
                    "role": row["role"],
                    "active": bool(row["active"]),
                    "productModelIds": product_model_ids,
                    "supplierIds": supplier_ids,
                },
            )
            database.commit()
        except sqlite3.IntegrityError as error:
            database.rollback()
            raise AuthError("该账号已存在", 409) from error
        except Exception:
            database.rollback()
            raise
        return jsonify({"ok": True, "data": user_dict(row, database=database)}), 201

    @app.put("/api/users/<int:user_id>")
    def update_user(user_id: int):
        require_admin()
        payload = request.get_json(silent=True) or {}
        database = get_db()
        target = database.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if not target:
            raise AuthError("账号不存在", 404)

        display_name = _clean_text(
            payload.get("displayName", target["display_name"]), "姓名", max_length=60
        )
        role = str(payload.get("role", target["role"])).upper()
        if role not in VALID_ROLES:
            raise AuthError("角色只能是管理员、仓管或运营")
        active = 1 if bool(payload.get("active", bool(target["active"]))) else 0
        actor = current_user()
        if actor["id"] == user_id and (not active or role != ROLE_ADMIN):
            raise AuthError("不能停用自己的账号或取消自己的管理员角色", 409)

        current_product_models, current_suppliers = _user_scope(database, user_id)
        current_product_ids = [item["id"] for item in current_product_models]
        current_supplier_ids = [item["id"] for item in current_suppliers]
        product_model_ids = (
            _parse_scope_ids(payload.get("productModelIds"), "产品型号")
            if "productModelIds" in payload
            else current_product_ids
        )
        if "supplierIds" in payload:
            supplier_ids = _parse_scope_ids(payload.get("supplierIds"), "供应商")
        else:
            derived_supplier_ids = _supplier_ids_for_products(database, product_model_ids)
            supplier_ids = derived_supplier_ids or current_supplier_ids
        if role != ROLE_WAREHOUSE:
            product_model_ids = []
            supplier_ids = []
        _validate_scope_records(database, product_model_ids, supplier_ids)
        scope_changed = (
            set(product_model_ids) != set(current_product_ids)
            or set(supplier_ids) != set(current_supplier_ids)
        )
        password_value = payload.get("password")
        password = _validate_password(password_value, username=target["username"]) if password_value else None
        timestamp = app.config["NOW_PROVIDER"]()
        invalidate_session = bool(password) or role != target["role"] or scope_changed
        database.execute("BEGIN IMMEDIATE")
        try:
            if password:
                database.execute(
                    """
                    UPDATE users SET display_name = ?, role = ?, active = ?,
                        password_hash = ?, must_change_password = 1,
                        session_version = session_version + 1, updated_at = ?
                    WHERE id = ?
                    """,
                    (display_name, role, active, generate_password_hash(password), timestamp, user_id),
                )
            else:
                database.execute(
                    """
                    UPDATE users SET display_name = ?, role = ?, active = ?,
                        session_version = session_version + ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (display_name, role, active, 1 if invalidate_session else 0, timestamp, user_id),
                )
            _replace_user_scope(database, user_id, role, product_model_ids, supplier_ids)
            row = database.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            _record_security_event(
                database,
                "USER_UPDATED",
                row,
                payload={
                    "displayNameChanged": display_name != target["display_name"],
                    "roleChanged": role != target["role"],
                    "activeChanged": bool(active) != bool(target["active"]),
                    "passwordReset": bool(password_value),
                    "scopeChanged": scope_changed,
                    "productModelIds": product_model_ids,
                    "supplierIds": supplier_ids,
                    "role": role,
                    "active": bool(active),
                },
            )
            database.commit()
        except Exception:
            database.rollback()
            raise
        return jsonify({"ok": True, "data": user_dict(row, database=database)})
