"""Maintenance commands must explain a schema gap instead of crashing on it.

Found by following the runbook: `python manage.py audit-verify` on a database
older than the code dies with `sqlite3.OperationalError: no such column:
event_hash`, and `login-status` with `no such table: login_attempts`. Both are
true statements about the database and useless statements to an operator.

Worth fixing rather than documenting around, because a database older than the
code is the *normal* state during an upgrade — `docs/UPGRADE.md` has you inspect
the database before migrating it. So the commands most likely to be run at that
moment were the ones that failed least legibly.

Three commands read columns a migration introduced and now refuse with both
version numbers. The rest work on either schema and are deliberately not
guarded: `preflight` and `postflight` already handle the gap properly, and
`backup`, `restore`, `integrity-check`, `db-info` and the login commands operate
on the database as it is.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

import manage  # noqa: E402
from capability_helpers import make_auth_app  # noqa: E402


@pytest.fixture
def migrated_database(tmp_path):
    """A database at the current schema, built by the app's own migrations."""
    make_auth_app(tmp_path)
    return tmp_path / "traceability.db"


@pytest.fixture
def stale_database(tmp_path):
    """A database that predates the migrations these commands depend on."""
    path = tmp_path / "stale.db"
    connection = sqlite3.connect(str(path))
    connection.execute("PRAGMA user_version = 18")
    connection.execute("CREATE TABLE audit_events (id INTEGER PRIMARY KEY, event_type TEXT)")
    connection.commit()
    connection.close()
    return path


# ---------------------------------------------------------------------------
# The commands that need the current schema
# ---------------------------------------------------------------------------


def test_the_guarded_set_is_the_one_that_actually_reads_new_columns():
    """Pinned so a new command can be considered rather than silently inherited."""
    assert set(manage.REQUIRES_CURRENT_SCHEMA) == {
        "audit-verify",
        "audit-info",
        "login-status",
    }


@pytest.mark.parametrize("command", ["audit-verify", "audit-info", "login-status"])
def test_a_stale_database_is_refused_clearly(command, stale_database, capsys):
    status = manage.main(["--database", str(stale_database), command])

    assert status == 1
    output = capsys.readouterr().out
    assert "18" in output, "应报出数据库的实际版本"
    assert str(manage.SCHEMA_VERSION) in output, "应报出代码期望的版本"
    assert "UPGRADE.md" in output, "应指向升级步骤"
    assert "Traceback" not in output


@pytest.mark.parametrize("command", ["audit-verify", "audit-info", "login-status"])
def test_a_current_database_is_not_refused(command, migrated_database, capsys):
    """The guard must not get in the way of the normal case."""
    status = manage.main(["--database", str(migrated_database), command])

    assert status == 0
    output = capsys.readouterr().out
    assert "本命令需要" not in output


def test_a_missing_database_is_left_to_the_command(tmp_path, capsys):
    """Each command already reports a missing database with its path."""
    missing = tmp_path / "not-there.db"

    status = manage.main(["--database", str(missing), "audit-verify"])

    assert status == 1
    output = capsys.readouterr().out
    assert "数据库不存在" in output, f"应报告数据库不存在: {output!r}"


# ---------------------------------------------------------------------------
# The commands that work on either schema
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    ["integrity-check", "db-info", "list-backups", "preflight", "postflight"],
)
def test_unguarded_commands_still_run_on_a_stale_database(command, stale_database, capsys):
    """They operate on the database as it is, so a version gap is not their business.

    `postflight` in particular is *supposed* to notice the gap and say so — it
    returns non-zero, which is correct, and the point here is that it does so
    through its own reporting rather than being cut off by the guard.
    """
    manage.main(["--database", str(stale_database), command])

    output = capsys.readouterr().out
    assert "本命令需要" not in output, f"{command} 不该被守卫拦下"
    assert "Traceback" not in output, f"{command} 在旧库上不应崩"
