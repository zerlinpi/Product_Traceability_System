"""Property-based test for production-batch generation authorization (Property 26).

This exercises ``POST /api/production-batches`` (app.py, task 6.1) under the
authorization model of Requirements 9.1 and 9.3:

* an administrator and an authorized warehouse user may generate a batch; and
* an operations user who is neither an administrator nor a warehouse user is
  rejected, with **no** production batch created and **no** supplier inventory
  deducted.

The final role model is exactly ``ADMIN`` / ``WAREHOUSE`` / ``OPERATIONS``.
The generation endpoint guards with ``require_admin_or_warehouse()`` followed
by ``require_product_model_access()``. The warehouse actor is authorized for
the product model while the operations actor is rejected with 403 before any
batch is created or any stock is deducted.

The tests drive the real Flask ``test_client`` with authentication enabled
(``AUTH_DISABLED=False``), logging in as each role, and assert the observable
outcome (HTTP status, production-batch count, supplier balances) directly
against the database.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from werkzeug.security import generate_password_hash

from app import create_app
from traceability.codes import batch_identification_code, parse_batch_payload
from traceability.db import connect_database

from batch_strategies import entity_code_prefixes, per_unit_usages


# Keep planned quantities modest so that seeding twice the worst-case stock stays
# well under the supplier-inventory-batch creation cap (10,000,000 units) while
# still exercising the accepted 1..999999 range including its boundaries.
_MAX_PLANNED_QUANTITY = 999_999
_USER_PASSWORD = "Passw0rd!123"


def _bounded_quantities() -> st.SearchStrategy[int]:
    """Valid planned quantities biased to boundaries, capped for stock seeding."""

    return st.one_of(
        st.sampled_from([1, 999999]),
        st.integers(min_value=1, max_value=_MAX_PLANNED_QUANTITY),
    ).filter(lambda value: value <= _MAX_PLANNED_QUANTITY)


def _auth_app(db_path: Path):
    """Create an auth-enabled app (a bootstrap admin is created automatically)."""

    return create_app(
        {
            "TESTING": True,
            "AUTH_DISABLED": False,
            "SECRET_KEY": "batch-authz-test-secret",
            "DATABASE": str(db_path),
            "BOOTSTRAP_ADMIN_USERNAME": "bootstrap.admin",
            "BOOTSTRAP_ADMIN_PASSWORD": "Bootstrap@12345",
            "BOOTSTRAP_ADMIN_DISPLAY_NAME": "引导管理员",
        }
    )


def _insert_user(
    db_path: Path,
    username: str,
    role: str,
    product_model_ids: tuple[int, ...] = (),
) -> int:
    """Insert a ready-to-login user (no forced password change) directly.

    Creating users through ``POST /api/users`` sets ``must_change_password=1``,
    which would trap the login behind a 428 before the authorization guard runs.
    Inserting directly with ``must_change_password=0`` lets each role log in and
    reach the generation endpoint so the *authorization* behavior is tested.
    """

    connection = connect_database(str(db_path))
    try:
        connection.execute("BEGIN IMMEDIATE")
        cursor = connection.execute(
            """
            INSERT INTO users(
                username, display_name, password_hash, role, active,
                must_change_password, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 1, 0, ?, ?)
            """,
            (
                username,
                username,
                generate_password_hash(_USER_PASSWORD),
                role,
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
            ),
        )
        user_id = int(cursor.lastrowid)
        for product_model_id in product_model_ids:
            connection.execute(
                """
                INSERT INTO user_product_model_permissions(user_id, product_model_id)
                VALUES (?, ?)
                """,
                (user_id, product_model_id),
            )
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()
    return user_id


def _login(app, username: str):
    """Log in and return a ``(client, csrf_token)`` pair for the given user."""

    client = app.test_client()
    response = client.post(
        "/api/auth/login", json={"username": username, "password": _USER_PASSWORD}
    )
    assert response.status_code == 200, response.get_json()
    csrf = response.get_json()["data"]["csrfToken"]
    return client, csrf


def _post(client, csrf: str, path: str, payload: dict, expected: int = 201) -> dict:
    response = client.post(path, json=payload, headers={"X-CSRF-Token": csrf})
    assert response.status_code == expected, response.get_json()
    return response.get_json()["data"]


def _build_product_scenario(client, csrf: str, usage: list[int], stock_per_batch: int) -> dict:
    """Materialize a product + supplier stock scenario as an authenticated admin.

    Mirrors ``batch_strategies.build_product_scenario`` but threads the CSRF
    token through every write so it works with authentication enabled. For each
    per-unit usage count a supplier + part + seeded inventory batch is created
    and bound as a product component with ``quantity == per_unit``.
    """

    inventory_batch_ids: list[int] = []
    components: list[dict] = []
    for index, per_unit in enumerate(usage):
        suffix = f"AUTHZ-{index}"
        supplier = _post(
            client,
            csrf,
            "/api/suppliers",
            {"supplierCode": f"SUP-{suffix}", "name": f"供应商 {suffix}"},
        )
        part = _post(
            client,
            csrf,
            "/api/part-types",
            {
                "partCode": f"PART-{suffix}",
                "name": f"部件 {suffix}",
                "supplierId": supplier["id"],
                "specification": f"SPEC-{suffix}",
            },
        )
        inventory = _post(
            client,
            csrf,
            "/api/supplier-inventory-batches",
            {
                "partTypeId": part["id"],
                "quantity": stock_per_batch,
                "batchNo": f"SIB-{suffix}",
                "productionDate": "20260101",
                "receivedDate": "20260102",
                "remarks": f"结存 {suffix}",
            },
        )
        inventory_batch_ids.append(inventory["id"])
        components.append({"inventoryBatchId": inventory["id"], "quantity": per_unit})

    product = _post(
        client, csrf, "/api/products", {"name": "走步机 AUTHZ", "components": components}
    )
    return {
        "product_model_id": product["id"],
        "inventory_batch_ids": inventory_batch_ids,
        "usage": list(usage),
    }


def _balances(connection, batch_ids: list[int]) -> dict[int, int]:
    balances: dict[int, int] = {}
    for batch_id in batch_ids:
        row = connection.execute(
            "SELECT quantity_available FROM supplier_inventory_batches WHERE id = ?",
            (batch_id,),
        ).fetchone()
        balances[batch_id] = row["quantity_available"]
    return balances


def _batch_count(connection) -> int:
    return connection.execute(
        "SELECT COUNT(*) AS n FROM production_batches"
    ).fetchone()["n"]


def _batch_codes(connection) -> set[str]:
    rows = connection.execute("SELECT batch_code FROM production_batches").fetchall()
    return {row["batch_code"] for row in rows}


# Feature: batch-traceability, Property 26: 生成授权
@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(
    usage=per_unit_usages(),
    quantity=_bounded_quantities(),
    prefix=entity_code_prefixes(),
    slack=st.integers(min_value=0, max_value=100),
)
def test_batch_generation_authorization(
    usage: list[int], quantity: int, prefix: str, slack: int
) -> None:
    """管理员或仓管允许生成；既非管理员亦非仓管者被拒且无批次、无库存扣减。

    Validates: Requirements 9.1, 9.3
    """

    workdir = Path(tempfile.mkdtemp(prefix="pts-batch-authz-"))
    db_path = workdir / "traceability.db"
    try:
        app = _auth_app(db_path)

        # An admin builds the product scenario. Seed each supplier batch with
        # enough stock for the two allowed generations (admin + warehouse).
        stock_per_batch = 2 * quantity * max(usage) + slack
        _insert_user(db_path, "gen.admin", "ADMIN")
        admin_client, admin_csrf = _login(app, "gen.admin")
        scenario = _build_product_scenario(
            admin_client, admin_csrf, usage, stock_per_batch
        )
        product_model_id = scenario["product_model_id"]
        batch_ids = scenario["inventory_batch_ids"]
        total_per_batch_deduction = quantity * sum(usage)

        # The warehouse user is authorized for the product model; the operations
        # user represents the explicit "neither admin nor warehouse" role.
        _insert_user(
            db_path, "wh.operator", "WAREHOUSE", product_model_ids=(product_model_id,)
        )
        _insert_user(db_path, "other.operator", "OPERATIONS", product_model_ids=())

        warehouse_client, warehouse_csrf = _login(app, "wh.operator")
        unauthorized_client, unauthorized_csrf = _login(app, "other.operator")

        generation_payload = {
            "productModelId": product_model_id,
            "quantity": quantity,
            "prefix": prefix,
        }

        connection = connect_database(str(db_path))
        try:
            baseline_balances = _balances(connection, batch_ids)
            baseline_batches = _batch_count(connection)

            # (A) Neither-admin-nor-warehouse actor is rejected (Requirement 9.3):
            # 403, no production batch created, no supplier stock deducted.
            rejected = unauthorized_client.post(
                "/api/production-batches",
                json=generation_payload,
                headers={"X-CSRF-Token": unauthorized_csrf},
            )
            assert rejected.status_code == 403, rejected.get_json()
            assert rejected.get_json()["ok"] is False
            assert _batch_count(connection) == baseline_batches
            assert _balances(connection, batch_ids) == baseline_balances

            # (B) An administrator may generate (Requirement 9.1): 201, exactly one
            # new production batch, and supplier stock deducted by N × per-unit usage.
            admin_created = _post(
                admin_client,
                admin_csrf,
                "/api/production-batches",
                generation_payload,
            )
            assert admin_created["batchCode"]
            assert _batch_count(connection) == baseline_batches + 1
            after_admin = _balances(connection, batch_ids)
            assert (
                sum(baseline_balances.values()) - sum(after_admin.values())
                == total_per_batch_deduction
            )

            # (C) The authorized warehouse user may also generate
            # (Requirement 9.1): another new batch and another deduction.
            warehouse_created = _post(
                warehouse_client,
                warehouse_csrf,
                "/api/production-batches",
                generation_payload,
            )
            assert warehouse_created["batchCode"]
            assert warehouse_created["batchCode"] != admin_created["batchCode"]
            assert _batch_count(connection) == baseline_batches + 2
            after_warehouse = _balances(connection, batch_ids)
            assert (
                sum(after_admin.values()) - sum(after_warehouse.values())
                == total_per_batch_deduction
            )
        finally:
            connection.close()
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# Feature: batch-traceability, Property 32: 后续打印复用既有码值
@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(
    usage=per_unit_usages(),
    quantity=_bounded_quantities(),
    prefix=entity_code_prefixes(),
    reprints=st.integers(min_value=1, max_value=6),
    slack=st.integers(min_value=0, max_value=100),
)
def test_subsequent_prints_reuse_existing_code(
    usage: list[int], quantity: int, prefix: str, reprints: int, slack: int
) -> None:
    """重复请求二维码 / 标签复用既有 ``batch_code``，不生成新码值，批次计数不变。

    After a production batch is generated, repeatedly requesting its QR
    (``GET /api/production-batches/<id>/qr``) and detail
    (``GET /api/production-batches/<id>``) must reuse the batch's existing
    ``batch_code``: the QR always encodes ``PTS:B:{batch_code}`` (byte-identical
    across re-prints, and parsing recovers the original code), the detail always
    reports the same ``batchCode``, and neither the ``production_batches`` row
    count nor the set of existing code values changes.

    Validates: Requirements 11.3
    """

    workdir = Path(tempfile.mkdtemp(prefix="pts-batch-reprint-"))
    db_path = workdir / "traceability.db"
    try:
        app = _auth_app(db_path)

        # An admin builds the product scenario and generates one batch. Seed
        # enough stock for that single generation (re-printing never deducts).
        stock_per_batch = quantity * max(usage) + slack
        _insert_user(db_path, "gen.admin", "ADMIN")
        admin_client, admin_csrf = _login(app, "gen.admin")
        scenario = _build_product_scenario(
            admin_client, admin_csrf, usage, stock_per_batch
        )
        product_model_id = scenario["product_model_id"]

        created = _post(
            admin_client,
            admin_csrf,
            "/api/production-batches",
            {
                "productModelId": product_model_id,
                "quantity": quantity,
                "prefix": prefix,
            },
        )
        batch_id = created["id"]
        batch_code = created["batchCode"]
        assert batch_code

        connection = connect_database(str(db_path))
        try:
            # Baseline snapshot taken *after* the single generation: the count
            # and the set of code values must not move during any re-prints.
            baseline_batches = _batch_count(connection)
            baseline_codes = _batch_codes(connection)
            assert batch_code in baseline_codes

            # The first QR download establishes the reference bytes. Because the
            # QR encodes only the existing ``batch_code``, every re-print of the
            # same batch renders the identical SVG.
            first_qr = admin_client.get(f"/api/production-batches/{batch_id}/qr")
            assert first_qr.status_code == 200, first_qr.get_data(as_text=True)
            assert first_qr.mimetype == "image/svg+xml"
            reference_svg = first_qr.get_data()
            assert b"<svg" in reference_svg

            for _ in range(reprints):
                # (A) Re-printing the QR reuses the existing code value: the
                # rendered SVG is byte-identical and its payload parses back to
                # the original ``batch_code`` (no new code minted).
                qr_response = admin_client.get(
                    f"/api/production-batches/{batch_id}/qr"
                )
                assert qr_response.status_code == 200, qr_response.get_data(
                    as_text=True
                )
                assert qr_response.mimetype == "image/svg+xml"
                assert qr_response.get_data() == reference_svg

                # (B) The detail view keeps reporting the same existing code.
                detail_response = admin_client.get(
                    f"/api/production-batches/{batch_id}"
                )
                assert detail_response.status_code == 200, detail_response.get_json()
                detail = detail_response.get_json()["data"]
                assert detail["batchCode"] == batch_code
                assert detail["identificationCode"] == batch_identification_code(
                    batch_code
                )
                assert parse_batch_payload(detail["identificationCode"]) == batch_code

                # (C) No new batch row and no new code value: the count and the
                # full set of code values are unchanged by re-printing.
                assert _batch_count(connection) == baseline_batches
                assert _batch_codes(connection) == baseline_codes
        finally:
            connection.close()
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
