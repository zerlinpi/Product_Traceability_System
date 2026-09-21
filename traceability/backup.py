"""Backup, verification and restore for the SQLite database.

Why this is more than ``cp traceability.db``
--------------------------------------------
A file copy of a live SQLite database can capture a torn write: the ``-wal``
sidecar may hold committed pages that the main file does not. The SQLite online
backup API (used here) takes a consistent snapshot even while the server runs.

Why verification matters as much as backup
------------------------------------------
A backup nobody has ever restored is a hypothesis, not a safety net. Every
backup this module writes gets an integrity check and a SHA-256 recorded in a
sidecar manifest, so a later restore can prove the file it is about to install
is exactly the file that was taken — and ``verify-backup`` can be run on a
schedule, or on a copy kept on another machine, without touching the live data.

Restore safety
--------------
``restore_backup`` refuses to be a footgun:

1. it verifies the source file first (integrity, checksum, required tables);
2. it refuses to install a database whose schema is **newer** than this build
   understands, so an old binary can never silently downgrade production data;
3. it copies the current database aside first, so a wrong restore is undoable;
4. it writes through a temporary file and an atomic replace, then clears the
   ``-wal``/``-shm`` sidecars, so an interrupted restore cannot leave a hybrid.

The module deliberately does not import ``traceability.db`` at module scope, so
it stays usable from maintenance scripts and tests without pulling Flask in.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from dataclasses import asdict, dataclass, field
from pathlib import Path

MANIFEST_SUFFIX = ".manifest.json"
DEFAULT_BACKUP_PREFIX = "traceability"

# Tables whose row counts are recorded in the manifest. A restore drill can
# compare these against the restored database without knowing the full schema.
COUNTED_TABLES = (
    "users",
    "product_models",
    "suppliers",
    "production_batches",
    "batch_trace_records",
    "purchase_orders",
    "inbound_receipts",
    "production_orders",
    "inbound_scan_records",
    "product_stock",
    "audit_events",
)

# Tables a database must have to be considered restorable at all.
REQUIRED_TABLES = ("users", "audit_events", "app_settings")


class BackupError(Exception):
    """A backup or restore could not be completed safely."""


@dataclass
class BackupReport:
    path: Path
    manifest_path: Path
    created_at: str
    schema_version: int
    sha256: str
    size_bytes: int
    integrity: str
    table_counts: dict[str, int] = field(default_factory=dict)
    sqlite_version: str = ""

    def to_dict(self) -> dict:
        data = asdict(self)
        data["path"] = str(self.path)
        data["manifest_path"] = str(self.manifest_path)
        return data


@dataclass
class VerificationReport:
    path: Path
    ok: bool
    problems: list[str] = field(default_factory=list)
    schema_version: int | None = None
    sha256: str | None = None
    integrity: str | None = None
    table_counts: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["path"] = str(self.path)
        return data


@dataclass
class RestoreReport:
    restored_from: Path
    database: Path
    safety_copy: Path | None
    schema_version: int
    sha256: str

    def to_dict(self) -> dict:
        data = asdict(self)
        data["restored_from"] = str(self.restored_from)
        data["database"] = str(self.database)
        data["safety_copy"] = str(self.safety_copy) if self.safety_copy else None
        return data


# --------------------------------------------------------------------------
# Low-level inspection
# --------------------------------------------------------------------------


def sha256_file(path: str | Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def open_read_only(path: str | Path) -> sqlite3.Connection:
    """Open a database for inspection without creating or modifying it."""
    connection = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


_read_only = open_read_only


def integrity_check(path: str | Path) -> str:
    """Return ``'ok'`` or a description of the first problem found.

    Never raises: a corrupt file is exactly what this function exists to report,
    so a ``sqlite3.DatabaseError`` ("database disk image is malformed") is
    returned as the message rather than propagated. An operator running
    ``integrity-check`` on a broken database should get a diagnosis, not a
    traceback.
    """
    try:
        connection = _read_only(path)
    except sqlite3.Error as error:
        return f"无法打开数据库：{error}"
    try:
        rows = connection.execute("PRAGMA integrity_check").fetchall()
    except sqlite3.Error as error:
        return str(error)
    finally:
        connection.close()
    messages = [str(row[0]) for row in rows]
    if messages == ["ok"]:
        return "ok"
    return "; ".join(messages) if messages else "unknown"


def foreign_key_violations(path: str | Path) -> list[str]:
    try:
        connection = _read_only(path)
    except sqlite3.Error:
        return []
    try:
        rows = connection.execute("PRAGMA foreign_key_check").fetchall()
    except sqlite3.Error:
        return []
    finally:
        connection.close()
    return [f"{row[0]}: rowid {row[1]}" for row in rows]


def schema_version(path: str | Path) -> int:
    try:
        connection = _read_only(path)
    except sqlite3.Error:
        return 0
    try:
        return int(connection.execute("PRAGMA user_version").fetchone()[0])
    except sqlite3.Error:
        return 0
    finally:
        connection.close()


def table_names(path: str | Path) -> set[str]:
    try:
        connection = _read_only(path)
    except sqlite3.Error:
        return set()
    try:
        return {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }
    except sqlite3.Error:
        return set()
    finally:
        connection.close()


def table_counts(path: str | Path, tables: tuple[str, ...] = COUNTED_TABLES) -> dict[str, int]:
    connection = _read_only(path)
    try:
        present = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        counts: dict[str, int] = {}
        for table in tables:
            if table not in present:
                continue
            counts[table] = int(
                connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            )
        return counts
    finally:
        connection.close()


def target_schema_version() -> int:
    """The schema version this build migrates to."""
    from traceability.db import SCHEMA_VERSION

    return int(SCHEMA_VERSION)


def check_disk_space(path: str | Path, required_bytes: int) -> int:
    """Return free bytes, raising if there is not enough room.

    A backup that fails halfway because the disk filled is worse than no backup,
    because it looks like one.
    """
    probe = Path(path)
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    free = shutil.disk_usage(probe).free
    if free < required_bytes:
        raise BackupError(
            f"磁盘空间不足：需要 {required_bytes / 1048576:.1f} MB，"
            f"可用 {free / 1048576:.1f} MB（{probe}）"
        )
    return free


# --------------------------------------------------------------------------
# Backup
# --------------------------------------------------------------------------


def manifest_path_for(backup_path: str | Path) -> Path:
    backup = Path(backup_path)
    return backup.with_name(backup.name + MANIFEST_SUFFIX)


def _unique_path(directory: Path, stem: str, suffix: str) -> Path:
    """Pick a filename that does not exist yet.

    Backups taken within the same second would otherwise collide, and the second
    one would silently overwrite the first — the worst possible failure mode for
    a backup tool, because the operator sees "备份完成" and assumes both exist.
    """
    candidate = directory / f"{stem}{suffix}"
    if not candidate.exists():
        return candidate
    for index in range(1, 1000):
        candidate = directory / f"{stem}_{index}{suffix}"
        if not candidate.exists():
            return candidate
    raise BackupError(f"备份目录中同名文件过多：{stem}{suffix}")


def create_backup(
    database: str | Path,
    backup_dir: str | Path,
    *,
    created_at: str,
    prefix: str = DEFAULT_BACKUP_PREFIX,
    keep: int | None = None,
) -> BackupReport:
    """Take a consistent snapshot, verify it, and record a manifest.

    Uses the SQLite online backup API, so the server may keep running: a plain
    file copy of a live database can miss pages that only exist in the ``-wal``.
    """
    source = Path(database)
    if not source.exists():
        raise BackupError("数据库尚未创建，请先启动一次系统。")

    destination_dir = Path(backup_dir)
    destination_dir.mkdir(parents=True, exist_ok=True)

    # Leave room for the copy plus the temporary files SQLite may need.
    source_size = source.stat().st_size
    check_disk_space(destination_dir, required_bytes=source_size * 2 + 8 * 1024 * 1024)

    stamp = created_at.replace(":", "").replace("-", "").replace("+", "_")
    target = _unique_path(destination_dir, f"{prefix}_{stamp}", ".db")

    source_connection = sqlite3.connect(str(source), timeout=30)
    target_connection = sqlite3.connect(str(target))
    try:
        source_connection.backup(target_connection)
        target_connection.commit()
    finally:
        target_connection.close()
        source_connection.close()

    integrity = integrity_check(target)
    if integrity != "ok":
        raise BackupError(f"备份文件自检失败：{integrity}")

    digest = sha256_file(target)
    version = schema_version(target)
    counts = table_counts(target)

    report = BackupReport(
        path=target,
        manifest_path=manifest_path_for(target),
        created_at=created_at,
        schema_version=version,
        sha256=digest,
        size_bytes=target.stat().st_size,
        integrity=integrity,
        table_counts=counts,
        sqlite_version=sqlite3.sqlite_version,
    )
    report.manifest_path.write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )

    if keep is not None and keep > 0:
        prune_backups(destination_dir, keep=keep, prefix=prefix)

    return report


def load_manifest(backup_path: str | Path) -> dict | None:
    manifest = manifest_path_for(backup_path)
    if not manifest.exists():
        return None
    try:
        return json.loads(manifest.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


# --------------------------------------------------------------------------
# Verification
# --------------------------------------------------------------------------


def verify_backup(backup_path: str | Path, *, expect_schema_version: int | None = None) -> VerificationReport:
    """Prove a backup file is usable before anyone relies on it.

    Checks, in order of cheapness: the file exists and is a SQLite database,
    the recorded checksum matches, ``integrity_check`` passes, the required
    tables are present, and the schema version is sane relative to this build.
    """
    path = Path(backup_path)
    problems: list[str] = []

    if not path.exists():
        return VerificationReport(path=path, ok=False, problems=["备份文件不存在"])
    if path.stat().st_size == 0:
        return VerificationReport(path=path, ok=False, problems=["备份文件为空"])

    try:
        header = path.open("rb").read(16)
    except OSError as error:
        return VerificationReport(path=path, ok=False, problems=[f"无法读取备份文件：{error}"])
    if header[:15] != b"SQLite format 3":
        return VerificationReport(path=path, ok=False, problems=["不是 SQLite 数据库文件"])

    manifest = load_manifest(path)
    digest = sha256_file(path)
    if manifest is None:
        problems.append("缺少校验清单（manifest），无法证明文件完整性")
    elif manifest.get("sha256") != digest:
        problems.append(
            "SHA-256 不匹配：文件与备份时记录的不一致（可能已损坏或被修改）"
        )

    integrity = integrity_check(path)
    if integrity != "ok":
        problems.append(f"integrity_check 未通过：{integrity}")

    present = table_names(path)
    missing = [table for table in REQUIRED_TABLES if table not in present]
    if missing:
        problems.append(f"缺少必要的表：{', '.join(missing)}")

    violations = foreign_key_violations(path)
    if violations:
        problems.append(f"外键校验失败：{'; '.join(violations[:5])}")

    version = schema_version(path)
    target = target_schema_version() if expect_schema_version is None else expect_schema_version
    if version > target:
        problems.append(
            f"备份的数据库结构版本为 {version}，高于当前程序支持的 {target}；"
            "请使用更新的程序来恢复"
        )

    return VerificationReport(
        path=path,
        ok=not problems,
        problems=problems,
        schema_version=version,
        sha256=digest,
        integrity=integrity,
        table_counts=table_counts(path),
    )


# --------------------------------------------------------------------------
# Restore
# --------------------------------------------------------------------------


def restore_backup(
    backup_path: str | Path,
    database: str | Path,
    *,
    safety_dir: str | Path | None = None,
    created_at: str,
    keep_safety_copy: bool = True,
) -> RestoreReport:
    """Install a verified backup as the live database.

    Stop the service first: this replaces the database file, and a running
    server holds its own handle plus ``-wal``/``-shm`` sidecars.
    """
    source = Path(backup_path)
    target = Path(database)

    verification = verify_backup(source)
    if not verification.ok:
        raise BackupError("备份未通过校验，已中止恢复：\n  - " + "\n  - ".join(verification.problems))

    # An older build must never silently downgrade a newer database.
    if verification.schema_version is not None and verification.schema_version > target_schema_version():
        raise BackupError(
            f"备份的结构版本 {verification.schema_version} 高于当前程序支持的 "
            f"{target_schema_version()}，拒绝恢复"
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    check_disk_space(target.parent, required_bytes=source.stat().st_size * 2 + 8 * 1024 * 1024)

    safety_copy: Path | None = None
    if target.exists() and keep_safety_copy:
        directory = Path(safety_dir) if safety_dir else target.parent
        directory.mkdir(parents=True, exist_ok=True)
        stamp = created_at.replace(":", "").replace("-", "").replace("+", "_")
        safety_copy = directory / f"{target.stem}_pre-restore_{stamp}{target.suffix}"
        shutil.copy2(target, safety_copy)

    # Write through a temporary file, then replace atomically, so an interrupted
    # restore cannot leave a half-written database in place.
    temporary = target.with_name(target.name + ".restore-tmp")
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()

    # Stale sidecars would otherwise be replayed onto the restored file and
    # corrupt it.
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(target) + suffix)
        if sidecar.exists():
            sidecar.unlink()

    integrity = integrity_check(target)
    if integrity != "ok":
        raise BackupError(
            f"恢复后的数据库自检失败：{integrity}。"
            + (f"恢复前的副本保留在 {safety_copy}" if safety_copy else "")
        )

    return RestoreReport(
        restored_from=source,
        database=target,
        safety_copy=safety_copy,
        schema_version=schema_version(target),
        sha256=sha256_file(target),
    )


# --------------------------------------------------------------------------
# Retention and reporting
# --------------------------------------------------------------------------


def list_backups(backup_dir: str | Path, *, prefix: str = DEFAULT_BACKUP_PREFIX) -> list[Path]:
    directory = Path(backup_dir)
    if not directory.exists():
        return []
    return sorted(
        (path for path in directory.glob(f"{prefix}_*.db") if path.is_file()),
        key=lambda path: path.name,
    )


def prune_backups(
    backup_dir: str | Path, *, keep: int, prefix: str = DEFAULT_BACKUP_PREFIX
) -> list[Path]:
    """Delete the oldest backups, keeping the newest ``keep``. Manifests go too."""
    backups = list_backups(backup_dir, prefix=prefix)
    if keep < 0:
        raise BackupError("保留数量不能为负数")
    doomed = backups[: max(0, len(backups) - keep)]
    removed: list[Path] = []
    for path in doomed:
        manifest = manifest_path_for(path)
        if manifest.exists():
            manifest.unlink()
        path.unlink()
        removed.append(path)
    return removed


def database_info(database: str | Path) -> dict:
    """Non-secret facts about a database, for operators and the ready probe."""
    path = Path(database)
    if not path.exists():
        return {"exists": False, "path": str(path)}
    connection = _read_only(path)
    try:
        page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
        page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
        freelist = int(connection.execute("PRAGMA freelist_count").fetchone()[0])
        journal_mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0])
    finally:
        connection.close()
    info = {
        "exists": True,
        "path": str(path),
        "size_bytes": path.stat().st_size,
        "schema_version": schema_version(path),
        "target_schema_version": target_schema_version(),
        "page_size": page_size,
        "page_count": page_count,
        "freelist_count": freelist,
        "journal_mode": journal_mode,
        "integrity": integrity_check(path),
        "foreign_key_violations": len(foreign_key_violations(path)),
        "table_counts": table_counts(path),
        "sqlite_version": sqlite3.sqlite_version,
    }
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(path) + suffix)
        info[f"has{suffix.replace('-', '_')}"] = sidecar.exists()
    return info
