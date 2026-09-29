"""System status — HTTP layer.

Three read-only views of the running system:

* ``GET /api/health`` is the liveness probe the launch scripts poll. It is
  public: the authentication gate in ``traceability/auth.py`` exempts
  ``/api/health`` by path, so it answers without a session wherever the route
  is declared.
* ``GET /api/dashboard`` is the administrator's board: stock alerts, both
  quality queues (legacy per-unit records and batch registrations), the flow
  backlogs and the latest audit activity.
* ``GET /api/audit-events`` is the searchable audit log.

Guards are called in the route, not inside a domain call: the permission matrix
is built by following the call graph of the file that declares the route, so a
guard moved into another module would disappear from the security document.
"""

from __future__ import annotations

from datetime import datetime

from flask import Blueprint, current_app, request

from traceability.auth import require_admin
from traceability.db import get_db
from traceability.errors import ApiError
from traceability.responses import success
from traceability.serializers import (
    audit_event_dict,
    batch_trace_record_dict,
    part_type_dict,
)
from traceability.trace_records import fetch_records
from traceability.validators import clean_text, now_iso

dashboard_bp = Blueprint("dashboard", __name__)


@dashboard_bp.get("/api/health")
def health():
    return success({"status": "ok", "time": now_iso()})


@dashboard_bp.get("/api/dashboard")
def dashboard():
    require_admin()
    database = get_db()
    # Derive "today" from the same clock the system stamps records with, so the
    # 今日 metrics agree with the stored timestamps instead of the wall clock.
    timestamp = current_app.config["NOW_PROVIDER"]()
    try:
        today = datetime.fromisoformat(timestamp.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        today = datetime.now().astimezone().date().isoformat()
    stock_rows = database.execute(
        """
        SELECT pt.*, s.supplier_code, s.name AS supplier_name,
               COUNT(DISTINCT CASE WHEN sib.active = 1 THEN sib.id END) AS batch_count,
               COALESCE(SUM(CASE WHEN sib.active = 1 THEN sib.quantity_available ELSE 0 END), 0)
                   AS quantity_available
        FROM part_types pt
        JOIN suppliers s ON s.id = pt.supplier_id
        LEFT JOIN supplier_inventory_batches sib ON sib.part_type_id = pt.id
        WHERE pt.active = 1 AND s.active = 1
        GROUP BY pt.id
        HAVING quantity_available = 0
            OR (pt.minimum_stock > 0 AND quantity_available <= pt.minimum_stock)
        ORDER BY CASE WHEN quantity_available = 0 THEN 0 ELSE 1 END,
                 quantity_available ASC, pt.updated_at DESC
        """
    ).fetchall()
    quality_queue = fetch_records(
        database, limit=8, statuses=["HOLD", "ASSEMBLED"]
    )
    # The batch model is where current work happens (走步机 moved to batch
    # traceability), so the board also carries the batch registration queue
    # awaiting quality handling. The legacy per-unit queue above is kept for
    # historical data.
    batch_quality_rows = database.execute(
        """
        SELECT r.id AS id, r.production_batch_id AS production_batch_id,
               r.registered_quantity AS registered_quantity,
               r.quality_status AS quality_status,
               r.operator_name AS operator_name,
               r.registered_at AS registered_at,
               b.batch_code AS batch_code, b.prefix AS prefix,
               b.generated_at AS generated_at,
               b.product_model_id AS product_model_id,
               pm.model_code AS product_model_code, pm.name AS product_name
        FROM batch_trace_records r
        JOIN production_batches b ON b.id = r.production_batch_id
        LEFT JOIN product_models pm ON pm.id = b.product_model_id
        WHERE r.quality_status IN ('ASSEMBLED', 'HOLD')
        ORDER BY r.registered_at DESC, r.id DESC
        LIMIT 8
        """
    ).fetchall()
    batch_status_counts = {
        row["quality_status"]: row["n"]
        for row in database.execute(
            "SELECT quality_status, COUNT(*) AS n FROM batch_trace_records "
            "GROUP BY quality_status"
        ).fetchall()
    }
    recent_audit_rows = database.execute(
        """
        SELECT ae.*, u.username AS actor_username, u.display_name AS actor_display_name
        FROM audit_events ae
        LEFT JOIN users u ON u.id = ae.actor_user_id
        ORDER BY ae.occurred_at DESC, ae.id DESC
        LIMIT 8
        """
    ).fetchall()
    counts = {
        "machines": database.execute("SELECT COUNT(*) AS n FROM machines").fetchone()["n"],
        "partLabels": database.execute("SELECT COUNT(*) AS n FROM part_labels").fetchone()["n"],
        "traceRecords": database.execute("SELECT COUNT(*) AS n FROM trace_records").fetchone()["n"],
        "todayRecords": database.execute(
            "SELECT COUNT(*) AS n FROM trace_records WHERE substr(completed_at, 1, 10) = ?", (today,)
        ).fetchone()["n"],
        "activeStations": database.execute(
            "SELECT COUNT(*) AS n FROM scan_sessions WHERE machine_id IS NOT NULL"
        ).fetchone()["n"],
        "activeTracePlans": database.execute(
            "SELECT COUNT(*) AS n FROM trace_plans WHERE status = 'ACTIVE'"
        ).fetchone()["n"],
        "assembledRecords": database.execute(
            "SELECT COUNT(*) AS n FROM trace_records WHERE status = 'ASSEMBLED'"
        ).fetchone()["n"],
        "holdRecords": database.execute(
            "SELECT COUNT(*) AS n FROM trace_records WHERE status = 'HOLD'"
        ).fetchone()["n"],
        "passedRecords": database.execute(
            "SELECT COUNT(*) AS n FROM trace_records WHERE status = 'PASSED'"
        ).fetchone()["n"],
        "todayGenerated": database.execute(
            "SELECT COUNT(*) AS n FROM product_code_sets WHERE substr(generated_at, 1, 10) = ?",
            (today,),
        ).fetchone()["n"],
        "lowStockParts": sum(1 for row in stock_rows if row["quantity_available"] > 0),
        "outOfStockParts": sum(1 for row in stock_rows if row["quantity_available"] <= 0),
        # --- Current (batch) model metrics ---
        "productionBatches": database.execute(
            "SELECT COUNT(*) AS n FROM production_batches"
        ).fetchone()["n"],
        "todayBatches": database.execute(
            "SELECT COUNT(*) AS n FROM production_batches "
            "WHERE substr(generated_at, 1, 10) = ?",
            (today,),
        ).fetchone()["n"],
        "registeredBatches": sum(batch_status_counts.values()),
        "batchAssembled": batch_status_counts.get("ASSEMBLED", 0),
        "batchPassed": batch_status_counts.get("PASSED", 0),
        "batchHold": batch_status_counts.get("HOLD", 0),
        "unregisteredBatches": database.execute(
            """
            SELECT COUNT(*) AS n FROM production_batches pb
            WHERE NOT EXISTS (
                SELECT 1 FROM batch_trace_records r
                WHERE r.production_batch_id = pb.id
            )
            """
        ).fetchone()["n"],
        "productionOrders": database.execute(
            "SELECT COUNT(*) AS n FROM production_orders"
        ).fetchone()["n"],
        # Purchase orders operations submitted that the warehouse has not
        # turned into a production order yet (流程第 1 步 backlog).
        "pendingProductionOrders": database.execute(
            """
            SELECT COUNT(*) AS n FROM purchase_orders po
            WHERE NOT EXISTS (
                SELECT 1 FROM production_orders pro
                WHERE pro.purchase_order_id = po.id
            )
            """
        ).fetchone()["n"],
        # Production orders still short of their planned quantity (待入库).
        "pendingStockIn": database.execute(
            """
            SELECT COUNT(*) AS n FROM production_orders pro
            JOIN production_batches pb ON pb.id = pro.production_batch_id
            WHERE COALESCE((
                SELECT SUM(isr.quantity) FROM inbound_scan_records isr
                WHERE isr.production_order_id = pro.id
            ), 0) < COALESCE(pb.planned_quantity, 0)
            """
        ).fetchone()["n"],
        "finishedGoodsOnHand": database.execute(
            "SELECT COALESCE(SUM(on_hand), 0) AS n FROM product_stock"
        ).fetchone()["n"],
        # Stocked products whose Lingxing inventory sync is not PUSHED.
        "inventorySyncPending": database.execute(
            """
            SELECT COUNT(*) AS n FROM product_stock ps
            LEFT JOIN product_stock_sync pss
                ON pss.product_model_id = ps.product_model_id
            WHERE pss.sync_status IS NULL OR pss.sync_status <> 'PUSHED'
            """
        ).fetchone()["n"],
    }
    return success(
        {
            "counts": counts,
            "qualityQueue": quality_queue,
            "batchQualityQueue": [
                batch_trace_record_dict(row) for row in batch_quality_rows
            ],
            "stockAlerts": [part_type_dict(row) for row in stock_rows[:8]],
            "recentActivity": [audit_event_dict(row) for row in recent_audit_rows],
            "recentRecords": fetch_records(database, limit=6),
        }
    )


@dashboard_bp.get("/api/audit-events")
def list_audit_events():
    require_admin()
    database = get_db()
    search = clean_text(request.args.get("search"), "搜索内容", max_length=120)
    event_type = clean_text(request.args.get("eventType"), "事件类型", max_length=64).upper()
    try:
        limit = int(request.args.get("limit", 200))
    except (TypeError, ValueError):
        raise ApiError("日志条数必须是整数") from None
    if not 1 <= limit <= 500:
        raise ApiError("日志条数需在 1-500 之间")

    conditions: list[str] = []
    parameters: list[object] = []
    if search:
        pattern = f"%{search}%"
        conditions.append(
            "(" 
            "ae.event_type LIKE ? OR ae.object_code LIKE ? OR ae.related_object_code LIKE ? OR "
            "ae.operator_name LIKE ? OR ae.station_name LIKE ? OR ae.reason LIKE ? OR "
            "COALESCE(u.username, '') LIKE ? OR COALESCE(u.display_name, '') LIKE ?"
            ")"
        )
        parameters.extend([pattern] * 8)
    if event_type:
        conditions.append("ae.event_type = ?")
        parameters.append(event_type)
    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    rows = database.execute(
        f"""
        SELECT ae.*, u.username AS actor_username, u.display_name AS actor_display_name
        FROM audit_events ae
        LEFT JOIN users u ON u.id = ae.actor_user_id
        {where_clause}
        ORDER BY ae.occurred_at DESC, ae.id DESC
        LIMIT ?
        """,
        (*parameters, limit),
    ).fetchall()
    filtered_total = database.execute(
        f"""
        SELECT COUNT(*) AS n
        FROM audit_events ae
        LEFT JOIN users u ON u.id = ae.actor_user_id
        {where_clause}
        """,
        parameters,
    ).fetchone()["n"]
    today = datetime.now().astimezone().date().isoformat()
    summary = database.execute(
        """
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN substr(occurred_at, 1, 10) = ? THEN 1 ELSE 0 END) AS today,
               COUNT(DISTINCT NULLIF(operator_name, '')) AS operators
        FROM audit_events
        """,
        (today,),
    ).fetchone()
    event_types = database.execute(
        """
        SELECT event_type, COUNT(*) AS event_count
        FROM audit_events
        GROUP BY event_type
        ORDER BY event_count DESC, event_type
        """
    ).fetchall()
    return success(
        {
            "items": [audit_event_dict(row) for row in rows],
            "filteredTotal": filtered_total,
            "total": summary["total"],
            "today": summary["today"] or 0,
            "operators": summary["operators"],
            "eventTypes": [
                {"value": row["event_type"], "count": row["event_count"]}
                for row in event_types
            ],
            "limit": limit,
        }
    )
