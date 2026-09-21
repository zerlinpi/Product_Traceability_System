"""聚星同创仓库管理系统维护工具。

用法示例：
    python manage.py backup                     # 备份 + 自检 + 生成校验清单
    python manage.py verify-backup FILE         # 校验一个备份是否可用
    python manage.py restore FILE --yes         # 用备份覆盖当前数据库
    python manage.py integrity-check            # 检查当前数据库
    python manage.py db-info                    # 结构与体量信息
    python manage.py list-backups
    python manage.py prune-backups --keep 30
    python manage.py preflight                  # 升级前检查
    python manage.py postflight                 # 升级后检查

恢复演练见 docs/BACKUP_RESTORE.md。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from traceability.backup import (
    BackupError,
    check_disk_space,
    create_backup,
    database_info,
    foreign_key_violations,
    integrity_check,
    list_backups,
    prune_backups,
    restore_backup,
    schema_version,
    target_schema_version,
    table_names,
    verify_backup,
    REQUIRED_TABLES,
)

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DATABASE = BASE_DIR / "data" / "traceability.db"
DEFAULT_BACKUP_DIR = BASE_DIR / "exports" / "backups"

OK = "[OK]"
WARN = "[警告]"
FAIL = "[失败]"


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _human_bytes(value: int) -> str:
    if value < 1024:
        return f"{value} B"
    if value < 1024 * 1024:
        return f"{value / 1024:.1f} KB"
    if value < 1024 * 1024 * 1024:
        return f"{value / 1048576:.1f} MB"
    return f"{value / 1073741824:.2f} GB"


def _print_counts(counts: dict[str, int]) -> None:
    if not counts:
        print("    （无计数）")
        return
    for table, count in sorted(counts.items()):
        print(f"    {table:<28} {count:>8}")


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def command_backup(args: argparse.Namespace) -> int:
    report = create_backup(
        args.database,
        args.backup_dir,
        created_at=now(),
        keep=args.keep,
    )
    print(f"{OK} 备份完成")
    print(f"    文件      {report.path}")
    print(f"    校验清单  {report.manifest_path}")
    print(f"    大小      {_human_bytes(report.size_bytes)}")
    print(f"    结构版本  {report.schema_version}")
    print(f"    自检      {report.integrity}")
    print(f"    SHA-256   {report.sha256}")
    print("    记录数：")
    _print_counts(report.table_counts)
    print()
    print("  提示：请把备份复制到另一块磁盘或受控文件服务器。")
    print("        备份在同一块盘上不能抵御磁盘损坏。")
    return 0


def command_verify_backup(args: argparse.Namespace) -> int:
    report = verify_backup(args.file)
    print(f"校验：{report.path}")
    print(f"    结构版本  {report.schema_version}")
    print(f"    自检      {report.integrity}")
    print(f"    SHA-256   {report.sha256}")
    if report.ok:
        print(f"{OK} 备份可用")
        print("    记录数：")
        _print_counts(report.table_counts)
        return 0
    print(f"{FAIL} 备份不可用：")
    for problem in report.problems:
        print(f"    - {problem}")
    return 1


def command_restore(args: argparse.Namespace) -> int:
    if not args.yes:
        print(f"{FAIL} 恢复会覆盖当前数据库，请加 --yes 明确确认。")
        print("       建议先执行 python manage.py backup 保留当前数据。")
        return 2
    report = restore_backup(
        args.file,
        args.database,
        safety_dir=args.backup_dir,
        created_at=now(),
        keep_safety_copy=not args.no_safety_copy,
    )
    print(f"{OK} 恢复完成")
    print(f"    来源          {report.restored_from}")
    print(f"    当前数据库    {report.database}")
    if report.safety_copy:
        print(f"    恢复前副本    {report.safety_copy}")
    print(f"    结构版本      {report.schema_version}")
    print()
    print("  请重新启动服务，并确认数据与预期一致。")
    return 0


def command_integrity_check(args: argparse.Namespace) -> int:
    database = Path(args.database)
    if not database.exists():
        print(f"{FAIL} 数据库不存在：{database}")
        return 1
    result = integrity_check(database)
    violations = foreign_key_violations(database)
    if result == "ok":
        print(f"{OK} integrity_check 通过")
    else:
        print(f"{FAIL} integrity_check 未通过：{result}")
    if violations:
        print(f"{FAIL} 外键校验发现 {len(violations)} 处问题：")
        for item in violations[:10]:
            print(f"    - {item}")
    else:
        print(f"{OK} foreign_key_check 通过")
    return 0 if result == "ok" and not violations else 1


def command_db_info(args: argparse.Namespace) -> int:
    info = database_info(args.database)
    if not info.get("exists"):
        print(f"{FAIL} 数据库不存在：{info['path']}")
        return 1
    print("数据库信息")
    print(f"    路径            {info['path']}")
    print(f"    大小            {_human_bytes(info['size_bytes'])}")
    print(f"    结构版本        {info['schema_version']}（程序目标 {info['target_schema_version']}）")
    print(f"    日志模式        {info['journal_mode']}")
    print(f"    页大小 / 页数   {info['page_size']} / {info['page_count']}")
    print(f"    空闲页          {info['freelist_count']}")
    print(f"    自检            {info['integrity']}")
    print(f"    外键问题        {info['foreign_key_violations']}")
    print(f"    残留 -wal       {info.get('has_wal', False)}")
    print(f"    SQLite 版本     {info['sqlite_version']}")
    print("    记录数：")
    _print_counts(info["table_counts"])
    if info["schema_version"] != info["target_schema_version"]:
        print()
        print(
            f"{WARN} 数据库结构版本与程序目标不一致；"
            "启动一次服务会执行增量迁移。"
        )
    return 0


def command_list_backups(args: argparse.Namespace) -> int:
    backups = list_backups(args.backup_dir)
    if not backups:
        print(f"备份目录为空：{args.backup_dir}")
        return 0
    print(f"备份目录：{args.backup_dir}")
    for path in reversed(backups):
        size = _human_bytes(path.stat().st_size)
        manifest = path.with_name(path.name + ".manifest.json")
        mark = "" if manifest.exists() else "  (无校验清单)"
        print(f"    {path.name:<40} {size:>10}{mark}")
    return 0


def command_prune_backups(args: argparse.Namespace) -> int:
    removed = prune_backups(args.backup_dir, keep=args.keep)
    if not removed:
        print(f"{OK} 无需清理（保留 {args.keep} 份）")
        return 0
    print(f"{OK} 已清理 {len(removed)} 份旧备份（保留最新 {args.keep} 份）：")
    for path in removed:
        print(f"    {path.name}")
    return 0


def command_preflight(args: argparse.Namespace) -> int:
    """升级前检查：先备份，再确认数据库健康、磁盘充足、结构版本已知。"""
    database = Path(args.database)
    print("升级前检查")
    problems: list[str] = []

    if not database.exists():
        print(f"    数据库          {database}（不存在，将视为全新安装）")
    else:
        result = integrity_check(database)
        print(f"    自检            {result}")
        if result != "ok":
            problems.append(f"数据库自检未通过：{result}")

        violations = foreign_key_violations(database)
        print(f"    外键问题        {len(violations)}")
        if violations:
            problems.append(f"外键校验发现 {len(violations)} 处问题")

        current = schema_version(database)
        target = target_schema_version()
        print(f"    结构版本        {current} → 目标 {target}")
        if current > target:
            problems.append(
                f"数据库结构版本 {current} 高于程序支持的 {target}，"
                "降级会导致数据丢失，已中止"
            )

    try:
        free = check_disk_space(args.backup_dir, required_bytes=64 * 1024 * 1024)
        print(f"    备份盘可用空间  {_human_bytes(free)}")
    except BackupError as error:
        problems.append(str(error))

    if problems:
        print(f"{FAIL} 检查未通过：")
        for problem in problems:
            print(f"    - {problem}")
        return 1

    if database.exists():
        report = create_backup(args.database, args.backup_dir, created_at=now(), keep=args.keep)
        print(f"    升级前备份      {report.path.name}（{_human_bytes(report.size_bytes)}）")
        print(f"{OK} 检查通过，可以升级")
    else:
        print(f"{OK} 检查通过（全新安装）")
    print()
    print("  下一步：停止服务 → 替换程序文件（保留 data/ 与 settings）→ 启动服务")
    return 0


def command_postflight(args: argparse.Namespace) -> int:
    """升级后检查：结构版本、完整性、外键、必要表、基本读写。"""
    database = Path(args.database)
    print("升级后检查")
    if not database.exists():
        print(f"{FAIL} 数据库不存在：{database}")
        return 1

    problems: list[str] = []
    current = schema_version(database)
    target = target_schema_version()
    print(f"    结构版本        {current}（目标 {target}）")
    if current != target:
        problems.append(f"结构版本为 {current}，与程序目标 {target} 不一致；请查看启动日志")

    result = integrity_check(database)
    print(f"    自检            {result}")
    if result != "ok":
        problems.append(f"数据库自检未通过：{result}")

    violations = foreign_key_violations(database)
    print(f"    外键问题        {len(violations)}")
    if violations:
        problems.append(f"外键校验发现 {len(violations)} 处问题")

    present = table_names(database)
    missing = [table for table in REQUIRED_TABLES if table not in present]
    print(f"    必要表          {'齐全' if not missing else '缺失 ' + ', '.join(missing)}")
    if missing:
        problems.append(f"缺少必要的表：{', '.join(missing)}")

    info = database_info(database)
    counts = info.get("table_counts", {})
    print(f"    账号数          {counts.get('users', 0)}")
    if counts.get("users", 0) == 0:
        problems.append("没有任何账号；请确认迁移是否正常，或需要重新引导管理员")

    if problems:
        print(f"{FAIL} 检查未通过：")
        for problem in problems:
            print(f"    - {problem}")
        print()
        print("  恢复方式：python manage.py restore <升级前的备份> --yes")
        return 1

    print(f"{OK} 检查通过，升级完成")
    return 0


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="聚星同创仓库管理系统维护工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--database",
        default=os.environ.get("PTS_DATABASE", str(DEFAULT_DATABASE)),
        help="数据库路径（默认 data/traceability.db）",
    )
    parser.add_argument(
        "--backup-dir",
        default=os.environ.get("PTS_BACKUP_DIR", str(DEFAULT_BACKUP_DIR)),
        help="备份目录（默认 exports/backups）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    backup = sub.add_parser("backup", help="安全备份数据库并生成校验清单")
    backup.add_argument("--keep", type=int, default=None, help="备份后仅保留最新 N 份")
    backup.set_defaults(handler=command_backup)

    verify = sub.add_parser("verify-backup", help="校验备份是否可用")
    verify.add_argument("file", help="备份文件路径")
    verify.set_defaults(handler=command_verify_backup)

    restore = sub.add_parser("restore", help="用备份覆盖当前数据库")
    restore.add_argument("file", help="备份文件路径")
    restore.add_argument("--yes", action="store_true", help="确认覆盖当前数据库")
    restore.add_argument(
        "--no-safety-copy",
        action="store_true",
        help="不保留恢复前的数据库副本（不推荐）",
    )
    restore.set_defaults(handler=command_restore)

    integrity = sub.add_parser("integrity-check", help="检查当前数据库完整性与外键")
    integrity.set_defaults(handler=command_integrity_check)

    info = sub.add_parser("db-info", help="显示数据库结构与体量信息")
    info.set_defaults(handler=command_db_info)

    listing = sub.add_parser("list-backups", help="列出备份文件")
    listing.set_defaults(handler=command_list_backups)

    prune = sub.add_parser("prune-backups", help="清理旧备份")
    prune.add_argument("--keep", type=int, required=True, help="保留最新 N 份")
    prune.set_defaults(handler=command_prune_backups)

    preflight = sub.add_parser("preflight", help="升级前检查（含自动备份）")
    preflight.add_argument("--keep", type=int, default=None, help="备份后仅保留最新 N 份")
    preflight.set_defaults(handler=command_preflight)

    postflight = sub.add_parser("postflight", help="升级后检查")
    postflight.set_defaults(handler=command_postflight)

    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        return int(arguments.handler(arguments))
    except BackupError as error:
        print(f"{FAIL} {error}")
        return 1
    except KeyboardInterrupt:
        print("\n已取消。")
        return 130


if __name__ == "__main__":
    sys.exit(main())
