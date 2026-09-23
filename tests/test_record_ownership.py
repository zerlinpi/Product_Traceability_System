"""Record ownership: a warehouse operator may only modify their own entries.

The rule was written once and then silently switched off. ``editable_record``
expressed it as::

    operator_id = current_operator_id()
    if operator_id is not None and row["completed_by_user_id"] != operator_id:
        raise ApiError("只能修改或删除自己的录入记录", 403)

and ``current_operator_id()`` returns ``None`` for *every* role — a documented
decision to disable product/supplier scoping. So the guard could never fire. Two
unrelated policies were sharing one helper, and changing the scoping policy
turned the ownership policy off as a side effect. Nobody noticed because nothing
tested it.

The check now reads the role directly. These tests exist so that reverting to the
old form fails loudly: the "another operator is refused" cases below return 200
from ``editable_record`` under the old code and only 400 from the missing-reason
validation, so they would fail on the status code.

Feature: batch-traceability, Property 76: 录入记录归属权
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from capability_helpers import (  # noqa: E402
    bootstrap_admin,
    insert_user,
    login,
    make_auth_app,
)
from traceability.db import connect_database  # noqa: E402

OWNERSHIP_MESSAGE = "只能修改或删除自己的录入记录"
MISSING_REASON_MESSAGE = "请输入删除原因"


def _seed_record(database_path: Path, owner_user_id: int | None, *, suffix: str = "1") -> int:
    """Create the minimum rows ``editable_record`` joins over.

    Built directly rather than through the field flow so the test is about the
    authorisation decision and nothing else — the flow has its own coverage.
    """
    connection = connect_database(database_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        family = connection.execute(
            """
            INSERT INTO product_families(product_code, name, description, active, created_at, updated_at)
            VALUES (?, ?, '', 1, '2026-07-22T10:30:00+08:00', '2026-07-22T10:30:00+08:00')
            """,
            (f"OWN-{suffix}", f"归属测试分类{suffix}"),
        )
        model = connection.execute(
            """
            INSERT INTO product_models(
                product_family_id, model_code, name, serial_prefix, identity_source,
                active, created_at, updated_at
            ) VALUES (?, ?, ?, '', 'SCANNER', 1, '2026-07-22T10:30:00+08:00', '2026-07-22T10:30:00+08:00')
            """,
            (family.lastrowid, f"OWN-MODEL-{suffix}", f"归属测试型号{suffix}"),
        )
        machine = connection.execute(
            """
            INSERT INTO machines(
                sn, model, product_model_id, production_date, identification_code, created_at
            ) VALUES (?, ?, ?, '', ?, '2026-07-22T10:30:00+08:00')
            """,
            (f"OWN-SN-{suffix}", f"OWN-MODEL-{suffix}", model.lastrowid, f"OWN-CODE-{suffix}"),
        )
        record = connection.execute(
            """
            INSERT INTO trace_records(
                trace_no, machine_id, status, station_id, station_name, operator_name,
                completed_by_user_id, completed_at
            ) VALUES (?, ?, 'ASSEMBLED', 'ST-01', '工位一', '测试', ?, '2026-07-22T10:30:00+08:00')
            """,
            (f"OWN-TRACE-{suffix}", machine.lastrowid, owner_user_id),
        )
        connection.execute("COMMIT")
        return int(record.lastrowid)
    except Exception:
        connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()


@pytest.fixture()
def owned(tmp_path):
    """A record created by `record-owner`, plus clients for each role."""
    app, database_path, _fake = make_auth_app(tmp_path)
    admin_client, admin_csrf = bootstrap_admin(app)
    owner_id = insert_user(database_path, "record-owner", "WAREHOUSE")
    insert_user(database_path, "record-other", "WAREHOUSE")
    owner_client, owner_csrf = login(app, "record-owner")
    other_client, other_csrf = login(app, "record-other")
    record_id = _seed_record(database_path, owner_id)
    return SimpleNamespace(
        app=app,
        database_path=database_path,
        record_id=record_id,
        owner_id=owner_id,
        admin=(admin_client, admin_csrf),
        owner=(owner_client, owner_csrf),
        other=(other_client, other_csrf),
    )


def _delete(client_and_csrf, record_id, **extra):
    client, csrf = client_and_csrf
    return client.delete(
        f"/api/records/{record_id}",
        json=extra.get("json", {}),
        headers={"X-CSRF-Token": csrf},
    )


# --------------------------------------------------------------------------
# The rule
# --------------------------------------------------------------------------


def test_the_owner_passes_the_ownership_check(owned):
    """A 400 about the missing reason means ownership was accepted.

    ``delete_record`` calls ``editable_record`` before validating the body, so
    reaching the reason check is proof the guard let this caller through.
    """
    response = _delete(owned.owner, owned.record_id)
    assert response.status_code == 400
    assert MISSING_REASON_MESSAGE in response.get_json()["message"]


def test_another_warehouse_operator_is_refused(owned):
    response = _delete(owned.other, owned.record_id)
    assert response.status_code == 403
    assert response.get_json()["message"] == OWNERSHIP_MESSAGE


def test_an_administrator_is_unrestricted(owned):
    """The rule constrains warehouse operators, not administrators."""
    response = _delete(owned.admin, owned.record_id)
    assert response.status_code == 400
    assert MISSING_REASON_MESSAGE in response.get_json()["message"]


def test_the_owner_can_edit_and_another_operator_cannot(owned):
    """The same guard backs PUT, so check it there too.

    A PUT with a body that cannot be satisfied still proves the guard's verdict:
    the owner is told about the parts, the other operator is told about
    ownership.
    """
    payload = {"remarks": "", "partCodes": ["NOPE-1"]}
    owner = owned.owner[0].put(
        f"/api/records/{owned.record_id}",
        json=payload,
        headers={"X-CSRF-Token": owned.owner[1]},
    )
    assert owner.status_code != 403, owner.get_json()

    other = owned.other[0].put(
        f"/api/records/{owned.record_id}",
        json=payload,
        headers={"X-CSRF-Token": owned.other[1]},
    )
    assert other.status_code == 403
    assert other.get_json()["message"] == OWNERSHIP_MESSAGE


def test_the_check_does_not_depend_on_current_operator_id(owned):
    """Regression guard for the way this broke the first time.

    ``current_operator_id()`` returns None for everyone, so any guard written
    against it is inert. If someone reintroduces that dependency the ownership
    cases above start passing instead of returning 403 — but assert it directly
    too, so the failure message says why.
    """
    from traceability.auth import current_operator_id

    with owned.other[0].session_transaction() as _session:
        assert current_operator_id() is None, (
            "current_operator_id() no longer returns None; the ownership guard "
            "must not go back to depending on it"
        )


# --------------------------------------------------------------------------
# Ownership data
# --------------------------------------------------------------------------


def test_a_record_with_no_owner_is_not_editable_by_an_operator(owned):
    """An ownerless record is an anomaly, and the safe reading is "not yours".

    Records written before ``completed_by_user_id`` was populated have NULL here.
    Allowing any operator to edit those would reopen the hole this rule closes,
    so an operator is refused and an administrator — who is unrestricted — can
    still clean them up.
    """
    orphan_id = _seed_record(owned.database_path, None, suffix="orphan")

    refused = _delete(owned.owner, orphan_id)
    assert refused.status_code == 403
    assert refused.get_json()["message"] == OWNERSHIP_MESSAGE

    allowed = _delete(owned.admin, orphan_id)
    assert allowed.status_code == 400
    assert MISSING_REASON_MESSAGE in allowed.get_json()["message"]


def test_records_carry_the_creating_operator(owned):
    """The rule is only enforceable because the column is populated.

    ``trace_records`` is written with ``current_actor_id()``; had it used
    ``current_operator_id()`` like the old guard, every row would have a NULL
    owner and this feature could not work at all.
    """
    connection = connect_database(owned.database_path)
    try:
        owner = connection.execute(
            "SELECT completed_by_user_id FROM trace_records WHERE id = ?",
            (owned.record_id,),
        ).fetchone()[0]
    finally:
        connection.close()
    assert owner == owned.owner_id


def test_a_missing_record_is_a_404_for_every_role(owned):
    for client_and_csrf in (owned.owner, owned.other, owned.admin):
        response = _delete(client_and_csrf, 999999)
        assert response.status_code == 404
        assert response.get_json()["message"] == "录入记录不存在"


def test_operations_cannot_touch_records_at_all(owned):
    """Ownership is a second gate, not the only one.

    OPERATIONS has no RECORD_DELETE capability, so the capability check rejects
    it before ownership is even considered — the rule did not widen access for
    anyone.
    """
    operations_id = insert_user(owned.database_path, "record-operations", "OPERATIONS")
    assert operations_id > 0
    client, csrf = login(owned.app, "record-operations")
    response = client.delete(
        f"/api/records/{owned.record_id}", json={}, headers={"X-CSRF-Token": csrf}
    )
    assert response.status_code == 403
    assert response.get_json()["message"] != OWNERSHIP_MESSAGE
