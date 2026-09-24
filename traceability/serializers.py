"""Row-to-JSON serialisers.

Every endpoint that returns a domain object builds its response through one of
these, so the wire format is defined in exactly one place. The front end depends
on the exact key names — ``productFamilyId``, ``bluetoothServiceUuid`` and so on —
which is why they are not inlined into the routes.

They are pure: a ``sqlite3.Row`` in, a plain dict out. Several use
``"column" in row.keys()`` rather than ``"column" in row``, which is deliberate —
``sqlite3.Row`` has no ``__contains__``, so ``in`` iterates the *values* and the
"simplification" would silently return False for every column. See the SIM118
note in pyproject.toml.

Extracted from ``app.py``; re-exported from there so existing imports keep
working.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from traceability.codes import batch_identification_code

__all__ = [
    "audit_event_dict",
    "_trace_plan_slot_dict",
    "supplier_dict",
    "part_type_dict",
    "supplier_inventory_batch_dict",
    "product_family_dict",
    "product_model_dict",
    "product_code_batch_dict",
    "machine_dict",
    "part_label_dict",
    "part_label_batch_dict",
    "batch_trace_record_dict",
    "production_batch_dict",
]


def audit_event_dict(row: sqlite3.Row) -> dict[str, Any]:
    try:
        payload = json.loads(row["payload_json"] or "{}")
    except json.JSONDecodeError:
        payload = {}
    return {
        "id": row["id"],
        "eventId": row["event_id"],
        "eventType": row["event_type"],
        "objectType": row["object_type"],
        "objectCode": row["object_code"],
        "relatedObjectCode": row["related_object_code"],
        "stationId": row["station_id"],
        "stationName": row["station_name"],
        "operatorName": row["operator_name"],
        "actorUserId": row["actor_user_id"] if "actor_user_id" in row.keys() else None,
        "actorUsername": row["actor_username"] if "actor_username" in row.keys() else "",
        "actorDisplayName": row["actor_display_name"] if "actor_display_name" in row.keys() else "",
        "reason": row["reason"],
        "payload": payload,
        "occurredAt": row["occurred_at"],
    }


def _trace_plan_slot_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "position": row["position"],
        "slotName": row["slot_name"],
        "partTypeId": row["part_type_id"],
        "partCode": row["part_code"],
        "partName": row["part_name"],
        "categoryCode": row["category_code"],
        "categoryName": row["category_name"],
        "specification": row["specification"],
        "minimumStock": row["minimum_stock"],
        "supplierId": row["supplier_id"],
        "supplierCode": row["supplier_code"],
        "supplierName": row["supplier_name"],
        "inventoryBatchId": row["supplier_inventory_batch_id"],
        "inventoryBatchNo": row["inventory_batch_no"],
        "inventoryQuantityReceived": row["inventory_quantity_received"],
        "inventoryQuantityAvailable": row["inventory_quantity_available"],
        "inventoryProductionDate": row["inventory_production_date"],
        "inventoryReceivedDate": row["inventory_received_date"],
        "inventoryBatchActive": (
            bool(row["inventory_batch_active"])
            if row["inventory_batch_active"] is not None else None
        ),
    }


def supplier_dict(row: sqlite3.Row) -> dict[str, Any]:
    keys = set(row.keys())
    return {
        "id": row["id"],
        "supplierCode": row["supplier_code"],
        "name": row["name"],
        "contact": row["contact"],
        "phone": row["phone"],
        "active": bool(row["active"]) if "active" in keys else True,
        "partCount": row["part_count"] if "part_count" in keys else 0,
        "batchCount": row["batch_count"] if "batch_count" in keys else 0,
        "quantityAvailable": row["quantity_available"] if "quantity_available" in keys else 0,
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"] if "updated_at" in keys else row["created_at"],
    }


def part_type_dict(row: sqlite3.Row) -> dict[str, Any]:
    keys = set(row.keys())
    quantity_available = row["quantity_available"] if "quantity_available" in keys else 0
    minimum_stock = row["minimum_stock"] if "minimum_stock" in keys else 0
    if quantity_available <= 0:
        stock_status = "OUT"
    elif minimum_stock > 0 and quantity_available <= minimum_stock:
        stock_status = "LOW"
    else:
        stock_status = "NORMAL"
    return {
        "id": row["id"],
        "partCode": row["part_code"],
        "name": row["name"],
        "categoryCode": row["category_code"],
        "categoryName": row["category_name"],
        "specification": row["specification"],
        "minimumStock": minimum_stock,
        "supplierId": row["supplier_id"],
        "supplierCode": row["supplier_code"],
        "supplierName": row["supplier_name"],
        "active": bool(row["active"]),
        "batchCount": row["batch_count"] if "batch_count" in keys else 0,
        "quantityAvailable": quantity_available,
        "stockStatus": stock_status,
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"] if "updated_at" in keys else row["created_at"],
    }


def supplier_inventory_batch_dict(row: sqlite3.Row) -> dict[str, Any]:
    keys = set(row.keys())
    received = row["quantity_received"]
    available = row["quantity_available"]
    return {
        "id": row["id"],
        "partTypeId": row["part_type_id"],
        "partCode": row["part_code"] if "part_code" in keys else None,
        "partName": row["part_name"] if "part_name" in keys else None,
        "specification": row["specification"] if "specification" in keys else "",
        "supplierId": row["supplier_id"] if "supplier_id" in keys else None,
        "supplierCode": row["supplier_code"] if "supplier_code" in keys else None,
        "supplierName": row["supplier_name"] if "supplier_name" in keys else None,
        "batchNo": row["batch_no"],
        "quantityReceived": received,
        "quantityAvailable": available,
        "quantityConsumed": received - available,
        "productionDate": row["production_date"],
        "receivedDate": row["received_date"],
        "remarks": row["remarks"],
        "active": bool(row["active"]),
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


def product_family_dict(row: sqlite3.Row) -> dict[str, Any]:
    keys = set(row.keys())
    return {
        "id": row["id"],
        "productCode": row["product_code"],
        "name": row["name"],
        "description": row["description"],
        "active": bool(row["active"]),
        "modelCount": row["model_count"] if "model_count" in keys else 0,
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


def product_model_dict(row: sqlite3.Row) -> dict[str, Any]:
    keys = set(row.keys())
    return {
        "id": row["id"],
        "productFamilyId": row["product_family_id"],
        "productFamilyCode": row["product_family_code"] if "product_family_code" in keys else None,
        "productFamilyName": row["product_family_name"] if "product_family_name" in keys else None,
        "modelCode": row["model_code"],
        "name": row["name"],
        "serialPrefix": row["serial_prefix"],
        "identitySource": row["identity_source"],
        "bluetoothNamePrefix": row["bluetooth_name_prefix"],
        "bluetoothServiceUuid": row["bluetooth_service_uuid"] if "bluetooth_service_uuid" in keys else "",
        "bluetoothNotifyUuid": row["bluetooth_notify_uuid"] if "bluetooth_notify_uuid" in keys else "",
        "active": bool(row["active"]),
        "finishedProductCount": row["finished_product_count"] if "finished_product_count" in keys else 0,
        "tracePlanCount": row["trace_plan_count"] if "trace_plan_count" in keys else 0,
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


def product_code_batch_dict(row: sqlite3.Row, *, include_sets: bool = False) -> dict[str, Any]:
    keys = set(row.keys())
    data = {
        "id": row["id"],
        "batchCode": row["batch_code"],
        "productModelId": row["product_model_id"],
        "productName": row["product_name"] if "product_name" in keys else None,
        "tracePlanId": row["trace_plan_id"],
        "prefix": row["prefix"],
        "dateCode": row["date_code"],
        "quantity": row["quantity"],
        "startSequence": row["start_sequence"],
        "endSequence": row["end_sequence"],
        "generatedBy": row["generated_by"] if "generated_by" in keys else "",
        "generatedAt": row["generated_at"],
        "downloadUrl": f"/api/product-code-batches/{row['id']}/qrcodes.zip",
    }
    if include_sets and "sets" in keys:
        data["sets"] = row["sets"]
    return data


def machine_dict(row: sqlite3.Row) -> dict[str, Any]:
    keys = set(row.keys())
    return {
        "id": row["id"],
        "sn": row["sn"],
        "model": row["model"],
        "productModelId": row["product_model_id"] if "product_model_id" in keys else None,
        "productModelName": row["product_model_name"] if "product_model_name" in keys else None,
        "productFamilyId": row["product_family_id"] if "product_family_id" in keys else None,
        "productFamilyCode": row["product_family_code"] if "product_family_code" in keys else None,
        "productFamilyName": row["product_family_name"] if "product_family_name" in keys else None,
        "productionDate": row["production_date"],
        "tracePlanId": row["trace_plan_id"] if "trace_plan_id" in keys else None,
        "tracePlanVersion": row["trace_plan_version"] if "trace_plan_version" in keys else None,
        "tracePlanName": row["trace_plan_name"] if "trace_plan_name" in keys else None,
        "identificationCode": row["identification_code"],
        "qrUrl": f"/api/machines/{row['id']}/qr",
        "createdAt": row["created_at"],
        "traced": bool(row["traced"]) if "traced" in keys else False,
        "reservedStation": row["reserved_station"] if "reserved_station" in keys else None,
    }


def part_label_dict(row: sqlite3.Row) -> dict[str, Any]:
    keys = set(row.keys())
    return {
        "id": row["id"],
        "identificationCode": row["identification_code"],
        "qrUrl": f"/api/part-labels/{row['id']}/qr",
        "partTypeId": row["part_type_id"],
        "partCode": row["part_code"],
        "partName": row["part_name"],
        "categoryCode": row["category_code"] if "category_code" in keys else row["part_code"],
        "categoryName": row["category_name"] if "category_name" in keys else row["part_name"],
        "supplierCode": row["supplier_code"],
        "supplierName": row["supplier_name"],
        "labelBatchId": row["label_batch_id"] if "label_batch_id" in keys else None,
        "batchCode": row["batch_code"] if "batch_code" in keys else None,
        "batchQuantity": row["batch_quantity"] if "batch_quantity" in keys else None,
        "batchGeneratedAt": row["batch_generated_at"] if "batch_generated_at" in keys else None,
        "batchGeneratedBy": row["batch_generated_by"] if "batch_generated_by" in keys else None,
        "lotNo": row["lot_no"],
        "supplierBatchNo": row["supplier_batch_no"],
        "sourceSerialNo": row["source_serial_no"] if "source_serial_no" in keys else "",
        "productionDate": row["production_date"] if "production_date" in keys else "",
        "inspectionStatus": row["inspection_status"] if "inspection_status" in keys else "PENDING",
        "remarks": row["remarks"] if "remarks" in keys else "",
        "enteredBy": row["entered_by"] if "entered_by" in keys else "",
        "enteredAt": row["entered_at"] if "entered_at" in keys else None,
        "updatedAt": row["updated_at"] if "updated_at" in keys else None,
        "dataEntered": bool(row["entered_at"]) if "entered_at" in keys else False,
        "createdAt": row["created_at"],
        "used": bool(row["used"]) if "used" in keys else False,
        "reservedStation": row["reserved_station"] if "reserved_station" in keys else None,
    }


def part_label_batch_dict(row: sqlite3.Row) -> dict[str, Any]:
    keys = set(row.keys())
    return {
        "id": row["id"],
        "batchCode": row["batch_code"],
        "partTypeId": row["part_type_id"],
        "partCode": row["part_code"],
        "partName": row["part_name"],
        "categoryCode": row["category_code"] if "category_code" in keys else row["part_code"],
        "categoryName": row["category_name"] if "category_name" in keys else row["part_name"],
        "supplierCode": row["supplier_code"],
        "supplierName": row["supplier_name"],
        "lotNo": row["lot_no"],
        "supplierBatchNo": row["supplier_batch_no"],
        "quantity": row["quantity"],
        "productionDate": row["production_date"],
        "generatedBy": row["generated_by"],
        "generatedAt": row["generated_at"],
        "generatedCount": row["generated_count"] if "generated_count" in keys else row["quantity"],
        "dataEnteredCount": row["data_entered_count"] if "data_entered_count" in keys else 0,
    }


def batch_trace_record_dict(row: sqlite3.Row) -> dict[str, Any]:
    """Serialize a batch registration record joined to its production batch.

    Carries the batch code value, product model, registered quantity, the
    batch's generation time, prefix and operator (Requirement 3.2). ``row`` is
    expected to expose the ``batch_trace_records`` columns together with the
    joined ``production_batches`` (``batch_code`` / ``prefix`` /
    ``generated_at`` / ``product_model_id``) and ``product_models``
    (``model_code`` / ``name``) fields.
    """
    keys = row.keys()
    batch_code = row["batch_code"]
    return {
        "id": row["id"],
        "productionBatchId": row["production_batch_id"],
        "batchCode": batch_code,
        "identificationCode": batch_identification_code(batch_code),
        "productModelId": row["product_model_id"],
        "productModelCode": row["product_model_code"] if "product_model_code" in keys else None,
        "productName": row["product_name"] if "product_name" in keys else None,
        "registeredQuantity": row["registered_quantity"],
        "qualityStatus": row["quality_status"],
        "prefix": row["prefix"],
        "generatedAt": row["generated_at"],
        "operatorName": row["operator_name"],
        "registeredAt": row["registered_at"],
    }


def production_batch_dict(
    database: sqlite3.Connection,
    row: sqlite3.Row,
) -> dict[str, Any]:
    """Serialize a ``production_batches`` row for API responses.

    Carries the batch code value, product model, prefix, planned quantity,
    generation time and operator (Requirements 1.3, 1.5, 3.1) plus the QR /
    download URLs served by task 6.2. The batch QR encodes ``PTS:B:{batch_code}``
    so a single code value represents the whole batch (Requirements 11.1, 11.2).
    """
    keys = row.keys()
    # Use the product model columns when the caller already joined them (list
    # endpoints), otherwise resolve them with a single lookup. This avoids an
    # N+1 product query when serializing a whole batch list.
    if "model_code" in keys:
        model_code = row["model_code"]
        product_name = row["product_name"] if "product_name" in keys else None
    else:
        product = database.execute(
            "SELECT model_code, name FROM product_models WHERE id = ?",
            (row["product_model_id"],),
        ).fetchone()
        model_code = product["model_code"] if product else None
        product_name = product["name"] if product else None
    batch_code = row["batch_code"]
    return {
        "id": row["id"],
        "batchCode": batch_code,
        "identificationCode": batch_identification_code(batch_code),
        "productModelId": row["product_model_id"],
        "productModelCode": model_code,
        "productName": product_name,
        "tracePlanId": row["trace_plan_id"],
        "prefix": row["prefix"],
        "plannedQuantity": row["planned_quantity"],
        "generatedBy": row["generated_by"] if "generated_by" in keys else "",
        "generatedByUserId": row["generated_by_user_id"],
        "generatedAt": row["generated_at"],
        "qrUrl": f"/api/production-batches/{row['id']}/qr",
        "downloadUrl": f"/api/production-batches/{row['id']}/qr",
    }
