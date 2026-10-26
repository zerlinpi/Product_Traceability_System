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


# ---------------------------------------------------------------------------
# The commands that consume a backup, and the one that lists them
# ---------------------------------------------------------------------------


def _take_a_backup(database: Path, backup_dir: Path) -> str:
    """Back up through the CLI and return the name list-backups would print."""
    assert manage.main(
        ["--database", str(database), "--backup-dir", str(backup_dir), "backup"]
    ) == 0
    return sorted(path.name for path in backup_dir.glob("*.db"))[-1]


def test_verifying_a_backup_by_the_name_it_was_listed_under(migrated_database, tmp_path, capsys):
    """The runbook's next command has to work with what the last one printed.

    `list-backups` prints `path.name`, so the operator's next command is
    naturally `verify-backup traceability_20261023T153540.db`. That used to
    answer 「备份不可用：备份文件不存在」 — the worst thing it could say, because
    the file is sitting in the directory the command was just told about, and
    reading it as 「my backup is broken」 during an incident is how a good backup
    gets deleted.

    Found by running all thirteen commands against a copy of the real deployment
    database, the same way the guard above was found.
    """
    backup_dir = tmp_path / "backups"
    name = _take_a_backup(migrated_database, backup_dir)

    status = manage.main(
        ["--database", str(migrated_database), "--backup-dir", str(backup_dir),
         "verify-backup", name]
    )

    assert status == 0, capsys.readouterr().out
    assert "备份可用" in capsys.readouterr().out


def test_restoring_a_backup_by_the_name_it_was_listed_under(migrated_database, tmp_path, capsys):
    """Restore takes the same name, because it is the same runbook.

    This is the command an operator runs when something has already gone wrong,
    which is exactly when they are copying a name out of `list-backups` output
    rather than typing a path.
    """
    backup_dir = tmp_path / "backups"
    name = _take_a_backup(migrated_database, backup_dir)

    status = manage.main(
        ["--database", str(migrated_database), "--backup-dir", str(backup_dir),
         "restore", name, "--yes"]
    )

    assert status == 0, capsys.readouterr().out
    assert "恢复完成" in capsys.readouterr().out


def test_a_full_path_still_works_and_a_absent_name_is_reported_accurately(
    migrated_database, tmp_path, capsys
):
    """The resolution must not swallow the case where nothing exists anywhere.

    Accepting a name is only safe if a name that resolves nowhere still fails
    with the truth: the path is reported as given, so the operator sees what was
    looked for rather than a directory that happens to be configured.
    """
    backup_dir = tmp_path / "backups"
    # A backup that is present, so the fabricated name below has something real
    # to be confused with and still not match.
    _take_a_backup(migrated_database, backup_dir)

    # A full path is honoured as given, even when it is not in the directory the
    # command was told about — the manifest travels with the file, so a backup
    # kept elsewhere still verifies.
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    other_name = _take_a_backup(migrated_database, elsewhere)
    status = manage.main(
        ["--database", str(migrated_database), "--backup-dir", str(backup_dir),
         "verify-backup", str(elsewhere / other_name)]
    )
    assert status == 0, capsys.readouterr().out

    # And a name that is in neither place fails naming the name.
    status = manage.main(
        ["--database", str(migrated_database), "--backup-dir", str(backup_dir),
         "verify-backup", "traceability_19700101T000000.db"]
    )
    assert status == 1
    output = capsys.readouterr().out
    assert "备份文件不存在" in output
    assert "traceability_19700101T000000.db" in output, "应报出被查找的名字"
