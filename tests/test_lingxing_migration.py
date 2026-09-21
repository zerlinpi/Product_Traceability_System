from __future__ import annotations

import sqlite3

from app import create_app
import traceability.db as db_module


V13_TABLES = {"purchase_orders", "inbound_receipts"}
V14_TABLES = {"app_settings", "production_orders", "product_stock", "inbound_scan_records"}


def table_names(connection: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }


def rebuild_roles(connection, roles, *, migrate_operator=False):
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute("BEGIN IMMEDIATE")
    try:
        db_module._rebuild_users_for_roles(
            connection, tuple(roles), migrate_operator=migrate_operator
        )
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise
    finally:
        connection.execute("PRAGMA foreign_keys = ON")


def prepare_version(database_path, version: int):
    create_app({"TESTING": True, "DATABASE": str(database_path)})
    connection = db_module.connect_database(database_path)
    try:
        connection.execute("PRAGMA foreign_keys = OFF")
        for table in ("inbound_scan_records", "product_stock", "production_orders", "app_settings"):
            connection.execute(f"DROP TABLE IF EXISTS {table}")
        if version == 12:
            for table in ("inbound_receipts", "purchase_orders"):
                connection.execute(f"DROP TABLE IF EXISTS {table}")
        connection.execute("PRAGMA foreign_keys = ON")
        roles = ("ADMIN", "OPERATOR") if version == 12 else (
            "ADMIN",
            "OPERATOR",
            "WAREHOUSE",
            "OPERATIONS",
        )
        rebuild_roles(connection, roles)
        connection.execute(
            """
            INSERT INTO users(
                username, display_name, password_hash, role, active,
                must_change_password, session_version, created_at, updated_at
            ) VALUES ('legacy.operator', '历史录入员', 'hash', 'OPERATOR', 1, 0, 7, 'before', 'before')
            """
        )
        connection.execute(f"PRAGMA user_version = {version}")
    finally:
        connection.close()


def test_real_v12_database_migrates_through_v13_without_losing_history(tmp_path):
    database_path = tmp_path / "v12.db"
    prepare_version(database_path, 12)
    connection = db_module.connect_database(database_path)
    try:
        before_products = [tuple(row) for row in connection.execute("SELECT * FROM product_models")]
        assert table_names(connection).isdisjoint(V13_TABLES | V14_TABLES)
    finally:
        connection.close()

    create_app({"TESTING": True, "DATABASE": str(database_path)})

    connection = db_module.connect_database(database_path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 20
        assert V13_TABLES | V14_TABLES <= table_names(connection)
        assert [tuple(row) for row in connection.execute("SELECT * FROM product_models")] == before_products
        migrated = connection.execute(
            "SELECT role, session_version, created_at, updated_at FROM users WHERE username='legacy.operator'"
        ).fetchone()
        assert tuple(migrated) == ("WAREHOUSE", 7, "before", "before")
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()

