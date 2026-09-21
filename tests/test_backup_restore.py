"""Backup, verification and restore.

The point of this suite: a backup nobody has restored is a hypothesis, not a
safety net. So it covers not just "the file gets written" but the properties an
operator depends on — the snapshot is consistent while the server runs, the
checksum catches a modified file, a restore is undoable, and an old build can
never silently downgrade a newer database.

Feature: batch-traceability, Property 71: 备份可验证与可恢复
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import manage  # noqa: E402
from capability_helpers import make_auth_app  # noqa: E402
from traceability import backup as backup_module  # noqa: E402
from traceability.backup import (  # noqa: E402
    BackupError,
    check_disk_space,
    create_backup,
    database_info,
    foreign_key_violations,
    integrity_check,
    list_backups,
    manifest_path_for,
    prune_backups,
    restore_backup,
    schema_version,
    sha256_file,
    table_counts,
    target_schema_version,
    verify_backup,
)

FIXED_NOW = "2026-07-22T10:30:00+08:00"


@pytest.fixture()
def live(tmp_path):
    """A migrated database with a little data in it."""
    _app, database_path, _fake = make_auth_app(tmp_path)
    database = sqlite3.connect(str(database_path))
    try:
        database.execute(
            "INSERT INTO suppliers(supplier_code, name, contact, phone, created_at, updated_at) "
            "VALUES ('SUP-BK', '备份测试供应商', '张三', '13800000000', 'now', 'now')"
        )
        database.commit()
    finally:
        database.close()
    return database_path


def _count_users(path) -> int:
    database = sqlite3.connect(str(path))
    try:
        return int(database.execute("SELECT COUNT(*) FROM users").fetchone()[0])
    finally:
        database.close()


# --------------------------------------------------------------------------
# 1. Creating a backup
# --------------------------------------------------------------------------


def test_backup_writes_the_file_and_a_manifest(tmp_path, live):
    report = create_backup(live, tmp_path / "bk", created_at=FIXED_NOW)
    assert report.path.exists()
    assert report.manifest_path.exists()
    assert report.manifest_path.name.endswith(backup_module.MANIFEST_SUFFIX)

    manifest = json.loads(report.manifest_path.read_text(encoding="utf-8"))
    assert manifest["sha256"] == report.sha256
    assert manifest["schema_version"] == target_schema_version()
    assert manifest["integrity"] == "ok"
    assert manifest["size_bytes"] == report.size_bytes


def test_backup_records_row_counts(tmp_path, live):
    report = create_backup(live, tmp_path / "bk", created_at=FIXED_NOW)
    assert report.table_counts["users"] >= 1
    assert report.table_counts["suppliers"] == 1


def test_backup_is_consistent_while_the_database_is_open(tmp_path, live):
    """The online backup API, not a file copy: a live -wal must not be missed."""
    connection = sqlite3.connect(str(live))
    try:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute(
            "INSERT INTO suppliers(supplier_code, name, contact, phone, created_at, updated_at) "
            "VALUES ('SUP-LIVE', '未落盘供应商', '李四', '13900000000', 'now', 'now')"
        )
        connection.commit()
        # The row is committed but may still live only in the -wal sidecar.
        report = create_backup(live, tmp_path / "bk", created_at=FIXED_NOW)
    finally:
        connection.close()

    database = sqlite3.connect(str(report.path))
    try:
        codes = {row[0] for row in database.execute("SELECT supplier_code FROM suppliers")}
    finally:
        database.close()
    assert "SUP-LIVE" in codes, "a committed row present only in -wal was lost"


def test_backup_refuses_a_missing_database(tmp_path):
    with pytest.raises(BackupError):
        create_backup(tmp_path / "nope.db", tmp_path / "bk", created_at=FIXED_NOW)


def test_backup_creates_the_destination_directory(tmp_path, live):
    nested = tmp_path / "a" / "b" / "c"
    report = create_backup(live, nested, created_at=FIXED_NOW)
    assert report.path.parent == nested


def test_backup_names_are_filesystem_safe(tmp_path, live):
    report = create_backup(live, tmp_path / "bk", created_at="2026-07-22T10:30:00+08:00")
    assert ":" not in report.path.name
    assert "+" not in report.path.name
    assert report.path.suffix == ".db"


def test_backups_in_the_same_second_do_not_overwrite_each_other(tmp_path, live):
    """A silently overwritten backup is the worst failure mode for this tool.

    Two runs inside the same second produce the same timestamp, so the filename
    has to be uniquified or the operator sees "备份完成" while one file is gone.
    """
    reports = [create_backup(live, tmp_path / "bk", created_at=FIXED_NOW) for _ in range(3)]
    paths = {report.path for report in reports}
    assert len(paths) == 3
    assert len(list_backups(tmp_path / "bk")) == 3
    for report in reports:
        assert report.path.exists()
        assert report.manifest_path.exists()


# --------------------------------------------------------------------------
# 2. Verification
# --------------------------------------------------------------------------


def test_verify_accepts_a_fresh_backup(tmp_path, live):
    report = create_backup(live, tmp_path / "bk", created_at=FIXED_NOW)
    verification = verify_backup(report.path)
    assert verification.ok, verification.problems
    assert verification.integrity == "ok"
    assert verification.schema_version == target_schema_version()


def test_verify_detects_a_modified_file(tmp_path, live):
    """The checksum is the whole point: it catches a corrupted or swapped file."""
    report = create_backup(live, tmp_path / "bk", created_at=FIXED_NOW)
    with open(report.path, "ab") as handle:
        handle.write(b"tampered")
    verification = verify_backup(report.path)
    assert not verification.ok
    assert any("SHA-256" in problem for problem in verification.problems)


def test_verify_flags_a_missing_manifest(tmp_path, live):
    report = create_backup(live, tmp_path / "bk", created_at=FIXED_NOW)
    report.manifest_path.unlink()
    verification = verify_backup(report.path)
    assert not verification.ok
    assert any("清单" in problem for problem in verification.problems)


def test_verify_rejects_a_non_sqlite_file(tmp_path):
    path = tmp_path / "not-a-database.db"
    path.write_bytes(b"this is plain text, not SQLite")
    verification = verify_backup(path)
    assert not verification.ok
    assert any("SQLite" in problem for problem in verification.problems)


def test_verify_rejects_an_empty_file(tmp_path):
    path = tmp_path / "empty.db"
    path.write_bytes(b"")
    verification = verify_backup(path)
    assert not verification.ok
    assert any("空" in problem for problem in verification.problems)


def test_verify_rejects_a_missing_file(tmp_path):
    verification = verify_backup(tmp_path / "absent.db")
    assert not verification.ok
    assert any("不存在" in problem for problem in verification.problems)


def test_verify_rejects_a_backup_from_a_newer_build(tmp_path, live):
    """An older binary must never be talked into downgrading newer data."""
    report = create_backup(live, tmp_path / "bk", created_at=FIXED_NOW)
    connection = sqlite3.connect(str(report.path))
    try:
        connection.execute("PRAGMA user_version = 999")
        connection.commit()
    finally:
        connection.close()

    verification = verify_backup(report.path)
    assert not verification.ok
    assert any("高于当前程序支持" in problem for problem in verification.problems)


def test_verify_reports_missing_required_tables(tmp_path, live):
    report = create_backup(live, tmp_path / "bk", created_at=FIXED_NOW)
    connection = sqlite3.connect(str(report.path))
    try:
        connection.execute("DROP TABLE audit_events")
        connection.commit()
    finally:
        connection.close()
    # Re-record the checksum so this test exercises the table check, not the
    # checksum check.
    manifest = json.loads(report.manifest_path.read_text(encoding="utf-8"))
    manifest["sha256"] = sha256_file(report.path)
    report.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    verification = verify_backup(report.path)
    assert not verification.ok
    assert any("audit_events" in problem for problem in verification.problems)


# --------------------------------------------------------------------------
# 3. Restore
# --------------------------------------------------------------------------


def test_restore_round_trip(tmp_path, live):
    report = create_backup(live, tmp_path / "bk", created_at=FIXED_NOW)
    baseline = _count_users(live)

    connection = sqlite3.connect(str(live))
    try:
        connection.execute(
            "INSERT INTO users(username, display_name, password_hash, role, active, "
            "must_change_password, created_at, updated_at) "
            "VALUES ('probe', '探针', 'x', 'WAREHOUSE', 1, 0, 'now', 'now')"
        )
        connection.commit()
    finally:
        connection.close()
    assert _count_users(live) == baseline + 1

    restore_backup(report.path, live, safety_dir=tmp_path / "bk", created_at=FIXED_NOW)
    assert _count_users(live) == baseline


def test_restore_keeps_a_safety_copy(tmp_path, live):
    report = create_backup(live, tmp_path / "bk", created_at=FIXED_NOW)
    baseline = _count_users(live)
    connection = sqlite3.connect(str(live))
    try:
        connection.execute(
            "INSERT INTO users(username, display_name, password_hash, role, active, "
            "must_change_password, created_at, updated_at) "
            "VALUES ('probe2', '探针2', 'x', 'WAREHOUSE', 1, 0, 'now', 'now')"
        )
        connection.commit()
    finally:
        connection.close()

    result = restore_backup(report.path, live, safety_dir=tmp_path / "bk", created_at=FIXED_NOW)
    assert result.safety_copy is not None
    assert result.safety_copy.exists()
    assert _count_users(result.safety_copy) == baseline + 1


def test_restore_can_skip_the_safety_copy(tmp_path, live):
    report = create_backup(live, tmp_path / "bk", created_at=FIXED_NOW)
    result = restore_backup(
        report.path, live, safety_dir=tmp_path / "bk", created_at=FIXED_NOW, keep_safety_copy=False
    )
    assert result.safety_copy is None


def test_restore_refuses_an_invalid_backup(tmp_path, live):
    report = create_backup(live, tmp_path / "bk", created_at=FIXED_NOW)
    with open(report.path, "ab") as handle:
        handle.write(b"corrupt")
    with pytest.raises(BackupError):
        restore_backup(report.path, live, created_at=FIXED_NOW)


def test_restore_refuses_a_newer_schema(tmp_path, live):
    report = create_backup(live, tmp_path / "bk", created_at=FIXED_NOW)
    connection = sqlite3.connect(str(report.path))
    try:
        connection.execute("PRAGMA user_version = 999")
        connection.commit()
    finally:
        connection.close()
    manifest = json.loads(report.manifest_path.read_text(encoding="utf-8"))
    manifest["sha256"] = sha256_file(report.path)
    report.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(BackupError):
        restore_backup(report.path, live, created_at=FIXED_NOW)


def test_restore_clears_stale_wal_and_shm_sidecars(tmp_path, live):
    """A stale -wal replayed onto a restored file would corrupt it.

    SQLite recreates empty sidecars as soon as the WAL-mode database is opened
    again, so the property that matters is that the *stale content* is gone, not
    that the files are absent.
    """
    report = create_backup(live, tmp_path / "bk", created_at=FIXED_NOW)
    marker = b"STALE-SIDECAR-MARKER"
    for suffix in ("-wal", "-shm"):
        Path(str(live) + suffix).write_bytes(marker * 64)

    restore_backup(report.path, live, created_at=FIXED_NOW, keep_safety_copy=False)

    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(live) + suffix)
        if sidecar.exists():
            assert marker not in sidecar.read_bytes(), f"{suffix} still holds stale content"
    assert integrity_check(live) == "ok"


def test_restore_leaves_no_temporary_file(tmp_path, live):
    report = create_backup(live, tmp_path / "bk", created_at=FIXED_NOW)
    restore_backup(report.path, live, created_at=FIXED_NOW, keep_safety_copy=False)
    leftovers = [path for path in live.parent.iterdir() if "restore-tmp" in path.name]
    assert leftovers == []


def test_restore_into_a_fresh_location(tmp_path, live):
    report = create_backup(live, tmp_path / "bk", created_at=FIXED_NOW)
    destination = tmp_path / "fresh" / "traceability.db"
    result = restore_backup(report.path, destination, created_at=FIXED_NOW)
    assert destination.exists()
    assert result.schema_version == target_schema_version()
    assert integrity_check(destination) == "ok"


def test_restore_verifies_the_result(tmp_path, live):
    report = create_backup(live, tmp_path / "bk", created_at=FIXED_NOW)
    result = restore_backup(report.path, live, created_at=FIXED_NOW, keep_safety_copy=False)
    assert result.sha256 == sha256_file(live)
    assert integrity_check(live) == "ok"
    assert foreign_key_violations(live) == []


# --------------------------------------------------------------------------
# 4. Retention and inspection
# --------------------------------------------------------------------------


def test_list_backups_is_sorted_oldest_first(tmp_path, live):
    for stamp in ("2026-07-20T10:00:00+08:00", "2026-07-22T10:00:00+08:00", "2026-07-21T10:00:00+08:00"):
        create_backup(live, tmp_path / "bk", created_at=stamp)
    names = [path.name for path in list_backups(tmp_path / "bk")]
    assert names == sorted(names)
    assert len(names) == 3


def test_prune_keeps_the_newest_n(tmp_path, live):
    for day in range(1, 6):
        create_backup(live, tmp_path / "bk", created_at=f"2026-07-2{day}T10:00:00+08:00")
    removed = prune_backups(tmp_path / "bk", keep=2)
    assert len(removed) == 3
    remaining = list_backups(tmp_path / "bk")
    assert len(remaining) == 2
    # The newest two survive: the 24th and the 25th.
    assert "20260724" in remaining[0].name
    assert "20260725" in remaining[-1].name


def test_prune_removes_the_manifests_too(tmp_path, live):
    for day in range(1, 4):
        create_backup(live, tmp_path / "bk", created_at=f"2026-07-2{day}T10:00:00+08:00")
    removed = prune_backups(tmp_path / "bk", keep=1)
    for path in removed:
        assert not manifest_path_for(path).exists()


def test_prune_with_keep_zero_removes_everything(tmp_path, live):
    for day in range(1, 4):
        create_backup(live, tmp_path / "bk", created_at=f"2026-07-2{day}T10:00:00+08:00")
    prune_backups(tmp_path / "bk", keep=0)
    assert list_backups(tmp_path / "bk") == []


def test_prune_on_an_empty_directory_is_a_no_op(tmp_path):
    assert prune_backups(tmp_path / "missing", keep=3) == []


def test_prune_rejects_a_negative_keep(tmp_path, live):
    create_backup(live, tmp_path / "bk", created_at=FIXED_NOW)
    with pytest.raises(BackupError):
        prune_backups(tmp_path / "bk", keep=-1)


def test_check_disk_space_raises_when_the_request_is_absurd(tmp_path):
    with pytest.raises(BackupError):
        check_disk_space(tmp_path, required_bytes=10**18)


def test_database_info_reports_the_essentials(tmp_path, live):
    info = database_info(live)
    assert info["exists"] is True
    assert info["schema_version"] == target_schema_version()
    assert info["integrity"] == "ok"
    assert info["foreign_key_violations"] == 0
    assert info["table_counts"]["suppliers"] == 1


def test_database_info_on_a_missing_file(tmp_path):
    assert database_info(tmp_path / "nope.db")["exists"] is False


def test_sha256_file_matches_hashlib(tmp_path):
    path = tmp_path / "sample.bin"
    payload = b"traceability" * 5000
    path.write_bytes(payload)
    assert sha256_file(path) == hashlib.sha256(payload).hexdigest()


def test_integrity_check_detects_corruption(tmp_path, live):
    path = tmp_path / "broken.db"
    path.write_bytes(live.read_bytes())
    assert integrity_check(path) == "ok"
    # Zero out a slab of pages in the middle of the file.
    raw = bytearray(path.read_bytes())
    start = len(raw) // 3
    raw[start : start + 4096] = b"\x00" * 4096
    path.write_bytes(bytes(raw))
    assert integrity_check(path) != "ok"


def test_table_counts_skips_tables_that_are_absent(tmp_path, live):
    counts = table_counts(live, tables=("users", "definitely_not_a_table"))
    assert "users" in counts
    assert "definitely_not_a_table" not in counts


# --------------------------------------------------------------------------
# 5. Schema version bookkeeping
# --------------------------------------------------------------------------


def test_target_schema_version_matches_the_migration_chain():
    """SCHEMA_VERSION must equal the highest migration in db.py.

    If someone adds a migration and forgets the constant, ``preflight`` and the
    restore downgrade guard would silently use the wrong target.
    """
    source = (ROOT / "traceability" / "db.py").read_text(encoding="utf-8")
    versions = [int(value) for value in re.findall(r"PRAGMA user_version = (\d+)", source)]
    assert versions, "no migration versions found"
    assert target_schema_version() == max(versions), (
        f"SCHEMA_VERSION is {target_schema_version()} but the migration chain reaches {max(versions)}"
    )


def test_schema_version_reads_the_live_database(tmp_path, live):
    assert schema_version(live) == target_schema_version()


# --------------------------------------------------------------------------
# 6. Command line
# --------------------------------------------------------------------------


def test_cli_backup_then_verify(tmp_path, live, capsys):
    code = manage.main(
        ["--database", str(live), "--backup-dir", str(tmp_path / "bk"), "backup"]
    )
    assert code == 0
    output = capsys.readouterr().out
    assert "备份完成" in output

    created = list_backups(tmp_path / "bk")[0]
    assert manage.main(["verify-backup", str(created)]) == 0


def test_cli_verify_backup_fails_on_a_bad_file(tmp_path, capsys):
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"not a database")
    assert manage.main(["verify-backup", str(bad)]) == 1
    assert "不可用" in capsys.readouterr().out


def test_cli_restore_requires_confirmation(tmp_path, live, capsys):
    manage.main(["--database", str(live), "--backup-dir", str(tmp_path / "bk"), "backup"])
    created = list_backups(tmp_path / "bk")[0]
    code = manage.main(["--database", str(live), "--backup-dir", str(tmp_path / "bk"), "restore", str(created)])
    assert code == 2
    assert "--yes" in capsys.readouterr().out


def test_cli_restore_with_confirmation(tmp_path, live, capsys):
    manage.main(["--database", str(live), "--backup-dir", str(tmp_path / "bk"), "backup"])
    created = list_backups(tmp_path / "bk")[0]
    code = manage.main(
        ["--database", str(live), "--backup-dir", str(tmp_path / "bk"), "restore", str(created), "--yes"]
    )
    assert code == 0
    assert "恢复完成" in capsys.readouterr().out


def test_cli_integrity_check(tmp_path, live, capsys):
    assert manage.main(["--database", str(live), "integrity-check"]) == 0
    assert "通过" in capsys.readouterr().out


def test_cli_db_info(tmp_path, live, capsys):
    assert manage.main(["--database", str(live), "db-info"]) == 0
    assert "结构版本" in capsys.readouterr().out


def test_cli_db_info_on_a_missing_database(tmp_path, capsys):
    assert manage.main(["--database", str(tmp_path / "nope.db"), "db-info"]) == 1


def test_cli_prune_backups(tmp_path, live, capsys):
    for day in range(1, 5):
        create_backup(live, tmp_path / "bk", created_at=f"2026-07-2{day}T10:00:00+08:00")
    code = manage.main(["--backup-dir", str(tmp_path / "bk"), "prune-backups", "--keep", "1"])
    assert code == 0
    assert len(list_backups(tmp_path / "bk")) == 1


def test_cli_preflight_backs_up_before_upgrading(tmp_path, live, capsys):
    code = manage.main(
        ["--database", str(live), "--backup-dir", str(tmp_path / "bk"), "preflight"]
    )
    assert code == 0
    output = capsys.readouterr().out
    assert "检查通过" in output
    assert len(list_backups(tmp_path / "bk")) == 1, "preflight must leave a backup behind"


def test_cli_preflight_refuses_a_newer_database(tmp_path, live, capsys):
    connection = sqlite3.connect(str(live))
    try:
        connection.execute("PRAGMA user_version = 999")
        connection.commit()
    finally:
        connection.close()
    code = manage.main(
        ["--database", str(live), "--backup-dir", str(tmp_path / "bk"), "preflight"]
    )
    assert code == 1
    assert "高于程序支持" in capsys.readouterr().out


def test_cli_postflight_on_a_healthy_database(tmp_path, live, capsys):
    assert manage.main(["--database", str(live), "postflight"]) == 0
    assert "升级完成" in capsys.readouterr().out


def test_cli_postflight_flags_a_version_mismatch(tmp_path, live, capsys):
    connection = sqlite3.connect(str(live))
    try:
        connection.execute("PRAGMA user_version = 19")
        connection.commit()
    finally:
        connection.close()
    assert manage.main(["--database", str(live), "postflight"]) == 1
    assert "不一致" in capsys.readouterr().out


def test_cli_postflight_flags_an_accountless_database(tmp_path, live, capsys):
    connection = sqlite3.connect(str(live))
    try:
        connection.execute("DELETE FROM users")
        connection.commit()
    finally:
        connection.close()
    assert manage.main(["--database", str(live), "postflight"]) == 1
    assert "账号" in capsys.readouterr().out


def test_cli_backup_with_retention(tmp_path, live, capsys):
    for day in range(1, 5):
        manage.main(
            [
                "--database",
                str(live),
                "--backup-dir",
                str(tmp_path / "bk"),
                "backup",
                "--keep",
                "2",
            ]
        )
    assert len(list_backups(tmp_path / "bk")) == 2
