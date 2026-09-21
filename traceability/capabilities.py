"""Capability matrix: the single source of truth for who may do what.

This module deliberately has **no Flask dependency** so that it can be imported
by both the application (``traceability.auth``) and by static tooling
(``tools/extract_routes.py``). Keeping the policy table importable by the
extractor is what stops ``docs/PERMISSION_MATRIX.md`` from drifting again.

Design notes
------------
- A **role** is what a user account is (ADMIN / WAREHOUSE / OPERATIONS).
- A **capability** is what a route requires (e.g. ``BATCH_GENERATE``).
- Routes should check capabilities, not roles, so the policy lives in exactly
  one place and can be reviewed without reading 8,700 lines of ``app.py``.

``ADMIN`` is a superuser: it holds every capability. That is expressed
explicitly below rather than being special-cased in the check, so the table
stays readable.
"""

from __future__ import annotations

from enum import Enum


class Capability(str, Enum):
    """What a request needs to be allowed."""

    # --- 基础资料（仅管理员维护） ---
    PRODUCT_MANAGE = "PRODUCT_MANAGE"
    SUPPLIER_MANAGE = "SUPPLIER_MANAGE"
    USER_MANAGE = "USER_MANAGE"
    SETTINGS_MANAGE = "SETTINGS_MANAGE"
    INVENTORY_MANAGE = "INVENTORY_MANAGE"
    LEGACY_QR_MANAGE = "LEGACY_QR_MANAGE"

    # --- 批次与现场作业（仓管） ---
    BATCH_GENERATE = "BATCH_GENERATE"
    BATCH_REGISTER = "BATCH_REGISTER"
    LEGACY_SCAN = "LEGACY_SCAN"
    QUALITY_RELEASE = "QUALITY_RELEASE"

    # --- 收货、生产与成品入库（仓管） ---
    RECEIPT_CREATE = "RECEIPT_CREATE"
    PRODUCTION_ORDER_CREATE = "PRODUCTION_ORDER_CREATE"
    FINISHED_GOODS_INBOUND = "FINISHED_GOODS_INBOUND"

    # --- 采购与领星（运营） ---
    PURCHASE_MANAGE = "PURCHASE_MANAGE"
    LINGXING_PUSH = "LINGXING_PUSH"
    INVENTORY_SYNC = "INVENTORY_SYNC"
    OPERATIONS_PRODUCT_MANAGE = "OPERATIONS_PRODUCT_MANAGE"

    # --- 历史录入记录（管理员 + 仓管；运营无权，见前端 allowedViews） ---
    RECORD_VIEW = "RECORD_VIEW"
    RECORD_EDIT = "RECORD_EDIT"
    RECORD_DELETE = "RECORD_DELETE"
    RECORD_ADMIN = "RECORD_ADMIN"

    # --- 只读 / 全体角色 ---
    TRACE_VIEW = "TRACE_VIEW"
    DASHBOARD_VIEW = "DASHBOARD_VIEW"
    AUDIT_VIEW = "AUDIT_VIEW"
    BLUETOOTH_USE = "BLUETOOTH_USE"


_ADMIN_ONLY: frozenset[Capability] = frozenset(
    {
        Capability.PRODUCT_MANAGE,
        Capability.SUPPLIER_MANAGE,
        Capability.USER_MANAGE,
        Capability.SETTINGS_MANAGE,
        Capability.INVENTORY_MANAGE,
        Capability.LEGACY_QR_MANAGE,
        Capability.QUALITY_RELEASE,
        Capability.RECORD_ADMIN,
        Capability.DASHBOARD_VIEW,
        Capability.AUDIT_VIEW,
    }
)

_WAREHOUSE: frozenset[Capability] = frozenset(
    {
        Capability.BATCH_GENERATE,
        Capability.BATCH_REGISTER,
        Capability.LEGACY_SCAN,
        Capability.RECEIPT_CREATE,
        Capability.PRODUCTION_ORDER_CREATE,
        Capability.FINISHED_GOODS_INBOUND,
        Capability.RECORD_VIEW,
        Capability.RECORD_EDIT,
        Capability.RECORD_DELETE,
    }
)

_OPERATIONS: frozenset[Capability] = frozenset(
    {
        Capability.PURCHASE_MANAGE,
        Capability.LINGXING_PUSH,
        Capability.INVENTORY_SYNC,
        Capability.OPERATIONS_PRODUCT_MANAGE,
    }
)

# Read-only capabilities every authenticated role holds.
_SHARED: frozenset[Capability] = frozenset(
    {
        Capability.TRACE_VIEW,
        Capability.BLUETOOTH_USE,
    }
)

ROLE_ADMIN = "ADMIN"
ROLE_WAREHOUSE = "WAREHOUSE"
ROLE_OPERATIONS = "OPERATIONS"
VALID_ROLES = frozenset({ROLE_ADMIN, ROLE_WAREHOUSE, ROLE_OPERATIONS})

ROLE_CAPABILITIES: dict[str, frozenset[Capability]] = {
    ROLE_ADMIN: frozenset(_ADMIN_ONLY | _WAREHOUSE | _OPERATIONS | _SHARED),
    ROLE_WAREHOUSE: frozenset(_WAREHOUSE | _SHARED),
    ROLE_OPERATIONS: frozenset(_OPERATIONS | _SHARED),
}


def capabilities_for_role(role: str) -> frozenset[Capability]:
    """Capabilities granted to ``role``; unknown roles get nothing."""
    return ROLE_CAPABILITIES.get(str(role), frozenset())


def roles_for(capability: Capability) -> frozenset[str]:
    """Which roles hold ``capability``. Used by docs and the extractor."""
    return frozenset(
        role for role, granted in ROLE_CAPABILITIES.items() if capability in granted
    )


def has_capability(role: str, capability: Capability) -> bool:
    return capability in capabilities_for_role(role)


def missing_capabilities(role: str, required: tuple[Capability, ...]) -> list[Capability]:
    granted = capabilities_for_role(role)
    return [item for item in required if item not in granted]
