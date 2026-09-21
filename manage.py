from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
DATABASE = BASE_DIR / "data" / "traceability.db"
BACKUP_DIR = BASE_DIR / "exports" / "backups"


def backup_database() -> Path:
    if not DATABASE.exists():
        raise SystemExit("数据库尚未创建，请先启动一次系统。")
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    target = BACKUP_DIR / f"traceability_{datetime.now():%Y%m%d_%H%M%S}.db"
    source_connection = sqlite3.connect(DATABASE)
    target_connection = sqlite3.connect(target)
    try:
        source_connection.backup(target_connection)
    finally:
        target_connection.close()
        source_connection.close()
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description="聚星同创仓库管理系统维护工具")
    parser.add_argument("command", choices=["backup"], help="backup：安全备份 SQLite 数据库")
    arguments = parser.parse_args()
    if arguments.command == "backup":
        print(f"备份完成：{backup_database()}")


if __name__ == "__main__":
    main()
