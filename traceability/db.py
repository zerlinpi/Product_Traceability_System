from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from flask import current_app, g


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL COLLATE NOCASE UNIQUE,
    display_name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('ADMIN', 'WAREHOUSE', 'OPERATIONS')),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    must_change_password INTEGER NOT NULL DEFAULT 0 CHECK (must_change_password IN (0, 1)),
    session_version INTEGER NOT NULL DEFAULT 1,
    last_login_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS product_families (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_code TEXT NOT NULL COLLATE NOCASE UNIQUE,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS product_models (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_family_id INTEGER NOT NULL REFERENCES product_families(id) ON DELETE RESTRICT,
    model_code TEXT NOT NULL COLLATE NOCASE UNIQUE,
    name TEXT NOT NULL,
    serial_prefix TEXT NOT NULL DEFAULT '',
    identity_source TEXT NOT NULL DEFAULT 'SCANNER'
        CHECK (identity_source IN ('BLUETOOTH', 'SCANNER', 'MANUAL')),
    bluetooth_name_prefix TEXT NOT NULL DEFAULT '',
    bluetooth_service_uuid TEXT NOT NULL DEFAULT '',
    bluetooth_notify_uuid TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS user_product_model_permissions (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    product_model_id INTEGER NOT NULL REFERENCES product_models(id) ON DELETE CASCADE,
    PRIMARY KEY (user_id, product_model_id)
);

CREATE TABLE IF NOT EXISTS suppliers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    supplier_code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    contact TEXT NOT NULL DEFAULT '',
    phone TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS user_supplier_permissions (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    supplier_id INTEGER NOT NULL REFERENCES suppliers(id) ON DELETE CASCADE,
    PRIMARY KEY (user_id, supplier_id)
);

CREATE TABLE IF NOT EXISTS part_types (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    part_code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    category_code TEXT NOT NULL DEFAULT '',
    category_name TEXT NOT NULL DEFAULT '',
    specification TEXT NOT NULL DEFAULT '',
    minimum_stock INTEGER NOT NULL DEFAULT 0 CHECK (minimum_stock >= 0),
    supplier_id INTEGER NOT NULL REFERENCES suppliers(id) ON DELETE RESTRICT,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS supplier_inventory_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    part_type_id INTEGER NOT NULL REFERENCES part_types(id) ON DELETE RESTRICT,
    batch_no TEXT NOT NULL COLLATE NOCASE,
    quantity_received INTEGER NOT NULL CHECK (quantity_received >= 0),
    quantity_available INTEGER NOT NULL CHECK (quantity_available >= 0),
    production_date TEXT NOT NULL DEFAULT '',
    received_date TEXT NOT NULL DEFAULT '',
    remarks TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (part_type_id, batch_no)
);

CREATE TABLE IF NOT EXISTS trace_plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model_code TEXT NOT NULL,
    product_model_id INTEGER REFERENCES product_models(id) ON DELETE RESTRICT,
    name TEXT NOT NULL,
    version TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'DRAFT' CHECK (status IN ('DRAFT', 'ACTIVE', 'ARCHIVED')),
    created_at TEXT NOT NULL,
    activated_at TEXT,
    UNIQUE (model_code, version)
);

CREATE TABLE IF NOT EXISTS trace_plan_slots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_plan_id INTEGER NOT NULL REFERENCES trace_plans(id) ON DELETE CASCADE,
    position INTEGER NOT NULL CHECK (position > 0),
    slot_name TEXT NOT NULL,
    part_type_id INTEGER NOT NULL REFERENCES part_types(id) ON DELETE RESTRICT,
    supplier_inventory_batch_id INTEGER REFERENCES supplier_inventory_batches(id) ON DELETE RESTRICT,
    UNIQUE (trace_plan_id, position)
);

CREATE TABLE IF NOT EXISTS machines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sn TEXT NOT NULL UNIQUE,
    model TEXT NOT NULL,
    product_model_id INTEGER REFERENCES product_models(id) ON DELETE RESTRICT,
    production_date TEXT NOT NULL DEFAULT '',
    trace_plan_id INTEGER REFERENCES trace_plans(id) ON DELETE RESTRICT,
    identification_code TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS part_label_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_code TEXT NOT NULL UNIQUE,
    part_type_id INTEGER NOT NULL REFERENCES part_types(id) ON DELETE RESTRICT,
    lot_no TEXT NOT NULL DEFAULT '',
    supplier_batch_no TEXT NOT NULL DEFAULT '',
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    production_date TEXT NOT NULL DEFAULT '',
    generated_by TEXT NOT NULL DEFAULT '',
    generated_by_user_id INTEGER REFERENCES users(id) ON DELETE RESTRICT,
    generated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS part_labels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    identification_code TEXT NOT NULL UNIQUE,
    part_type_id INTEGER NOT NULL REFERENCES part_types(id) ON DELETE RESTRICT,
    label_batch_id INTEGER REFERENCES part_label_batches(id) ON DELETE RESTRICT,
    lot_no TEXT NOT NULL DEFAULT '',
    supplier_batch_no TEXT NOT NULL DEFAULT '',
    source_serial_no TEXT NOT NULL DEFAULT '',
    production_date TEXT NOT NULL DEFAULT '',
    inspection_status TEXT NOT NULL DEFAULT 'PENDING',
    remarks TEXT NOT NULL DEFAULT '',
    entered_by TEXT NOT NULL DEFAULT '',
    entered_by_user_id INTEGER REFERENCES users(id) ON DELETE RESTRICT,
    entered_at TEXT,
    updated_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS product_code_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_code TEXT NOT NULL COLLATE NOCASE UNIQUE,
    product_model_id INTEGER NOT NULL REFERENCES product_models(id) ON DELETE RESTRICT,
    trace_plan_id INTEGER NOT NULL REFERENCES trace_plans(id) ON DELETE RESTRICT,
    prefix TEXT NOT NULL COLLATE NOCASE,
    date_code TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    start_sequence INTEGER NOT NULL CHECK (start_sequence > 0),
    end_sequence INTEGER NOT NULL CHECK (end_sequence >= start_sequence),
    generated_by_user_id INTEGER REFERENCES users(id) ON DELETE RESTRICT,
    generated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS product_code_sets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    generation_batch_id INTEGER REFERENCES product_code_batches(id) ON DELETE RESTRICT,
    product_model_id INTEGER NOT NULL REFERENCES product_models(id) ON DELETE RESTRICT,
    trace_plan_id INTEGER NOT NULL REFERENCES trace_plans(id) ON DELETE RESTRICT,
    machine_id INTEGER NOT NULL UNIQUE REFERENCES machines(id) ON DELETE CASCADE,
    prefix TEXT NOT NULL COLLATE NOCASE,
    date_code TEXT NOT NULL,
    daily_sequence INTEGER NOT NULL CHECK (daily_sequence > 0),
    set_code TEXT NOT NULL COLLATE NOCASE UNIQUE,
    generated_by_user_id INTEGER REFERENCES users(id) ON DELETE RESTRICT,
    generated_at TEXT NOT NULL,
    UNIQUE (prefix, date_code, daily_sequence)
);

CREATE TABLE IF NOT EXISTS supplier_inventory_movements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    inventory_batch_id INTEGER NOT NULL REFERENCES supplier_inventory_batches(id) ON DELETE RESTRICT,
    movement_type TEXT NOT NULL CHECK (movement_type IN ('RECEIPT', 'ISSUE', 'ADJUSTMENT', 'RETURN')),
    quantity_change INTEGER NOT NULL CHECK (quantity_change <> 0),
    balance_after INTEGER NOT NULL CHECK (balance_after >= 0),
    product_model_id INTEGER REFERENCES product_models(id) ON DELETE RESTRICT,
    product_code_batch_id INTEGER REFERENCES product_code_batches(id) ON DELETE RESTRICT,
    actor_user_id INTEGER REFERENCES users(id) ON DELETE RESTRICT,
    reason TEXT NOT NULL DEFAULT '',
    occurred_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS product_code_set_parts (
    product_code_set_id INTEGER NOT NULL REFERENCES product_code_sets(id) ON DELETE CASCADE,
    position INTEGER NOT NULL CHECK (position > 0),
    slot_name TEXT NOT NULL,
    part_label_id INTEGER NOT NULL UNIQUE REFERENCES part_labels(id) ON DELETE CASCADE,
    PRIMARY KEY (product_code_set_id, position)
);

CREATE TABLE IF NOT EXISTS scan_sessions (
    station_id TEXT PRIMARY KEY,
    station_name TEXT NOT NULL DEFAULT '',
    operator_name TEXT NOT NULL DEFAULT '',
    user_id INTEGER REFERENCES users(id) ON DELETE RESTRICT,
    machine_id INTEGER REFERENCES machines(id) ON DELETE SET NULL,
    trace_plan_id INTEGER REFERENCES trace_plans(id) ON DELETE RESTRICT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scan_session_items (
    station_id TEXT NOT NULL REFERENCES scan_sessions(station_id) ON DELETE CASCADE,
    position INTEGER NOT NULL CHECK (position > 0),
    part_label_id INTEGER NOT NULL REFERENCES part_labels(id) ON DELETE RESTRICT,
    PRIMARY KEY (station_id, position),
    UNIQUE (station_id, part_label_id)
);

CREATE TABLE IF NOT EXISTS trace_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_no TEXT NOT NULL UNIQUE,
    machine_id INTEGER NOT NULL UNIQUE REFERENCES machines(id) ON DELETE RESTRICT,
    trace_plan_id INTEGER REFERENCES trace_plans(id) ON DELETE RESTRICT,
    status TEXT NOT NULL DEFAULT 'ASSEMBLED' CHECK (status IN ('ASSEMBLED', 'PASSED', 'HOLD', 'VOID')),
    station_id TEXT NOT NULL,
    station_name TEXT NOT NULL DEFAULT '',
    operator_name TEXT NOT NULL DEFAULT '',
    remarks TEXT NOT NULL DEFAULT '',
    status_reason TEXT NOT NULL DEFAULT '',
    status_updated_at TEXT,
    status_updated_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    completed_by_user_id INTEGER REFERENCES users(id) ON DELETE RESTRICT,
    completed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trace_record_parts (
    trace_record_id INTEGER NOT NULL REFERENCES trace_records(id) ON DELETE CASCADE,
    position INTEGER NOT NULL CHECK (position > 0),
    part_label_id INTEGER NOT NULL UNIQUE REFERENCES part_labels(id) ON DELETE RESTRICT,
    PRIMARY KEY (trace_record_id, position)
);

CREATE TABLE IF NOT EXISTS system_settings (
    setting_key TEXT PRIMARY KEY,
    setting_value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL,
    object_type TEXT NOT NULL,
    object_code TEXT NOT NULL,
    related_object_code TEXT NOT NULL DEFAULT '',
    station_id TEXT NOT NULL DEFAULT '',
    station_name TEXT NOT NULL DEFAULT '',
    operator_name TEXT NOT NULL DEFAULT '',
    actor_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    reason TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL DEFAULT '{}',
    occurred_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_machine_sn ON machines(sn);
CREATE INDEX IF NOT EXISTS idx_user_role_active ON users(role, active);
CREATE INDEX IF NOT EXISTS idx_product_family_active ON product_families(active, product_code);
CREATE INDEX IF NOT EXISTS idx_product_model_family ON product_models(product_family_id, active);
CREATE INDEX IF NOT EXISTS idx_user_product_permission_model
ON user_product_model_permissions(product_model_id, user_id);
CREATE INDEX IF NOT EXISTS idx_user_supplier_permission_supplier
ON user_supplier_permissions(supplier_id, user_id);
CREATE INDEX IF NOT EXISTS idx_part_label_code ON part_labels(identification_code);
CREATE INDEX IF NOT EXISTS idx_part_label_batch_part ON part_label_batches(part_type_id, generated_at DESC);
CREATE INDEX IF NOT EXISTS idx_part_type_supplier ON part_types(supplier_id);
CREATE INDEX IF NOT EXISTS idx_supplier_inventory_part
ON supplier_inventory_batches(part_type_id, active, received_date DESC);
CREATE INDEX IF NOT EXISTS idx_supplier_inventory_available
ON supplier_inventory_batches(active, quantity_available);
CREATE INDEX IF NOT EXISTS idx_supplier_inventory_movement
ON supplier_inventory_movements(inventory_batch_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_trace_completed ON trace_records(completed_at DESC);
CREATE INDEX IF NOT EXISTS idx_trace_part_record ON trace_record_parts(trace_record_id, position);
CREATE INDEX IF NOT EXISTS idx_plan_model_status ON trace_plans(model_code, status);
CREATE INDEX IF NOT EXISTS idx_plan_slot_plan ON trace_plan_slots(trace_plan_id, position);
CREATE INDEX IF NOT EXISTS idx_audit_object ON audit_events(object_code, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_related ON audit_events(related_object_code, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_product_code_set_model
ON product_code_sets(product_model_id, generated_at DESC);
CREATE INDEX IF NOT EXISTS idx_product_code_batch_model
ON product_code_batches(product_model_id, generated_at DESC);
CREATE INDEX IF NOT EXISTS idx_product_code_set_part
ON product_code_set_parts(part_label_id, product_code_set_id);

INSERT OR IGNORE INTO system_settings(setting_key, setting_value, updated_at)
VALUES ('require_quality_release', '0', datetime('now'));
"""


# Incremental migration to user_version 12 (batch-traceability feature).
# Statements are executed individually inside a single BEGIN IMMEDIATE
# transaction so the whole migration is atomic. We deliberately avoid
# ``executescript`` here because it implicitly commits any pending
# transaction, which would defeat the explicit transaction boundary.
BATCH_TRACEABILITY_V12_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS production_batches (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        batch_code TEXT NOT NULL COLLATE NOCASE UNIQUE,
        product_model_id INTEGER NOT NULL REFERENCES product_models(id) ON DELETE RESTRICT,
        trace_plan_id INTEGER NOT NULL REFERENCES trace_plans(id) ON DELETE RESTRICT,
        prefix TEXT NOT NULL COLLATE NOCASE,
        planned_quantity INTEGER NOT NULL CHECK (planned_quantity BETWEEN 1 AND 999999),
        generated_by TEXT NOT NULL DEFAULT '',
        generated_by_user_id INTEGER REFERENCES users(id) ON DELETE RESTRICT,
        generated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS batch_trace_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        production_batch_id INTEGER NOT NULL UNIQUE
            REFERENCES production_batches(id) ON DELETE RESTRICT,
        registered_quantity INTEGER NOT NULL CHECK (registered_quantity >= 1),
        quality_status TEXT NOT NULL DEFAULT 'ASSEMBLED'
            CHECK (quality_status IN ('ASSEMBLED', 'PASSED', 'HOLD')),
        status_reason TEXT NOT NULL DEFAULT '',
        station_id TEXT NOT NULL DEFAULT '',
        station_name TEXT NOT NULL DEFAULT '',
        operator_name TEXT NOT NULL DEFAULT '',
        completed_by_user_id INTEGER REFERENCES users(id) ON DELETE RESTRICT,
        registered_at TEXT NOT NULL,
        status_updated_at TEXT,
        status_updated_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS production_batch_supplier_consumption (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        production_batch_id INTEGER NOT NULL
            REFERENCES production_batches(id) ON DELETE CASCADE,
        supplier_inventory_batch_id INTEGER NOT NULL
            REFERENCES supplier_inventory_batches(id) ON DELETE RESTRICT,
        part_type_id INTEGER NOT NULL REFERENCES part_types(id) ON DELETE RESTRICT,
        quantity_consumed INTEGER NOT NULL CHECK (quantity_consumed > 0),
        UNIQUE (production_batch_id, supplier_inventory_batch_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_production_batch_model
        ON production_batches(product_model_id, generated_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_batch_trace_record_batch
        ON batch_trace_records(production_batch_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_batch_trace_record_status
        ON batch_trace_records(quality_status, registered_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_batch_supplier_consumption_batch
        ON production_batch_supplier_consumption(production_batch_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_batch_supplier_consumption_supplier
        ON production_batch_supplier_consumption(supplier_inventory_batch_id)
    """,
)


# Lingxing integration schema (user_version 12 -> 13).  The role table is
# rebuilt separately because SQLite cannot alter an existing CHECK constraint.
LINGXING_V13_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS purchase_orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        po_no TEXT NOT NULL COLLATE NOCASE UNIQUE,
        supplier_id INTEGER NOT NULL REFERENCES suppliers(id) ON DELETE RESTRICT,
        part_type_id INTEGER NOT NULL REFERENCES part_types(id) ON DELETE RESTRICT,
        quantity INTEGER NOT NULL CHECK (quantity BETWEEN 1 AND 999999),
        sync_status TEXT NOT NULL DEFAULT 'PENDING'
            CHECK (sync_status IN ('PENDING', 'PUSHED', 'FAILED')),
        push_in_progress INTEGER NOT NULL DEFAULT 0 CHECK (push_in_progress IN (0, 1)),
        lingxing_po_id TEXT NOT NULL DEFAULT '',
        lingxing_raw_response TEXT NOT NULL DEFAULT '',
        push_error TEXT NOT NULL DEFAULT '',
        created_by TEXT NOT NULL DEFAULT '',
        created_by_user_id INTEGER REFERENCES users(id) ON DELETE RESTRICT,
        created_at TEXT NOT NULL,
        pushed_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS inbound_receipts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        purchase_order_id INTEGER NOT NULL
            REFERENCES purchase_orders(id) ON DELETE RESTRICT,
        quantity INTEGER NOT NULL CHECK (quantity BETWEEN 1 AND 999999),
        receiver TEXT NOT NULL DEFAULT '',
        receiver_user_id INTEGER REFERENCES users(id) ON DELETE RESTRICT,
        received_at TEXT NOT NULL,
        sync_status TEXT NOT NULL DEFAULT 'PENDING'
            CHECK (sync_status IN ('PENDING', 'PUSHED', 'FAILED')),
        push_in_progress INTEGER NOT NULL DEFAULT 0 CHECK (push_in_progress IN (0, 1)),
        lingxing_inbound_id TEXT NOT NULL DEFAULT '',
        lingxing_raw_response TEXT NOT NULL DEFAULT '',
        push_error TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        pushed_at TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_purchase_order_sync ON purchase_orders(sync_status, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_purchase_order_supplier ON purchase_orders(supplier_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_inbound_receipt_po ON inbound_receipts(purchase_order_id, received_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_inbound_receipt_sync ON inbound_receipts(sync_status, received_at DESC)",
)


# Three-role convergence and the additional warehouse/operations schema
# (user_version 13 -> 14).
THREE_ROLE_V14_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS app_settings (
        setting_key TEXT PRIMARY KEY,
        setting_value TEXT NOT NULL DEFAULT '',
        is_secret INTEGER NOT NULL DEFAULT 0 CHECK (is_secret IN (0, 1)),
        updated_by TEXT NOT NULL DEFAULT '',
        updated_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS production_orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        purchase_order_id INTEGER NOT NULL UNIQUE
            REFERENCES purchase_orders(id) ON DELETE RESTRICT,
        production_batch_id INTEGER NOT NULL UNIQUE
            REFERENCES production_batches(id) ON DELETE RESTRICT,
        created_by TEXT NOT NULL DEFAULT '',
        created_by_user_id INTEGER REFERENCES users(id) ON DELETE RESTRICT,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS product_stock (
        product_model_id INTEGER PRIMARY KEY
            REFERENCES product_models(id) ON DELETE RESTRICT,
        on_hand INTEGER NOT NULL DEFAULT 0 CHECK (on_hand >= 0),
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS inbound_scan_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        production_order_id INTEGER NOT NULL
            REFERENCES production_orders(id) ON DELETE RESTRICT,
        product_model_id INTEGER NOT NULL
            REFERENCES product_models(id) ON DELETE RESTRICT,
        quantity INTEGER NOT NULL CHECK (quantity BETWEEN 1 AND 999999),
        operator_name TEXT NOT NULL DEFAULT '',
        operator_user_id INTEGER REFERENCES users(id) ON DELETE RESTRICT,
        received_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_production_order_po ON production_orders(purchase_order_id)",
    "CREATE INDEX IF NOT EXISTS idx_inbound_scan_order ON inbound_scan_records(production_order_id, received_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_inbound_scan_product ON inbound_scan_records(product_model_id, received_at DESC)",
)


def _rebuild_users_for_roles(
    connection: sqlite3.Connection,
    allowed_roles: tuple[str, ...],
    *,
    migrate_operator: bool = False,
) -> None:
    """Replace only ``users`` so its role CHECK matches the target version.

    Foreign-key enforcement is disabled by the caller for the duration of the
    transaction. Child tables keep referencing the stable table name ``users``;
    all user ids and account data are copied verbatim, with the sole v14 mapping
    ``OPERATOR -> WAREHOUSE``.
    """

    quoted_roles = ", ".join(f"'{role}'" for role in allowed_roles)
    connection.execute("DROP TABLE IF EXISTS users_role_migration")
    connection.execute(
        f"""
        CREATE TABLE users_role_migration (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL COLLATE NOCASE UNIQUE,
            display_name TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK (role IN ({quoted_roles})),
            active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
            must_change_password INTEGER NOT NULL DEFAULT 0 CHECK (must_change_password IN (0, 1)),
            session_version INTEGER NOT NULL DEFAULT 1,
            last_login_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    role_expression = "CASE WHEN role = 'OPERATOR' THEN 'WAREHOUSE' ELSE role END" if migrate_operator else "role"
    connection.execute(
        f"""
        INSERT INTO users_role_migration(
            id, username, display_name, password_hash, role, active,
            must_change_password, session_version, last_login_at, created_at, updated_at
        )
        SELECT id, username, display_name, password_hash, {role_expression}, active,
               must_change_password, session_version, last_login_at, created_at, updated_at
        FROM users
        """
    )
    connection.execute("DROP TABLE users")
    connection.execute("ALTER TABLE users_role_migration RENAME TO users")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_user_role_active ON users(role, active)")


def _rebuild_production_batches_v16(connection: sqlite3.Connection) -> None:
    """Make ``production_batches.trace_plan_id`` nullable (user_version 16).

    A product may be a complete, indivisible finished good with no BOM, so it
    has no ``trace_plans`` row. Production orders for such products still need a
    batch (and its unique QR code), which the original ``NOT NULL`` column
    forbade. The column keeps its foreign key so batches that *do* belong to a
    plan stay referentially intact.

    Foreign-key enforcement is disabled by the caller while the stable table
    name is swapped; ``batch_trace_records``,
    ``production_batch_supplier_consumption``, ``production_orders`` and
    ``supplier_inventory_movements`` keep referencing ``production_batches`` by
    the same name and id. All existing rows are copied verbatim.
    """
    connection.execute("DROP TABLE IF EXISTS production_batches_v16")
    connection.execute(
        """
        CREATE TABLE production_batches_v16 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_code TEXT NOT NULL COLLATE NOCASE UNIQUE,
            product_model_id INTEGER NOT NULL REFERENCES product_models(id) ON DELETE RESTRICT,
            trace_plan_id INTEGER REFERENCES trace_plans(id) ON DELETE RESTRICT,
            prefix TEXT NOT NULL COLLATE NOCASE,
            planned_quantity INTEGER NOT NULL CHECK (planned_quantity BETWEEN 1 AND 999999),
            generated_by TEXT NOT NULL DEFAULT '',
            generated_by_user_id INTEGER REFERENCES users(id) ON DELETE RESTRICT,
            generated_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        INSERT INTO production_batches_v16(
            id, batch_code, product_model_id, trace_plan_id, prefix,
            planned_quantity, generated_by, generated_by_user_id, generated_at
        )
        SELECT id, batch_code, product_model_id, trace_plan_id, prefix,
               planned_quantity, generated_by, generated_by_user_id, generated_at
        FROM production_batches
        """
    )
    connection.execute("DROP TABLE production_batches")
    connection.execute("ALTER TABLE production_batches_v16 RENAME TO production_batches")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_production_batch_model "
        "ON production_batches(product_model_id, generated_at DESC)"
    )


def _rebuild_purchase_orders_v15(connection: sqlite3.Connection) -> None:
    """Relax ``purchase_orders`` for free-form, product-linked purchase orders.

    Operations enter all purchase-order fields directly (matching the export
    template), so the supplier /商品(part) links are no longer mandatory.  This
    rebuild:
      - makes ``supplier_id`` / ``part_type_id`` / ``quantity`` nullable,
      - adds ``product_model_id`` (the linked product) and ``fields_json`` (the
        free-form template values keyed by column name).
    Foreign-key enforcement is disabled by the caller while the stable table
    name is swapped; ``production_orders`` / ``inbound_receipts`` keep
    referencing ``purchase_orders`` by the same name and id. All existing rows
    are copied verbatim; the two new columns take their defaults.
    """
    connection.execute("DROP TABLE IF EXISTS purchase_orders_v15")
    connection.execute(
        """
        CREATE TABLE purchase_orders_v15 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            po_no TEXT NOT NULL COLLATE NOCASE UNIQUE,
            supplier_id INTEGER REFERENCES suppliers(id) ON DELETE RESTRICT,
            part_type_id INTEGER REFERENCES part_types(id) ON DELETE RESTRICT,
            product_model_id INTEGER REFERENCES product_models(id) ON DELETE RESTRICT,
            quantity INTEGER CHECK (quantity IS NULL OR quantity BETWEEN 1 AND 999999),
            fields_json TEXT NOT NULL DEFAULT '{}',
            sync_status TEXT NOT NULL DEFAULT 'PENDING'
                CHECK (sync_status IN ('PENDING', 'PUSHED', 'FAILED')),
            push_in_progress INTEGER NOT NULL DEFAULT 0 CHECK (push_in_progress IN (0, 1)),
            lingxing_po_id TEXT NOT NULL DEFAULT '',
            lingxing_raw_response TEXT NOT NULL DEFAULT '',
            push_error TEXT NOT NULL DEFAULT '',
            created_by TEXT NOT NULL DEFAULT '',
            created_by_user_id INTEGER REFERENCES users(id) ON DELETE RESTRICT,
            created_at TEXT NOT NULL,
            pushed_at TEXT
        )
        """
    )
    connection.execute(
        """
        INSERT INTO purchase_orders_v15(
            id, po_no, supplier_id, part_type_id, quantity, sync_status,
            push_in_progress, lingxing_po_id, lingxing_raw_response, push_error,
            created_by, created_by_user_id, created_at, pushed_at
        )
        SELECT id, po_no, supplier_id, part_type_id, quantity, sync_status,
               push_in_progress, lingxing_po_id, lingxing_raw_response, push_error,
               created_by, created_by_user_id, created_at, pushed_at
        FROM purchase_orders
        """
    )
    connection.execute("DROP TABLE purchase_orders")
    connection.execute("ALTER TABLE purchase_orders_v15 RENAME TO purchase_orders")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_purchase_order_sync "
        "ON purchase_orders(sync_status, created_at DESC)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_purchase_order_supplier "
        "ON purchase_orders(supplier_id, created_at DESC)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_purchase_order_creator "
        "ON purchase_orders(created_by_user_id, created_at DESC)"
    )


def _ensure_column(connection: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def connect_database(path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        str(path),
        timeout=10,
        isolation_level=None,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 10000")
    return connection


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = connect_database(current_app.config["DATABASE"])
    return g.db


def close_db(_error: BaseException | None = None) -> None:
    database = g.pop("db", None)
    if database is not None:
        database.close()


def initialize_database(app) -> None:
    database_path = Path(app.config["DATABASE"])
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = connect_database(database_path)
    try:
        previous_version = connection.execute("PRAGMA user_version").fetchone()[0]
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
        connection.executescript(SCHEMA)
        # Incremental migrations preserve databases created by earlier standalone
        # releases. SQLite's CREATE TABLE IF NOT EXISTS does not add new columns.
        _ensure_column(
            connection,
            "machines",
            "trace_plan_id",
            "INTEGER REFERENCES trace_plans(id) ON DELETE RESTRICT",
        )
        _ensure_column(
            connection,
            "machines",
            "product_model_id",
            "INTEGER REFERENCES product_models(id) ON DELETE RESTRICT",
        )
        _ensure_column(
            connection,
            "trace_plans",
            "product_model_id",
            "INTEGER REFERENCES product_models(id) ON DELETE RESTRICT",
        )
        _ensure_column(connection, "product_models", "bluetooth_service_uuid", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(connection, "product_models", "bluetooth_notify_uuid", "TEXT NOT NULL DEFAULT ''")
        # Records which operations/admin account created the product so the
        # product catalog can be scoped (operations see only their own; admins
        # see every product plus its creator).
        _ensure_column(
            connection,
            "product_models",
            "created_by_user_id",
            "INTEGER REFERENCES users(id) ON DELETE SET NULL",
        )
        _ensure_column(
            connection,
            "scan_sessions",
            "trace_plan_id",
            "INTEGER REFERENCES trace_plans(id) ON DELETE RESTRICT",
        )
        _ensure_column(
            connection,
            "scan_sessions",
            "user_id",
            "INTEGER REFERENCES users(id) ON DELETE RESTRICT",
        )
        _ensure_column(
            connection,
            "trace_records",
            "trace_plan_id",
            "INTEGER REFERENCES trace_plans(id) ON DELETE RESTRICT",
        )
        _ensure_column(
            connection,
            "trace_records",
            "status",
            "TEXT NOT NULL DEFAULT 'ASSEMBLED'",
        )
        _ensure_column(
            connection,
            "trace_records",
            "completed_by_user_id",
            "INTEGER REFERENCES users(id) ON DELETE RESTRICT",
        )
        _ensure_column(connection, "trace_records", "remarks", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(connection, "trace_records", "status_reason", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(connection, "trace_records", "status_updated_at", "TEXT")
        _ensure_column(
            connection,
            "trace_records",
            "status_updated_by_user_id",
            "INTEGER REFERENCES users(id) ON DELETE SET NULL",
        )
        _ensure_column(
            connection,
            "part_labels",
            "label_batch_id",
            "INTEGER REFERENCES part_label_batches(id) ON DELETE RESTRICT",
        )
        _ensure_column(connection, "part_labels", "source_serial_no", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(connection, "part_labels", "production_date", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(connection, "part_labels", "inspection_status", "TEXT NOT NULL DEFAULT 'PENDING'")
        _ensure_column(connection, "part_labels", "remarks", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(connection, "part_labels", "entered_by", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(connection, "part_labels", "entered_at", "TEXT")
        _ensure_column(connection, "part_labels", "updated_at", "TEXT")
        _ensure_column(
            connection,
            "part_labels",
            "entered_by_user_id",
            "INTEGER REFERENCES users(id) ON DELETE RESTRICT",
        )
        _ensure_column(
            connection,
            "part_label_batches",
            "generated_by_user_id",
            "INTEGER REFERENCES users(id) ON DELETE RESTRICT",
        )
        _ensure_column(
            connection,
            "audit_events",
            "actor_user_id",
            "INTEGER REFERENCES users(id) ON DELETE SET NULL",
        )
        _ensure_column(connection, "users", "session_version", "INTEGER NOT NULL DEFAULT 1")
        _ensure_column(connection, "suppliers", "active", "INTEGER NOT NULL DEFAULT 1")
        _ensure_column(connection, "suppliers", "updated_at", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(connection, "part_types", "category_code", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(connection, "part_types", "category_name", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(connection, "part_types", "minimum_stock", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(connection, "part_types", "updated_at", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(
            connection,
            "trace_plan_slots",
            "supplier_inventory_batch_id",
            "INTEGER REFERENCES supplier_inventory_batches(id) ON DELETE RESTRICT",
        )
        _ensure_column(
            connection,
            "product_code_sets",
            "generation_batch_id",
            "INTEGER REFERENCES product_code_batches(id) ON DELETE RESTRICT",
        )
        # The configurable per-unit "默认部件数量" (required_part_count) setting was
        # removed; drop the stale row from databases created by earlier releases
        # so no orphaned setting data remains.
        connection.execute(
            "DELETE FROM system_settings WHERE setting_key = 'required_part_count'"
        )
        connection.execute(
            "UPDATE suppliers SET updated_at = created_at WHERE updated_at = ''"
        )
        connection.execute(
            "UPDATE part_types SET updated_at = created_at WHERE updated_at = ''"
        )
        connection.execute(
            "UPDATE part_types SET category_code = part_code WHERE category_code = ''"
        )
        connection.execute(
            "UPDATE part_types SET category_name = name WHERE category_name = ''"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_part_type_category ON part_types(category_code, active)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_part_type_minimum_stock "
            "ON part_types(active, minimum_stock)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_trace_status_completed "
            "ON trace_records(status, completed_at DESC)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_part_label_batch ON part_labels(label_batch_id)"
        )
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_part_label_source_serial "
            "ON part_labels(source_serial_no) WHERE source_serial_no <> ''"
        )
        timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
        connection.execute(
            """
            INSERT OR IGNORE INTO product_families(
                product_code, name, description, active, created_at, updated_at
            ) VALUES ('TREADMILL', '走步机', '走步机及律动走步机产品', 1, ?, ?)
            """,
            (timestamp, timestamp),
        )
        treadmill_family = connection.execute(
            "SELECT id FROM product_families WHERE product_code = 'TREADMILL' COLLATE NOCASE"
        ).fetchone()
        connection.execute(
            """
            INSERT OR IGNORE INTO product_models(
                product_family_id, model_code, name, serial_prefix,
                identity_source, bluetooth_name_prefix,
                bluetooth_service_uuid, bluetooth_notify_uuid,
                active, created_at, updated_at
            ) VALUES (
                ?, 'TW-04', 'TW-04 走步机', 'TW-04', 'BLUETOOTH', 'TW-04',
                '0000fff0-0000-1000-8000-00805f9b34fb',
                '0000fff1-0000-1000-8000-00805f9b34fb', 1, ?, ?
            )
            """,
            (treadmill_family["id"], timestamp, timestamp),
        )
        connection.execute(
            """
            UPDATE product_models
            SET bluetooth_service_uuid = CASE
                    WHEN bluetooth_service_uuid = ''
                    THEN '0000fff0-0000-1000-8000-00805f9b34fb'
                    ELSE bluetooth_service_uuid END,
                bluetooth_notify_uuid = CASE
                    WHEN bluetooth_notify_uuid = ''
                    THEN '0000fff1-0000-1000-8000-00805f9b34fb'
                    ELSE bluetooth_notify_uuid END
            WHERE model_code = 'TW-04' COLLATE NOCASE
              AND identity_source = 'BLUETOOTH'
            """
        )

        # Preserve arbitrary model codes from older databases. Unknown models are
        # grouped under a neutral legacy family and can later be reassigned from
        # the admin UI without rewriting historical finished-product rows.
        legacy_family_id: int | None = None
        legacy_models = connection.execute(
            """
            SELECT model_code FROM trace_plans WHERE model_code <> ''
            UNION
            SELECT model AS model_code FROM machines WHERE model <> ''
            """
        ).fetchall()
        for legacy in legacy_models:
            model_code = legacy["model_code"].strip()
            if not model_code:
                continue
            existing_model = connection.execute(
                "SELECT id FROM product_models WHERE model_code = ? COLLATE NOCASE",
                (model_code,),
            ).fetchone()
            if existing_model:
                continue
            if legacy_family_id is None:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO product_families(
                        product_code, name, description, active, created_at, updated_at
                    ) VALUES ('LEGACY', '历史产品', '由旧版本数据自动迁移', 1, ?, ?)
                    """,
                    (timestamp, timestamp),
                )
                legacy_family_id = connection.execute(
                    "SELECT id FROM product_families WHERE product_code = 'LEGACY' COLLATE NOCASE"
                ).fetchone()["id"]
            connection.execute(
                """
                INSERT INTO product_models(
                    product_family_id, model_code, name, serial_prefix,
                    identity_source, bluetooth_name_prefix,
                    bluetooth_service_uuid, bluetooth_notify_uuid,
                    active, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'SCANNER', '', '', '', 1, ?, ?)
                """,
                (legacy_family_id, model_code, model_code, model_code, timestamp, timestamp),
            )

        connection.execute(
            """
            UPDATE machines
            SET product_model_id = (
                SELECT pm.id FROM product_models pm
                WHERE pm.model_code = machines.model COLLATE NOCASE
            )
            WHERE product_model_id IS NULL
            """
        )
        connection.execute(
            """
            UPDATE trace_plans
            SET product_model_id = (
                SELECT pm.id FROM product_models pm
                WHERE pm.model_code = trace_plans.model_code COLLATE NOCASE
            )
            WHERE product_model_id IS NULL
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_machine_product_model ON machines(product_model_id, created_at DESC)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_plan_product_model_status "
            "ON trace_plans(product_model_id, status)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_scan_session_user ON scan_sessions(user_id, updated_at DESC)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_product_code_set_batch "
            "ON product_code_sets(generation_batch_id, daily_sequence)"
        )
        legacy_code_groups = connection.execute(
            """
            SELECT product_model_id, trace_plan_id, prefix, date_code, generated_at,
                   COUNT(*) AS quantity, MIN(daily_sequence) AS start_sequence,
                   MAX(daily_sequence) AS end_sequence, MIN(id) AS first_set_id,
                   MIN(generated_by_user_id) AS generated_by_user_id
            FROM product_code_sets
            WHERE generation_batch_id IS NULL
            GROUP BY product_model_id, trace_plan_id, prefix, date_code, generated_at
            ORDER BY first_set_id
            """
        ).fetchall()
        for legacy_group in legacy_code_groups:
            batch_code = f"LEGACY-{legacy_group['first_set_id']:08d}"
            connection.execute(
                """
                INSERT OR IGNORE INTO product_code_batches(
                    batch_code, product_model_id, trace_plan_id, prefix, date_code,
                    quantity, start_sequence, end_sequence,
                    generated_by_user_id, generated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    batch_code,
                    legacy_group["product_model_id"],
                    legacy_group["trace_plan_id"],
                    legacy_group["prefix"],
                    legacy_group["date_code"],
                    legacy_group["quantity"],
                    legacy_group["start_sequence"],
                    legacy_group["end_sequence"],
                    legacy_group["generated_by_user_id"],
                    legacy_group["generated_at"],
                ),
            )
            batch_id = connection.execute(
                "SELECT id FROM product_code_batches WHERE batch_code = ?",
                (batch_code,),
            ).fetchone()["id"]
            connection.execute(
                """
                UPDATE product_code_sets SET generation_batch_id = ?
                WHERE generation_batch_id IS NULL
                  AND product_model_id = ? AND trace_plan_id = ?
                  AND prefix = ? COLLATE NOCASE AND date_code = ? AND generated_at = ?
                """,
                (
                    batch_id,
                    legacy_group["product_model_id"],
                    legacy_group["trace_plan_id"],
                    legacy_group["prefix"],
                    legacy_group["date_code"],
                    legacy_group["generated_at"],
                ),
            )
        if previous_version < 8:
            # Existing operator accounts previously had unrestricted data-entry
            # access. Grant their current catalog once during upgrade so the new
            # least-privilege model does not interrupt an active factory rollout.
            connection.execute(
                """
                INSERT OR IGNORE INTO user_product_model_permissions(user_id, product_model_id)
                SELECT u.id, pm.id
                FROM users u CROSS JOIN product_models pm
                WHERE u.role = 'OPERATOR' AND pm.active = 1
                """
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO user_supplier_permissions(user_id, supplier_id)
                SELECT u.id, s.id
                FROM users u CROSS JOIN suppliers s
                WHERE u.role = 'OPERATOR'
                """
            )
        # Baseline for databases created by earlier standalone releases. Never
        # downgrade a database that already reached a later feature version,
        # otherwise the incremental migrations below would run again instead of
        # skipping (Req 8.5).
        if previous_version < 11:
            connection.execute("PRAGMA user_version = 11")

        # Incremental migration to user_version 12 (batch traceability). Only
        # adds new tables, indexes and a single column; never modifies or drops
        # existing structures. The whole migration runs inside one explicit
        # BEGIN IMMEDIATE transaction (including the user_version bump) so a
        # failure at any step rolls back both structure and version, leaving the
        # database exactly as it was before and aborting startup (Req 8.4, 8.6).
        current_version = connection.execute("PRAGMA user_version").fetchone()[0]
        if current_version < 12:
            connection.execute("BEGIN IMMEDIATE")
            try:
                for statement in BATCH_TRACEABILITY_V12_STATEMENTS:
                    connection.execute(statement)
                _ensure_column(
                    connection,
                    "supplier_inventory_movements",
                    "production_batch_id",
                    "INTEGER REFERENCES production_batches(id) ON DELETE RESTRICT",
                )
                connection.execute("PRAGMA user_version = 12")
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

        # v13 adds the local purchase/inbound integration structures and
        # temporarily accepts both the historical OPERATOR role and the two
        # target business roles.  Rebuilding users is transactional; foreign
        # keys are disabled only while the stable table name is replaced.
        current_version = connection.execute("PRAGMA user_version").fetchone()[0]
        if current_version < 13:
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute("BEGIN IMMEDIATE")
            try:
                for statement in LINGXING_V13_STATEMENTS:
                    connection.execute(statement)
                _rebuild_users_for_roles(
                    connection,
                    ("ADMIN", "OPERATOR", "WAREHOUSE", "OPERATIONS"),
                )
                connection.execute("PRAGMA user_version = 13")
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
            finally:
                connection.execute("PRAGMA foreign_keys = ON")
            violations = connection.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise sqlite3.IntegrityError(
                    f"v13 migration produced foreign-key violations: {violations!r}"
                )

        # v14 converges accounts to exactly ADMIN / WAREHOUSE / OPERATIONS and
        # adds the settings, production-order and finished-goods stock tables.
        current_version = connection.execute("PRAGMA user_version").fetchone()[0]
        if current_version < 14:
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute("BEGIN IMMEDIATE")
            try:
                for statement in THREE_ROLE_V14_STATEMENTS:
                    connection.execute(statement)
                _rebuild_users_for_roles(
                    connection,
                    ("ADMIN", "WAREHOUSE", "OPERATIONS"),
                    migrate_operator=True,
                )
                connection.execute("PRAGMA user_version = 14")
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
            finally:
                connection.execute("PRAGMA foreign_keys = ON")
            violations = connection.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise sqlite3.IntegrityError(
                    f"v14 migration produced foreign-key violations: {violations!r}"
                )

        # v15 relaxes purchase_orders for free-form, product-linked purchase
        # orders (supplier / part become optional; product_model_id and
        # fields_json are added). Rebuilding needs FK enforcement off while the
        # stable table name is swapped, mirroring the v13/v14 pattern.
        current_version = connection.execute("PRAGMA user_version").fetchone()[0]
        if current_version < 15:
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute("BEGIN IMMEDIATE")
            try:
                _rebuild_purchase_orders_v15(connection)
                connection.execute("PRAGMA user_version = 15")
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
            finally:
                connection.execute("PRAGMA foreign_keys = ON")
            violations = connection.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise sqlite3.IntegrityError(
                    f"v15 migration produced foreign-key violations: {violations!r}"
                )

        # v16 supports products that are complete, indivisible finished goods:
        # ``production_batches.trace_plan_id`` becomes nullable so a production
        # order can be created for a product without a BOM, and
        # ``product_models.attributes_json`` stores the extended product profile
        # (SKU / 品名 / 采购 / 报关 / 物流 fields) keyed by column name.
        current_version = connection.execute("PRAGMA user_version").fetchone()[0]
        if current_version < 16:
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute("BEGIN IMMEDIATE")
            try:
                _rebuild_production_batches_v16(connection)
                _ensure_column(
                    connection,
                    "product_models",
                    "attributes_json",
                    "TEXT NOT NULL DEFAULT '{}'",
                )
                connection.execute("PRAGMA user_version = 16")
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
            finally:
                connection.execute("PRAGMA foreign_keys = ON")
            violations = connection.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise sqlite3.IntegrityError(
                    f"v16 migration produced foreign-key violations: {violations!r}"
                )

        # v17 supports the warehouse batch production-order flow and per-product
        # inventory sync:
        #   - ``production_orders.is_external`` flags an order whose production
        #     is externally procured (外采) — the trace code is still minted so
        #     inbound / stock stays uniform, but the order carries the 外采 label.
        #   - ``product_stock_sync`` records the Lingxing inventory-sync status of
        #     each product model so operations can see per-product sync state and
        #     push a selected subset (design: inventory sync per product).
        # This migration is purely additive (a new column plus a new table with
        # no existing rows to rebuild), so foreign-key enforcement is left on.
        # ``production_orders`` is created by the v14 statements, so the column is
        # added here — after that block has run on both fresh and upgraded DBs.
        current_version = connection.execute("PRAGMA user_version").fetchone()[0]
        if current_version < 17:
            connection.execute("BEGIN IMMEDIATE")
            try:
                _ensure_column(
                    connection,
                    "production_orders",
                    "is_external",
                    "INTEGER NOT NULL DEFAULT 0 CHECK (is_external IN (0, 1))",
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS product_stock_sync (
                        product_model_id INTEGER PRIMARY KEY
                            REFERENCES product_models(id) ON DELETE CASCADE,
                        sync_status TEXT NOT NULL DEFAULT 'PENDING'
                            CHECK (sync_status IN ('PENDING', 'PUSHED', 'FAILED')),
                        synced_quantity INTEGER NOT NULL DEFAULT 0,
                        lingxing_id TEXT NOT NULL DEFAULT '',
                        push_error TEXT NOT NULL DEFAULT '',
                        synced_at TEXT
                    )
                    """
                )
                connection.execute("PRAGMA user_version = 17")
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
            violations = connection.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise sqlite3.IntegrityError(
                    f"v17 migration produced foreign-key violations: {violations!r}"
                )

        # v18 adds indexes on hot foreign-key / filter columns that earlier
        # schema versions left unindexed. The base schema and the v12/v13
        # migrations already cover most joins; these fill the remaining gaps for
        # the products list (active trace plan by product), purchase-order ↔
        # product linkage, and the read-only legacy trace lookups. Creating an
        # index cannot violate foreign keys, so this block is a plain additive
        # migration with no rebuild. ``purchase_orders.product_model_id`` is
        # added by the v15 rebuild, which runs before this block.
        current_version = connection.execute("PRAGMA user_version").fetchone()[0]
        if current_version < 18:
            connection.execute("BEGIN IMMEDIATE")
            try:
                for statement in (
                    "CREATE INDEX IF NOT EXISTS idx_trace_plan_product_model "
                    "ON trace_plans(product_model_id, status)",
                    "CREATE INDEX IF NOT EXISTS idx_purchase_order_product_model "
                    "ON purchase_orders(product_model_id)",
                    "CREATE INDEX IF NOT EXISTS idx_machine_product_model "
                    "ON machines(product_model_id)",
                    "CREATE INDEX IF NOT EXISTS idx_trace_record_completed_by "
                    "ON trace_records(completed_by_user_id)",
                    "CREATE INDEX IF NOT EXISTS idx_part_label_part_type "
                    "ON part_labels(part_type_id)",
                    "CREATE INDEX IF NOT EXISTS idx_product_code_set_generation_batch "
                    "ON product_code_sets(generation_batch_id)",
                ):
                    connection.execute(statement)
                connection.execute("PRAGMA user_version = 18")
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

        # v19 records when a Lingxing push took its in-progress guard. Without it a
        # process that dies mid-push leaves ``push_in_progress`` at 1 forever and
        # that order can never be pushed again (it answers 409 indefinitely). With
        # the start time the next attempt can detect an abandoned guard and take
        # over. Purely additive: ``purchase_orders`` exists from the v15 rebuild and
        # ``inbound_receipts`` from v13, both before this block.
        current_version = connection.execute("PRAGMA user_version").fetchone()[0]
        if current_version < 19:
            connection.execute("BEGIN IMMEDIATE")
            try:
                _ensure_column(connection, "purchase_orders", "push_started_at", "TEXT")
                _ensure_column(connection, "inbound_receipts", "push_started_at", "TEXT")
                connection.execute("PRAGMA user_version = 19")
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

        _clear_abandoned_guards(connection)
    finally:
        connection.close()

    app.teardown_appcontext(close_db)


def _clear_abandoned_guards(connection: sqlite3.Connection) -> None:
    """Release in-progress guards left behind by a process that died mid-push.

    Runs on every startup. A push / sync guard can only be held by a live request,
    and no request can be in flight while the database is being initialized, so any
    flag still set here belongs to a process that never finished (restart, kill,
    power loss). Without this a crash would strand the flag and answer 409
    "正在进行中" forever, needing manual DB surgery. This is the deterministic
    counterpart to the timeout-based takeover in the route layer, which covers a
    worker that is hung but still alive.
    """
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute(
            "UPDATE purchase_orders SET push_in_progress = 0 WHERE push_in_progress <> 0"
        )
        connection.execute(
            "UPDATE inbound_receipts SET push_in_progress = 0 WHERE push_in_progress <> 0"
        )
        connection.execute(
            "UPDATE app_settings SET setting_value = '0' "
            "WHERE setting_key = 'inventory_sync.in_progress' AND setting_value <> '0'"
        )
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise
