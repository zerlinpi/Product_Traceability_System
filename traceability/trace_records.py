"""Per-unit trace records (逐台录入记录): the read model.

``fetch_records`` turns ``trace_records`` rows into the payload the UI shows for
a record: the finished product, its parts in position order, the BOM it was
assembled against and the code-generation batch it came from. The admin
dashboard, the legacy station scan (to return the record it just completed) and
the records / genealogy / export routes all read records through it, which is
why it lives here rather than in any one of their blueprints.

Extracted from ``app.py`` when those routes moved to ``traceability/api/``.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from typing import Any

from traceability.serializers import part_label_dict

__all__ = ["fetch_records"]


def fetch_records(
    database: sqlite3.Connection,
    search: str = "",
    limit: int = 300,
    *,
    product_model_id: int | None = None,
    completed_by_user_id: int | None = None,
    statuses: list[str] | None = None,
    generation_batch_id: int | None = None,
    completed_date_from: str = "",
    completed_date_to: str = "",
    record_ids: list[int] | None = None,
) -> list[dict[str, Any]]:
    parameters: list[Any] = []
    clauses: list[str] = []
    if search:
        wildcard = f"%{search}%"
        clauses.append("""
        (r.trace_no LIKE ? OR m.sn LIKE ? OR m.model LIKE ?
           OR pm.name LIKE ? OR pf.product_code LIKE ? OR pf.name LIKE ?
           OR r.operator_name LIKE ? OR r.station_name LIKE ?
           OR tp.name LIKE ? OR tp.version LIKE ?
           OR EXISTS (
                SELECT 1 FROM trace_record_parts srp
                JOIN part_labels spl ON spl.id = srp.part_label_id
                JOIN part_types spt ON spt.id = spl.part_type_id
                JOIN suppliers ss ON ss.id = spt.supplier_id
                LEFT JOIN part_label_batches spb ON spb.id = spl.label_batch_id
                WHERE srp.trace_record_id = r.id
                  AND (spl.identification_code LIKE ? OR spt.part_code LIKE ?
                       OR spt.name LIKE ? OR ss.supplier_code LIKE ? OR ss.name LIKE ?
                       OR spl.lot_no LIKE ? OR spl.supplier_batch_no LIKE ?
                       OR spl.source_serial_no LIKE ? OR spb.batch_code LIKE ?)
           )
        )""")
        parameters.extend([wildcard] * 19)
    if product_model_id is not None:
        clauses.append("m.product_model_id = ?")
        parameters.append(product_model_id)
    if completed_by_user_id is not None:
        clauses.append("r.completed_by_user_id = ?")
        parameters.append(completed_by_user_id)
    if statuses:
        status_placeholders = ",".join("?" for _ in statuses)
        clauses.append(f"r.status IN ({status_placeholders})")
        parameters.extend(statuses)
    if generation_batch_id is not None:
        clauses.append("pcs.generation_batch_id = ?")
        parameters.append(generation_batch_id)
    if completed_date_from:
        clauses.append("date(r.completed_at) >= date(?)")
        parameters.append(completed_date_from)
    if completed_date_to:
        clauses.append("date(r.completed_at) <= date(?)")
        parameters.append(completed_date_to)
    if record_ids:
        record_placeholders = ",".join("?" for _ in record_ids)
        clauses.append(f"r.id IN ({record_placeholders})")
        parameters.extend(record_ids)
    where_clause = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    parameters.append(limit)
    record_rows = database.execute(
        f"""
        SELECT r.*, m.sn, m.model, m.product_model_id, m.production_date,
               m.identification_code AS machine_code,
               pm.name AS product_model_name,
               pf.id AS product_family_id, pf.product_code AS product_family_code,
               pf.name AS product_family_name,
               tp.name AS plan_name, tp.version AS plan_version, tp.model_code AS plan_model_code,
               pcb.id AS generation_batch_id, pcb.batch_code AS generation_batch_code,
               pcb.date_code AS generation_batch_date_code,
               pcb.generated_at AS generation_batch_generated_at
        FROM trace_records r
        JOIN machines m ON m.id = r.machine_id
        LEFT JOIN product_models pm ON pm.id = m.product_model_id
        LEFT JOIN product_families pf ON pf.id = pm.product_family_id
        LEFT JOIN trace_plans tp ON tp.id = r.trace_plan_id
        LEFT JOIN product_code_sets pcs ON pcs.machine_id = m.id
        LEFT JOIN product_code_batches pcb ON pcb.id = pcs.generation_batch_id
        {where_clause}
        ORDER BY r.completed_at DESC, r.id DESC
        LIMIT ?
        """,
        parameters,
    ).fetchall()
    if not record_rows:
        return []

    record_ids = [row["id"] for row in record_rows]
    placeholders = ",".join("?" for _ in record_ids)
    part_rows = database.execute(
        f"""
        SELECT trp.trace_record_id, trp.position, pl.*, pt.part_code,
               pt.name AS part_name, pt.category_code, pt.category_name,
               s.supplier_code, s.name AS supplier_name,
               plb.batch_code, plb.quantity AS batch_quantity,
               plb.generated_at AS batch_generated_at, plb.generated_by AS batch_generated_by,
               1 AS used, NULL AS reserved_station
        FROM trace_record_parts trp
        JOIN part_labels pl ON pl.id = trp.part_label_id
        JOIN part_types pt ON pt.id = pl.part_type_id
        JOIN suppliers s ON s.id = pt.supplier_id
        LEFT JOIN part_label_batches plb ON plb.id = pl.label_batch_id
        WHERE trp.trace_record_id IN ({placeholders})
        ORDER BY trp.trace_record_id, trp.position
        """,
        record_ids,
    ).fetchall()
    grouped_parts: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in part_rows:
        grouped_parts[row["trace_record_id"]].append(
            {"position": row["position"], **part_label_dict(row)}
        )

    return [
        {
            "id": row["id"],
            "traceNo": row["trace_no"],
            "machine": {
                "id": row["machine_id"],
                "sn": row["sn"],
                "model": row["model"],
                "productModelId": row["product_model_id"],
                "productModelName": row["product_model_name"],
                "productFamilyId": row["product_family_id"],
                "productFamilyCode": row["product_family_code"],
                "productFamilyName": row["product_family_name"],
                "productionDate": row["production_date"],
                "identificationCode": row["machine_code"],
                "qrUrl": f"/api/machines/{row['machine_id']}/qr",
            },
            "parts": grouped_parts[row["id"]],
            "status": row["status"],
            "tracePlan": {
                "id": row["trace_plan_id"],
                "name": row["plan_name"],
                "version": row["plan_version"],
                "modelCode": row["plan_model_code"],
            } if row["trace_plan_id"] else None,
            "generationBatch": {
                "id": row["generation_batch_id"],
                "batchCode": row["generation_batch_code"],
                "dateCode": row["generation_batch_date_code"],
                "generatedAt": row["generation_batch_generated_at"],
            } if row["generation_batch_id"] else None,
            "stationId": row["station_id"],
            "stationName": row["station_name"],
            "operatorName": row["operator_name"],
            "remarks": row["remarks"] if "remarks" in row.keys() else "",
            "statusReason": row["status_reason"] if "status_reason" in row.keys() else "",
            "statusUpdatedAt": row["status_updated_at"] if "status_updated_at" in row.keys() else None,
            "statusUpdatedByUserId": (
                row["status_updated_by_user_id"]
                if "status_updated_by_user_id" in row.keys() else None
            ),
            "completedByUserId": row["completed_by_user_id"],
            "completedAt": row["completed_at"],
        }
        for row in record_rows
    ]
