/**
 * Display helpers shared by every page. Ported from the previous UI so numbers,
 * dates and status labels read exactly the same after the migration.
 */

/** Quantities: keep integers clean, trim trailing zeros on decimals. */
export function formatNumber(value: unknown): string {
  if (value === null || value === undefined || value === '') {
    return ''
  }
  const number = Number(value)
  if (!Number.isFinite(number)) {
    return String(value)
  }
  return Number.isInteger(number) ? String(number) : String(Number(number.toFixed(4)))
}

/** Amounts: two decimals with thousands separators. */
export function formatMoney(value: unknown): string {
  if (value === null || value === undefined || value === '') {
    return ''
  }
  const number = Number(value)
  if (!Number.isFinite(number)) {
    return String(value)
  }
  return number.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
}

const dateTimeFormat = new Intl.DateTimeFormat('zh-CN', {
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
  hour12: false,
})

/** `YYYY-MM-DD HH:mm` in local time, `-` when empty. */
export function formatDate(value: unknown): string {
  if (!value) {
    return '-'
  }
  const date = new Date(String(value))
  if (Number.isNaN(date.getTime())) {
    return String(value)
  }
  const parts = Object.fromEntries(dateTimeFormat.formatToParts(date).map(part => [part.type, part.value]))
  return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}`
}

/** `YYYY-MM-DD` (no time), `-` when empty. */
export function formatDay(value: unknown): string {
  const text = formatDate(value)
  return text === '-' ? text : text.slice(0, 10)
}

/** Zero-padded sequence, e.g. 7 → `0007`. */
export function padSequence(value: unknown, width = 4): string {
  return String(value ?? '').padStart(width, '0')
}

export type TagType = 'primary' | 'success' | 'warning' | 'danger' | 'info'

/** Quality status of a record / batch registration: label + Element Plus tag type. */
export const QUALITY_STATUS: Record<string, { label: string, type: TagType }> = {
  ASSEMBLED: { label: '待检', type: 'warning' },
  PASSED: { label: '合格', type: 'success' },
  HOLD: { label: '暂扣', type: 'danger' },
  VOID: { label: '作废', type: 'info' },
}

export function qualityStatus(status: unknown) {
  const key = String(status ?? '')
  return QUALITY_STATUS[key] ?? { label: key || '未知', type: 'info' as TagType }
}

/** Lingxing push status of purchase orders / supplier receipts / stock sync. */
export const SYNC_STATUS: Record<string, { label: string, type: TagType }> = {
  PENDING: { label: '待推送', type: 'warning' },
  PUSHED: { label: '已推送', type: 'success' },
  FAILED: { label: '推送失败', type: 'danger' },
}

export function syncStatus(status: unknown) {
  const key = String(status ?? '').toUpperCase()
  return SYNC_STATUS[key] ?? { label: String(status ?? '') || '-', type: 'info' as TagType }
}

const AUDIT_EVENT_LABELS: Record<string, string> = {
  ASSEMBLY_COMPLETED: '产品录入完成',
  TRACE_RECORD_STATUS_CHANGED: '质量状态更新',
  TRACE_RECORD_CORRECTED: '录入记录校对',
  TRACE_RECORD_DELETED: '录入记录删除',
  PRODUCT_CODE_BATCH_GENERATED: '产品码批次生成',
  PRODUCT_CODES_GENERATED: '产品二维码生成',
  SUPPLIER_BATCH_RECEIVED: '供应批次入库',
  SUPPLIER_BATCH_UPDATED: '供应批次调整',
  SUPPLIER_PART_UPDATED: '供应部件更新',
  PRODUCT_CREATED: '产品创建',
  PRODUCT_UPDATED: '产品更新',
  USER_CREATED: '用户创建',
  USER_UPDATED: '用户更新',
  PO_CREATED: '采购订单创建',
  PO_PUSHED: '采购订单已推送',
  PO_PUSH_FAILED: '采购订单推送失败',
  INBOUND_RECEIVED: '供应收货登记',
  INBOUND_PUSHED: '供应收货已推送',
  INBOUND_PUSH_FAILED: '供应收货推送失败',
  PRODUCTION_ORDER_CREATED: '生产订单创建',
  FINISHED_GOODS_RECEIVED: '成品扫码入库',
  INVENTORY_SYNC_PUSHED: '库存同步成功',
  INVENTORY_SYNC_FAILED: '库存同步失败',
  USER_LOGIN: '账号登录',
  USER_LOGOUT: '退出登录',
  USER_PASSWORD_CHANGED: '密码已修改',
}

export function auditEventLabel(type: unknown): string {
  return AUDIT_EVENT_LABELS[String(type ?? '')] ?? '系统操作'
}

export function auditEventDescription(event: { eventType?: string, objectCode?: string, relatedObjectCode?: string }): string {
  const objectCode = String(event.objectCode ?? '').trim()
  const relatedCode = String(event.relatedObjectCode ?? '').trim()
  switch (event.eventType) {
    case 'USER_LOGIN':
      return objectCode ? `登录账号 ${objectCode}` : '账号已登录系统'
    case 'USER_LOGOUT':
      return objectCode ? `账号 ${objectCode} 已退出` : '账号已退出系统'
    case 'USER_PASSWORD_CHANGED':
      return objectCode ? `账号 ${objectCode} 更新登录密码` : '登录密码已更新'
    case 'PRODUCT_CODE_BATCH_GENERATED':
    case 'PRODUCT_CODES_GENERATED':
      return objectCode ? `生成产品二维码 ${objectCode}` : '生成产品二维码'
    case 'TRACE_RECORD_STATUS_CHANGED':
      return objectCode ? `更新质量记录 ${objectCode}` : '更新质量记录'
    default:
      return [objectCode, relatedCode].filter(Boolean).join(' · ') || '系统数据已更新'
  }
}
