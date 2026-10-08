"""Supplier inventory batches (供应批次) — HTTP layer.

The administrator's record of supplier stock: receiving a batch of parts,
correcting it, its movement ledger, and the forward trace ("which production
batches consumed this supplier batch?"). Receiving and correcting a batch record
the change in ``supplier_inventory_movements`` in the same transaction, so the
ledger explains the balance.

The deduction that consumes this stock when a production batch is generated
lives in ``traceability/inventory.py``; the forward-trace query lives in
``traceability/production.py``, next to its mirror image, the reverse trace.

Guards are called in the route, not inside a domain call: the permission matrix
is built by following the call graph of the file that declares the route, so a
guard moved into another module would disappear from the security document.
"""

from __future__ import annotations

from typing import Any

from flask import Blueprint, current_app, request

from traceability.audit_events import record_audit_event
from traceability.auth import current_actor_id, require_admin
from traceability.codes import normalize_entity_code
from traceability.db import get_db
from traceability.errors import ApiError
from traceability.pagination import parse_list_window
from traceability.production import supplier_inventory_batch_forward_trace
from traceability.responses import success
from traceability.serializers import supplier_inventory_batch_dict
from traceability.validators import clean_text, parse_bool

supplier_inventory_bp = Blueprint("supplier_inventory", __name__)


@supplier_inventory_bp.get("/api/supplier-inventory-batches")
def list_supplier_inventory_batches():
    require_admin()
    database = get_db()
    clauses: list[str] = []
    parameters: list[Any] = []
    for argument, column, label in (
        (request.args.get("supplierId"), "s.id", "供应商"),
        (request.args.get("partTypeId"), "pt.id", "部件"),
    ):
        if argument in (None, ""):
            continue
        try:
            parameters.append(int(argument))
        except (TypeError, ValueError) as error:
            raise ApiError(f"{label}参数无效") from error
        clauses.append(f"{column} = ?")
    if request.args.get("available") == "1":
        clauses.extend(["sib.active = 1", "sib.quantity_available > 0", "pt.active = 1", "s.active = 1"])
    where_clause = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    # Bounded: one row per incoming batch and the front end renders every row it
    # is given. COUNT(*) OVER () is evaluated before LIMIT, so the total comes
    # back with the window instead of costing a second scan with the same filters.
    window = parse_list_window()
    rows = database.execute(
        f"""
        SELECT sib.*, pt.part_code, pt.name AS part_name, pt.specification,
               s.id AS supplier_id, s.supplier_code, s.name AS supplier_name,
               COUNT(*) OVER () AS total_count
        FROM supplier_inventory_batches sib
        JOIN part_types pt ON pt.id = sib.part_type_id
        JOIN suppliers s ON s.id = pt.supplier_id
        {where_clause}
        ORDER BY s.name, pt.name, sib.active DESC, sib.received_date DESC, sib.id DESC
        LIMIT ? OFFSET ?
        """,
        [*parameters, window.limit, window.offset],
    ).fetchall()
    total = rows[0]["total_count"] if rows else 0
    return window.apply(
        success([supplier_inventory_batch_dict(row) for row in rows]), total
    )


@supplier_inventory_bp.post("/api/supplier-inventory-batches")
def create_supplier_inventory_batch():
    require_admin()
    payload = request.get_json(silent=True) or {}
    try:
        part_type_id = int(payload.get("partTypeId"))
        quantity = int(payload.get("quantity"))
    except (TypeError, ValueError) as error:
        raise ApiError("请选择部件并填写到货数量") from error
    if not 1 <= quantity <= 10_000_000:
        raise ApiError("到货数量需为 1-10000000")
    batch_no = normalize_entity_code(payload.get("batchNo"), "供应批次号")
    production_date = clean_text(payload.get("productionDate"), "生产日期", max_length=20)
    received_date = clean_text(
        payload.get("receivedDate"), "到货日期", required=True, max_length=20
    )
    remarks = clean_text(payload.get("remarks"), "备注", max_length=200)
    database = get_db()
    part = database.execute(
        """
        SELECT pt.*, s.active AS supplier_active
        FROM part_types pt JOIN suppliers s ON s.id = pt.supplier_id
        WHERE pt.id = ?
        """,
        (part_type_id,),
    ).fetchone()
    if not part:
        raise ApiError("供应部件不存在", 404)
    if not part["active"] or not part["supplier_active"]:
        raise ApiError("供应商或部件已停用", 409)
    timestamp = current_app.config["NOW_PROVIDER"]()
    actor_user_id = current_actor_id()
    database.execute("BEGIN IMMEDIATE")
    try:
        cursor = database.execute(
            """
            INSERT INTO supplier_inventory_batches(
                part_type_id, batch_no, quantity_received, quantity_available,
                production_date, received_date, remarks, active,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (
                part_type_id, batch_no, quantity, quantity,
                production_date, received_date, remarks, timestamp, timestamp,
            ),
        )
        database.execute(
            """
            INSERT INTO supplier_inventory_movements(
                inventory_batch_id, movement_type, quantity_change, balance_after,
                actor_user_id, reason, occurred_at
            ) VALUES (?, 'RECEIPT', ?, ?, ?, ?, ?)
            """,
            (cursor.lastrowid, quantity, quantity, actor_user_id, "供应批次入库", timestamp),
        )
        record_audit_event(
            database,
            "SUPPLIER_BATCH_RECEIVED",
            "SUPPLIER_INVENTORY_BATCH",
            batch_no,
            related_object_code=part["part_code"],
            payload={"partTypeId": part_type_id, "quantity": quantity},
            occurred_at=timestamp,
        )
        database.commit()
    except Exception:
        database.rollback()
        raise
    row = database.execute(
        """
        SELECT sib.*, pt.part_code, pt.name AS part_name, pt.specification,
               s.id AS supplier_id, s.supplier_code, s.name AS supplier_name
        FROM supplier_inventory_batches sib
        JOIN part_types pt ON pt.id = sib.part_type_id
        JOIN suppliers s ON s.id = pt.supplier_id
        WHERE sib.id = ?
        """,
        (cursor.lastrowid,),
    ).fetchone()
    return success(supplier_inventory_batch_dict(row), 201)


@supplier_inventory_bp.put("/api/supplier-inventory-batches/<int:inventory_batch_id>")
def update_supplier_inventory_batch(inventory_batch_id: int):
    require_admin()
    database = get_db()
    current = database.execute(
        """
        SELECT sib.*, pt.part_code, pt.name AS part_name
        FROM supplier_inventory_batches sib
        JOIN part_types pt ON pt.id = sib.part_type_id
        WHERE sib.id = ?
        """,
        (inventory_batch_id,),
    ).fetchone()
    if not current:
        raise ApiError("供应批次不存在", 404)
    payload = request.get_json(silent=True) or {}
    batch_no = normalize_entity_code(
        payload.get("batchNo", current["batch_no"]), "供应批次号"
    )
    try:
        quantity_received = int(payload.get("quantity", current["quantity_received"]))
    except (TypeError, ValueError) as error:
        raise ApiError("到货数量无效") from error
    quantity_consumed = current["quantity_received"] - current["quantity_available"]
    if quantity_received < quantity_consumed:
        raise ApiError(f"到货数量不能小于已领用数量 {quantity_consumed}", 409)
    if quantity_received > 10_000_000:
        raise ApiError("到货数量不能超过 10000000")
    production_date = clean_text(
        payload.get("productionDate", current["production_date"]), "生产日期", max_length=20
    )
    received_date = clean_text(
        payload.get("receivedDate", current["received_date"]),
        "到货日期", required=True, max_length=20,
    )
    remarks = clean_text(payload.get("remarks", current["remarks"]), "备注", max_length=200)
    active = parse_bool(payload.get("active"), bool(current["active"]))
    quantity_available = quantity_received - quantity_consumed
    quantity_change = quantity_available - current["quantity_available"]
    timestamp = current_app.config["NOW_PROVIDER"]()
    actor_user_id = current_actor_id()
    database.execute("BEGIN IMMEDIATE")
    try:
        database.execute(
            """
            UPDATE supplier_inventory_batches
            SET batch_no = ?, quantity_received = ?, quantity_available = ?,
                production_date = ?, received_date = ?, remarks = ?,
                active = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                batch_no, quantity_received, quantity_available,
                production_date, received_date, remarks,
                int(active), timestamp, inventory_batch_id,
            ),
        )
        if quantity_change:
            database.execute(
                """
                INSERT INTO supplier_inventory_movements(
                    inventory_batch_id, movement_type, quantity_change,
                    balance_after, actor_user_id, reason, occurred_at
                ) VALUES (?, 'ADJUSTMENT', ?, ?, ?, ?, ?)
                """,
                (
                    inventory_batch_id, quantity_change, quantity_available,
                    actor_user_id, "管理员修改批次数量", timestamp,
                ),
            )
        record_audit_event(
            database,
            "SUPPLIER_BATCH_UPDATED",
            "SUPPLIER_INVENTORY_BATCH",
            batch_no,
            related_object_code=current["part_code"],
            payload={
                "quantityReceived": quantity_received,
                "quantityAvailable": quantity_available,
                "active": active,
            },
            occurred_at=timestamp,
        )
        database.commit()
    except Exception:
        database.rollback()
        raise
    row = database.execute(
        """
        SELECT sib.*, pt.part_code, pt.name AS part_name, pt.specification,
               s.id AS supplier_id, s.supplier_code, s.name AS supplier_name
        FROM supplier_inventory_batches sib
        JOIN part_types pt ON pt.id = sib.part_type_id
        JOIN suppliers s ON s.id = pt.supplier_id
        WHERE sib.id = ?
        """,
        (inventory_batch_id,),
    ).fetchone()
    return success(supplier_inventory_batch_dict(row))


@supplier_inventory_bp.get("/api/supplier-inventory-batches/<int:inventory_batch_id>/movements")
def list_supplier_inventory_movements(inventory_batch_id: int):
    require_admin()
    rows = get_db().execute(
        """
        SELECT sim.*, COALESCE(u.display_name, u.username, '') AS actor_name,
               pm.name AS product_name, pcb.batch_code AS product_code_batch
        FROM supplier_inventory_movements sim
        LEFT JOIN users u ON u.id = sim.actor_user_id
        LEFT JOIN product_models pm ON pm.id = sim.product_model_id
        LEFT JOIN product_code_batches pcb ON pcb.id = sim.product_code_batch_id
        WHERE sim.inventory_batch_id = ?
        ORDER BY sim.occurred_at DESC, sim.id DESC
        """,
        (inventory_batch_id,),
    ).fetchall()
    return success(
        [
            {
                "id": row["id"],
                "movementType": row["movement_type"],
                "quantityChange": row["quantity_change"],
                "balanceAfter": row["balance_after"],
                "productName": row["product_name"],
                "productCodeBatch": row["product_code_batch"],
                "actorName": row["actor_name"],
                "reason": row["reason"],
                "occurredAt": row["occurred_at"],
            }
            for row in rows
        ]
    )


@supplier_inventory_bp.get("/api/supplier-inventory-batches/<int:inventory_batch_id>/forward-trace")
def supplier_inventory_batch_forward_trace_view(inventory_batch_id: int):
    # Admin-only forward trace: list every production batch that consumed
    # this supplier batch, each carrying the batch code value, product
    # model, generation time and consumed quantity (Requirement 5.5). A
    # supplier batch with no consumption returns an empty list rather than
    # an error (Requirement 5.6); a missing supplier batch returns 404.
    require_admin()
    database = get_db()
    batch = database.execute(
        "SELECT id FROM supplier_inventory_batches WHERE id = ?",
        (inventory_batch_id,),
    ).fetchone()
    if not batch:
        raise ApiError("供应批次不存在", 404)
    return success(
        supplier_inventory_batch_forward_trace(database, inventory_batch_id)
    )
