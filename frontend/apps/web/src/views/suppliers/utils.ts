/**
 * Shared helpers for the 供应商 pages.
 */
import type { FormItemRule } from 'element-plus'
import type { TagType } from '@/utils/format'

/**
 * Supplier / part / batch codes follow ENTITY_CODE_PATTERN in
 * traceability/codes.py (checked after the server upper-cases them), so the
 * form reports the same message before the round trip.
 */
const ENTITY_CODE_PATTERN = /^\s*[A-Z0-9][\w-]{1,31}\s*$/i

export function entityCodeRules(label: string): FormItemRule[] {
  return [
    { required: true, whitespace: true, message: `请输入${label}`, trigger: 'blur' },
    { pattern: ENTITY_CODE_PATTERN, message: `${label}需为 2-32 位字母、数字、下划线或短横线`, trigger: 'blur' },
  ]
}

/** Stock status of a supplier part (part_type_dict on the server). */
export const PART_STOCK_STATUS: Record<string, { label: string, type: TagType }> = {
  OUT: { label: '库存已用尽', type: 'danger' },
  LOW: { label: '低于安全库存', type: 'warning' },
  NORMAL: { label: '库存正常', type: 'success' },
}

export function partStockStatus(status: unknown) {
  return PART_STOCK_STATUS[String(status ?? '')] ?? PART_STOCK_STATUS.NORMAL
}

/** Supplier inventory movement types (supplier_inventory_movements.movement_type). */
export const MOVEMENT_TYPES: Record<string, { label: string, type: TagType }> = {
  RECEIPT: { label: '到货入库', type: 'success' },
  ISSUE: { label: '生产领用', type: 'info' },
  ADJUSTMENT: { label: '库存调整', type: 'warning' },
}

export function movementType(type: unknown) {
  const key = String(type ?? '')
  return MOVEMENT_TYPES[key] ?? { label: key || '-', type: 'info' as TagType }
}

/** Today as `YYYY-MM-DD` in local time (default 到货日期). */
export function todayInputDate(): string {
  const now = new Date()
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}`
}
