"""Login throttling, password policy and session invalidation.

Without a throttle the login endpoint is an unlimited oracle: an attacker can
try passwords as fast as the LAN allows. On a factory LAN the realistic threat
is a curious insider with a browser, so the bar is "make guessing impractical and
leave a trace" — not "resist a nation state".

Feature: batch-traceability, Property 74: 登录限流与密码策略
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import manage  # noqa: E402
from capability_helpers import (  # noqa: E402
    PASSWORD,
    bootstrap_admin,
    insert_user,
    login,
    make_auth_app,
)
from traceability import login_guard, passwords  # noqa: E402
from traceability.db import connect_database  # noqa: E402
from traceability.login_guard import LoginPolicy  # noqa: E402

DEFAULT_PASSWORD = "Admin@12345"
WRONG = "WrongPass@1"


@pytest.fixture()
def app_and_path(tmp_path):
    app, database_path, _fake = make_auth_app(tmp_path)
    return app, database_path


def _login(client, username="admin", password=DEFAULT_PASSWORD, ip=None):
    extra = {"environ_base": {"REMOTE_ADDR": ip}} if ip else {}
    return client.post(
        "/api/auth/login", json={"username": username, "password": password}, **extra
    )


def _fail_n_times(client, times, username="admin", ip=None):
    for _ in range(times):
        _login(client, username=username, password=WRONG, ip=ip)


# --------------------------------------------------------------------------
# 1. Throttling
# --------------------------------------------------------------------------


def test_failures_below_the_threshold_still_answer_401(app_and_path):
    app, _path = app_and_path
    client = app.test_client()
    for _ in range(LoginPolicy.max_failures_per_username - 1):
        assert _login(client, password=WRONG).status_code == 401


def test_the_next_attempt_after_the_threshold_is_throttled(app_and_path):
    app, _path = app_and_path
    client = app.test_client()
    _fail_n_times(client, LoginPolicy.max_failures_per_username)
    response = _login(client, password=WRONG)
    assert response.status_code == 429
    assert "过于频繁" in response.get_json()["message"]


def test_the_correct_password_is_also_refused_while_locked(app_and_path):
    """A lockout that lets the right password through is not a lockout."""
    app, _path = app_and_path
    client = app.test_client()
    _fail_n_times(client, LoginPolicy.max_failures_per_username)
    assert _login(client, password=DEFAULT_PASSWORD).status_code == 429


def test_the_lockout_message_states_a_retry_delay(app_and_path):
    app, _path = app_and_path
    client = app.test_client()
    _fail_n_times(client, LoginPolicy.max_failures_per_username)
    message = _login(client, password=WRONG).get_json()["message"]
    assert "分钟" in message


def test_the_lockout_is_per_username(app_and_path):
    """Locking one account must not lock the whole plant out."""
    app, database_path = app_and_path
    insert_user(database_path, "lock-other", "WAREHOUSE")
    client = app.test_client()
    _fail_n_times(client, LoginPolicy.max_failures_per_username, username="admin")

    assert _login(client, username="admin", password=WRONG).status_code == 429
    # A different account from the same client is unaffected.
    assert _login(client, username="lock-other", password=PASSWORD).status_code == 200


def test_usernames_are_throttled_case_insensitively(app_and_path):
    app, _path = app_and_path
    client = app.test_client()
    for index in range(LoginPolicy.max_failures_per_username):
        _login(client, username="ADMIN" if index % 2 else "admin", password=WRONG)
    assert _login(client, username="Admin", password=WRONG).status_code == 429


def test_the_per_ip_budget_locks_the_source(app_and_path):
    """Guessing many accounts from one workstation is the other half of the
    problem."""
    app, _path = app_and_path
    client = app.test_client()
    ip = "10.20.30.40"
    for index in range(LoginPolicy.max_failures_per_ip):
        _login(client, username=f"ghost-{index}", password=WRONG, ip=ip)
    response = _login(client, username="ghost-final", password=WRONG, ip=ip)
    assert response.status_code == 429


def test_a_different_source_is_not_affected_by_another_ips_budget(app_and_path):
    app, _path = app_and_path
    client = app.test_client()
    for index in range(LoginPolicy.max_failures_per_ip):
        _login(client, username=f"ghost-{index}", password=WRONG, ip="10.0.0.9")
    assert _login(client, username="admin", password=DEFAULT_PASSWORD, ip="10.0.0.10").status_code == 200


def test_a_successful_login_clears_the_usernames_failures(app_and_path):
    app, _path = app_and_path
    client = app.test_client()
    _fail_n_times(client, LoginPolicy.max_failures_per_username - 1)
    assert _login(client, password=DEFAULT_PASSWORD).status_code == 200
    # The budget is back to zero, so a fresh run of failures is allowed.
    _fail_n_times(client, LoginPolicy.max_failures_per_username - 1)
    assert _login(client, password=WRONG).status_code == 401


def test_a_successful_login_does_not_clear_the_ip_budget(app_and_path):
    """Otherwise one valid low-privilege account resets the per-IP budget and an
    attacker keeps guessing from the same workstation."""
    app, database_path = app_and_path
    insert_user(database_path, "reset-probe", "WAREHOUSE")
    client = app.test_client()
    ip = "10.0.0.55"
    for index in range(LoginPolicy.max_failures_per_ip - 1):
        _login(client, username=f"ghost-{index}", password=WRONG, ip=ip)

    assert _login(client, username="reset-probe", password=PASSWORD, ip=ip).status_code == 200

    connection = connect_database(database_path)
    try:
        remaining = connection.execute(
            "SELECT COUNT(*) FROM login_attempts WHERE scope = ? AND subject = ? AND succeeded = 0",
            (login_guard.SCOPE_IP, ip),
        ).fetchone()[0]
    finally:
        connection.close()
    assert remaining >= LoginPolicy.max_failures_per_ip - 1


def test_the_lockout_expires_once_the_window_slides(app_and_path):
    app, database_path = app_and_path
    client = app.test_client()
    _fail_n_times(client, LoginPolicy.max_failures_per_username)
    assert _login(client, password=WRONG).status_code == 429

    # Age the recorded failures out of the window.
    connection = connect_database(database_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("UPDATE login_attempts SET attempted_epoch = 0")
        connection.execute("COMMIT")
    finally:
        connection.close()

    assert _login(client, password=DEFAULT_PASSWORD).status_code == 200


def test_an_existing_and_a_missing_account_are_indistinguishable(app_and_path):
    """The throttle must not become an account-enumeration oracle."""
    app, _path = app_and_path
    client = app.test_client()
    existing = _login(client, username="admin", password=WRONG)
    missing = _login(client, username="no-such-user", password=WRONG)
    assert existing.status_code == missing.status_code
    assert existing.get_json() == missing.get_json()


def test_a_throttled_attempt_is_also_indistinguishable(app_and_path):
    app, _path = app_and_path
    client = app.test_client()
    for _ in range(LoginPolicy.max_failures_per_username):
        _login(client, username="admin", password=WRONG)
    locked_existing = _login(client, username="admin", password=WRONG)

    other = app.test_client()
    for _ in range(LoginPolicy.max_failures_per_username):
        _login(other, username="no-such-user", password=WRONG)
    locked_missing = _login(other, username="no-such-user", password=WRONG)

    assert locked_existing.status_code == locked_missing.status_code == 429
    assert locked_existing.get_json() == locked_missing.get_json()


# --------------------------------------------------------------------------
# 2. Audit trail
# --------------------------------------------------------------------------


def test_a_failed_login_is_audited(app_and_path):
    app, database_path = app_and_path
    client = app.test_client()
    _login(client, password=WRONG)

    connection = connect_database(database_path)
    try:
        types = {
            row[0]
            for row in connection.execute("SELECT event_type FROM audit_events").fetchall()
        }
    finally:
        connection.close()
    assert "USER_LOGIN_FAILED" in types


def test_a_blocked_attempt_is_audited(app_and_path):
    app, database_path = app_and_path
    client = app.test_client()
    _fail_n_times(client, LoginPolicy.max_failures_per_username)
    _login(client, password=WRONG)

    connection = connect_database(database_path)
    try:
        row = connection.execute(
            "SELECT payload_json FROM audit_events WHERE event_type = 'USER_LOGIN_BLOCKED' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        connection.close()
    assert row is not None
    assert "retryAfterSeconds" in row["payload_json"]


def test_login_audit_events_stay_inside_the_hash_chain(app_and_path):
    """The throttle writes to the same append-only ledger."""
    from traceability.audit_chain import verify_chain

    app, database_path = app_and_path
    client = app.test_client()
    _fail_n_times(client, 3)
    bootstrap_admin(app)

    connection = connect_database(database_path)
    try:
        report = verify_chain(connection)
    finally:
        connection.close()
    assert report.ok, report.first_broken_reason
    assert report.unhashed == 0


def test_attempts_are_pruned_to_the_window(app_and_path):
    app, database_path = app_and_path
    client = app.test_client()
    _login(client, password=WRONG)

    connection = connect_database(database_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("UPDATE login_attempts SET attempted_epoch = 0")
        connection.execute("COMMIT")
        removed = login_guard.prune_attempts(connection, now_epoch=10**9)
    finally:
        connection.close()
    assert removed >= 1


# --------------------------------------------------------------------------
# 3. Policy configuration
# --------------------------------------------------------------------------


def test_the_policy_defaults_are_sane():
    policy = LoginPolicy()
    assert policy.max_failures_per_username == 5
    assert policy.max_failures_per_ip == 20
    assert policy.window_seconds == 15 * 60
    assert policy.lockout_seconds == 15 * 60


def test_the_policy_can_be_overridden_from_the_environment():
    policy = LoginPolicy.from_environment(
        {
            "PTS_LOGIN_WINDOW_SECONDS": "60",
            "PTS_LOGIN_LOCKOUT_SECONDS": "120",
            "PTS_LOGIN_MAX_FAILURES_PER_USERNAME": "2",
            "PTS_LOGIN_MAX_FAILURES_PER_IP": "3",
        }
    )
    assert policy.window_seconds == 60
    assert policy.lockout_seconds == 120
    assert policy.max_failures_per_username == 2
    assert policy.max_failures_per_ip == 3


@pytest.mark.parametrize("value", ["", "abc", "0", "-5"])
def test_unusable_policy_values_keep_the_default(value):
    policy = LoginPolicy.from_environment({"PTS_LOGIN_MAX_FAILURES_PER_USERNAME": value})
    assert policy.max_failures_per_username == 5


def test_to_epoch_handles_offsets_and_garbage():
    assert login_guard.to_epoch("2026-07-22T10:30:00+08:00") == 1784687400
    assert login_guard.to_epoch("") == 0
    assert login_guard.to_epoch("not a date") == 0
    assert login_guard.to_epoch(None) == 0


# --------------------------------------------------------------------------
# 4. Password policy
# --------------------------------------------------------------------------


@pytest.mark.parametrize("password", ["short", "1234567", "x" * 129])
def test_password_length_is_enforced(password):
    assert passwords.password_policy_error(password) is not None


def test_a_reasonable_password_is_accepted():
    assert passwords.password_policy_error("Treadmill#2026") is None


def test_the_shipped_default_is_refused():
    assert passwords.password_policy_error(DEFAULT_PASSWORD) is not None
    assert passwords.password_policy_error(DEFAULT_PASSWORD.lower()) is not None


@pytest.mark.parametrize("password", ["password", "12345678", "qwerty123", "changeme", "admin1234"])
def test_common_passwords_are_refused(password):
    assert passwords.password_policy_error(password) is not None


def test_a_password_equal_to_the_username_is_refused():
    assert passwords.password_policy_error("warehouse1", username="warehouse1") is not None


def test_a_password_merely_containing_the_username_is_accepted():
    """A "must not contain the username" rule was tried and removed: with short
    account names it rejected ordinary passwords."""
    assert passwords.password_policy_error("Admin@98765", username="admin") is None


@pytest.mark.parametrize("password", ["aaaaaaaa", "11111111", "abcdefgh", "98765432", "qwertyui"])
def test_trivial_patterns_are_refused(password):
    assert passwords.password_policy_error(password) is not None


def test_the_policy_is_applied_when_changing_a_password(app_and_path):
    app, _path = app_and_path
    client = app.test_client()
    first = _login(client)
    csrf = first.get_json()["data"]["csrfToken"]
    response = client.post(
        "/api/auth/change-password",
        json={"currentPassword": DEFAULT_PASSWORD, "newPassword": "password"},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 400
    assert "常见" in response.get_json()["message"]


def test_the_default_cannot_be_re_set_as_the_new_password(app_and_path):
    app, _path = app_and_path
    client = app.test_client()
    first = _login(client)
    csrf = first.get_json()["data"]["csrfToken"]
    response = client.post(
        "/api/auth/change-password",
        json={"currentPassword": DEFAULT_PASSWORD, "newPassword": DEFAULT_PASSWORD},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 400


def test_the_policy_is_applied_when_an_admin_creates_a_user(tmp_path):
    app, _path, _fake = make_auth_app(tmp_path)
    admin, csrf = bootstrap_admin(app)
    response = admin.post(
        "/api/users",
        json={
            "username": "weak-probe",
            "displayName": "弱密码探针",
            "password": "password",
            "role": "WAREHOUSE",
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 400
    assert "常见" in response.get_json()["message"]


def test_the_policy_is_applied_when_an_admin_resets_a_password(tmp_path):
    app, database_path, _fake = make_auth_app(tmp_path)
    admin, csrf = bootstrap_admin(app)
    user_id = insert_user(database_path, "reset-target", "WAREHOUSE")
    response = admin.put(
        f"/api/users/{user_id}",
        json={"password": "password"},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 400


def test_an_admin_created_user_must_change_their_password(tmp_path):
    app, _path, _fake = make_auth_app(tmp_path)
    admin, csrf = bootstrap_admin(app)
    admin.post(
        "/api/users",
        json={
            "username": "fresh-user",
            "displayName": "新账号",
            "password": PASSWORD,
            "role": "WAREHOUSE",
        },
        headers={"X-CSRF-Token": csrf},
    )
    client, _csrf = login(app, "fresh-user")
    response = client.get("/api/product-models")
    assert response.status_code == 428


# --------------------------------------------------------------------------
# 5. Session invalidation
# --------------------------------------------------------------------------


def test_changing_a_password_invalidates_other_sessions(tmp_path):
    app, _path, _fake = make_auth_app(tmp_path)
    first_client, first_csrf = bootstrap_admin(app)
    # bootstrap_admin already changed the password once; the second client
    # represents another device signed in with the current password.
    second_client, _second_csrf = login(app, "admin")
    assert second_client.get("/api/product-models").status_code == 200

    changed = first_client.post(
        "/api/auth/change-password",
        json={"currentPassword": PASSWORD, "newPassword": "Treadmill#2026"},
        headers={"X-CSRF-Token": first_csrf},
    )
    assert changed.status_code == 200

    assert second_client.get("/api/product-models").status_code == 401
    # The session that performed the change keeps working.
    assert first_client.get("/api/product-models").status_code == 200


def test_an_admin_reset_invalidates_the_targets_sessions(tmp_path):
    app, database_path, _fake = make_auth_app(tmp_path)
    admin, admin_csrf = bootstrap_admin(app)
    user_id = insert_user(database_path, "reset-victim", "WAREHOUSE")
    victim, _victim_csrf = login(app, "reset-victim")
    assert victim.get("/api/product-models").status_code == 200

    reset = admin.put(
        f"/api/users/{user_id}",
        json={"password": "Treadmill#2026"},
        headers={"X-CSRF-Token": admin_csrf},
    )
    assert reset.status_code == 200
    assert victim.get("/api/product-models").status_code == 401


def test_a_role_change_invalidates_sessions(tmp_path):
    app, database_path, _fake = make_auth_app(tmp_path)
    admin, admin_csrf = bootstrap_admin(app)
    user_id = insert_user(database_path, "role-victim", "WAREHOUSE")
    victim, _csrf = login(app, "role-victim")

    changed = admin.put(
        f"/api/users/{user_id}",
        json={"role": "OPERATIONS"},
        headers={"X-CSRF-Token": admin_csrf},
    )
    assert changed.status_code == 200
    assert victim.get("/api/product-models").status_code == 401


def test_logging_in_as_another_user_replaces_the_session(tmp_path):
    """``session.clear()`` on login, so a pre-login session cannot be reused."""
    app, database_path, _fake = make_auth_app(tmp_path)
    bootstrap_admin(app)
    insert_user(database_path, "swap-user", "WAREHOUSE")

    client = app.test_client()
    first = client.post(
        "/api/auth/login", json={"username": "admin", "password": PASSWORD}
    )
    assert first.status_code == 200
    assert first.get_json()["data"]["role"] == "ADMIN"

    second = client.post(
        "/api/auth/login", json={"username": "swap-user", "password": PASSWORD}
    )
    assert second.status_code == 200
    assert second.get_json()["data"]["role"] == "WAREHOUSE"
    assert client.get("/api/auth/me").get_json()["data"]["username"] == "swap-user"


def test_login_issues_a_fresh_csrf_token(tmp_path):
    app, _path, _fake = make_auth_app(tmp_path)
    bootstrap_admin(app)
    client = app.test_client()
    first = client.post("/api/auth/login", json={"username": "admin", "password": PASSWORD})
    second = client.post("/api/auth/login", json={"username": "admin", "password": PASSWORD})
    assert first.get_json()["data"]["csrfToken"] != second.get_json()["data"]["csrfToken"]


# --------------------------------------------------------------------------
# 6. Command line
# --------------------------------------------------------------------------


def test_cli_login_status_reports_the_policy(tmp_path, capsys):
    app, database_path, _fake = make_auth_app(tmp_path)
    bootstrap_admin(app)
    assert manage.main(["--database", str(database_path), "login-status"]) == 0
    output = capsys.readouterr().out
    assert "登录失败计数" in output
    assert "账号阈值" in output


def test_cli_login_unlock_clears_a_budget(tmp_path, capsys):
    app, database_path, _fake = make_auth_app(tmp_path)
    client = app.test_client()
    _fail_n_times(client, LoginPolicy.max_failures_per_username)
    assert _login(client, password=WRONG).status_code == 429

    assert (
        manage.main(["--database", str(database_path), "login-unlock", "--username", "admin"]) == 0
    )
    assert "已清除" in capsys.readouterr().out
    # The account is usable again.
    assert _login(client, password=DEFAULT_PASSWORD).status_code == 200


def test_cli_login_unlock_requires_a_target(tmp_path, capsys):
    app, database_path, _fake = make_auth_app(tmp_path)
    bootstrap_admin(app)
    assert manage.main(["--database", str(database_path), "login-unlock"]) == 2
    assert "--username" in capsys.readouterr().out


def test_cli_login_unlock_on_an_untouched_account(tmp_path, capsys):
    app, database_path, _fake = make_auth_app(tmp_path)
    bootstrap_admin(app)
    assert (
        manage.main(["--database", str(database_path), "login-unlock", "--username", "nobody"]) == 0
    )
    assert "没有需要清除" in capsys.readouterr().out


# --------------------------------------------------------------------------
# 7. Migration
# --------------------------------------------------------------------------


def test_migration_v22_creates_the_attempt_table(app_and_path):
    _app, database_path = app_and_path
    connection = connect_database(database_path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 22
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(login_attempts)").fetchall()
        }
        assert columns == {
            "id",
            "scope",
            "subject",
            "succeeded",
            "attempted_at",
            "attempted_epoch",
            "username",
            "ip",
        }
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()


def test_the_scope_column_is_constrained(app_and_path):
    _app, database_path = app_and_path
    connection = connect_database(database_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO login_attempts(scope, subject, attempted_at, attempted_epoch) "
                "VALUES ('nonsense', 'x', 'now', 0)"
            )
    finally:
        connection.close()
