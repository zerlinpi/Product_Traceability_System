from __future__ import annotations

import sqlite3

import pytest

from app import create_app
import traceability.db as db_module
from test_lingxing_migration import V14_TABLES, prepare_version, table_names


def test_real_v13_database_converges_roles_and_adds_v14_tables(tmp_path):
    database_path = tmp_path / "v13.db"
    prepare_version(database_path, 13)

    create_app({"TESTING": True, "DATABASE": str(database_path)})

    connection = db_module.connect_database(database_path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 20
        assert V14_TABLES <= table_names(connection)
        assert connection.execute(
            "SELECT role FROM users WHERE username='legacy.operator'"
        ).fetchone()[0] == "WAREHOUSE"
        sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='users'"
        ).fetchone()[0]
        assert "'ADMIN', 'WAREHOUSE', 'OPERATIONS'" in sql
        assert "'OPERATOR'" not in sql
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO users(username, display_name, password_hash, role, created_at, updated_at)
                VALUES ('invalid.operator', '非法角色', 'hash', 'OPERATOR', 'now', 'now')
                """
            )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()


def test_v14_failure_rolls_back_version_tables_and_role_mapping(tmp_path, monkeypatch):
    database_path = tmp_path / "v13-rollback.db"
    prepare_version(database_path, 13)
    failing = db_module.THREE_ROLE_V14_STATEMENTS + (
        "INSERT INTO __missing_v14_table__(id) VALUES (1)",
    )
    monkeypatch.setattr(db_module, "THREE_ROLE_V14_STATEMENTS", failing)

    with pytest.raises(sqlite3.OperationalError):
        create_app({"TESTING": True, "DATABASE": str(database_path)})

    connection = db_module.connect_database(database_path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 13
        assert table_names(connection).isdisjoint(V14_TABLES)
        assert connection.execute(
            "SELECT role FROM users WHERE username='legacy.operator'"
        ).fetchone()[0] == "OPERATOR"
    finally:
        connection.close()

