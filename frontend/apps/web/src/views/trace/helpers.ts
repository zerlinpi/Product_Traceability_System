/**
 * Helpers shared by 产品查询 (trace) and 数据概览 (dashboard): date grouping of
 * table rows, the legacy day label, per-unit record labels and the two
 * genealogy views (batch → supplier batches, unit → bound parts).
 *
 * Image URLs are always rebuilt from numeric ids through the API modules, so
 * nothing taken from a payload ends up in an `src` attribute unchecked.
 */
import type { AnyRecord } from '@/api/types'
import batchesApi from '@/api/modules/batches'
import recordsApi from '@/api/modules/records'

// ---- dates ------------------------------------------------------------------

const groupDayFormat = new Intl.DateTimeFormat('zh-CN', {
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
  weekday: 'short',
})

/** The stored (server-local) calendar day of an ISO timestamp, `''` when empty. */
export function dayKey(value: unknown): string {
  return value ? String(value).slice(0, 10) : ''
}

/**
 * Day label used by the legacy trace tables and genealogy, e.g. `2026/07/16周四`;
 * `日期未记录` when empty. A bare `YYYY-MM-DD` is read as a local date so the
 * label never shifts to the previous day west of UTC.
 */
export function formatGroupDay(value: unknown): string {
  if (!value) {
    return '日期未记录'
  }
  const text = String(value)
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(text)
  const date = match ? new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3])) : new Date(text)
  if (Number.isNaN(date.getTime())) {
    return text.slice(0, 10)
  }
  return groupDayFormat.format(date)
}

/** A date header row interleaved with the data rows of a grouped table. */
export type DateGroupRow = {
  __group: true
  key: string
  label: string
  count: number
}

export type GroupedRow<T> = T | DateGroupRow

export function isGroupRow(row: unknown): row is DateGroupRow {
  return Boolean(row && typeof row === 'object' && (row as AnyRecord).__group === true)
}

/** ElTable `span-method`: a date header row spans every column from the first one. */
export function dateGroupSpan(columnCount: number) {
  return ({ row, columnIndex }: { row: unknown, columnIndex: number }) => {
    if (!isGroupRow(row)) {
      return undefined
    }
    return columnIndex === 0 ? [1, columnCount] : [0, 0]
  }
}

/** ElTable `row-class-name` for grouped tables. */
export function dateGroupRowClass({ row }: { row: unknown }) {
  return isGroupRow(row) ? 'pts-date-group-row' : ''
}

/** ElTable `row-key` for grouped tables: header rows and data rows never collide. */
export function groupedRowKey(prefix: string) {
  return (row: AnyRecord) => (isGroupRow(row) ? row.key : `${prefix}-${row.id}`)
}

/**
 * Group rows by calendar day, keeping the incoming order (the API already
 * sorts newest first), and interleave one header row per day.
 */
export function groupByDay<T extends AnyRecord>(items: T[], dateOf: (item: T) => unknown): GroupedRow<T>[] {
  const groups = new Map<string, T[]>()
  for (const item of items) {
    const key = dayKey(dateOf(item))
    const bucket = groups.get(key)
    if (bucket) {
      bucket.push(item)
    }
    else {
      groups.set(key, [item])
    }
  }
  const rows: GroupedRow<T>[] = []
  for (const [key, bucket] of groups) {
    rows.push({ __group: true, key: `day-${key || 'none'}`, label: formatGroupDay(key), count: bucket.length })
    rows.push(...bucket)
  }
  return rows
}

// ---- per-unit records -----------------------------------------------------------

/** Generation batch (二维码生成批次) of a per-unit record. */
export function generationBatchLabel(record: AnyRecord | null | undefined): string {
  const batch = record?.generationBatch
  return batch ? String(batch.batchCode ?? '') : '历史录入 / 无生成批次'
}

/** `产品型号 · SN`, as shown in the record dialogs. */
export function recordMachineLabel(record: AnyRecord | null | undefined): string {
  const machine = record?.machine ?? {}
  return `${machine.productModelName || machine.model || ''} · ${machine.sn || ''}`
}

function positiveId(value: unknown): number | null {
  const id = Number(value)
  return Number.isInteger(id) && id > 0 ? id : null
}

// ---- genealogy ----------------------------------------------------------------

export interface GenealogySummaryItem {
  label: string
  value: string
  /** When set, the value is rendered as a quality status tag. */
  status?: string
}

export interface GenealogyItem {
  key: string
  index: string
  title: string
  detail: string
  code: string
  note: string
  qrUrl: string
  qrAlt: string
}

export interface GenealogyView {
  kind: 'batch' | 'unit'
  subtitle: string
  qrUrl: string
  qrAlt: string
  codeLabel: string
  code: string
  codeHint: string
  summary: GenealogySummaryItem[]
  sectionLabel: string
  sectionTitle: string
  sectionHint: string
  items: GenealogyItem[]
  emptyText: string
}

/** Batch genealogy: the whole batch plus the supplier batches it consumed. */
export function batchGenealogyView(data: AnyRecord, batchId: number): GenealogyView {
  const registration: AnyRecord = data.registration ?? {}
  const registered = Boolean(registration.registered)
  const consumption: AnyRecord[] = Array.isArray(data.reverseTrace) ? data.reverseTrace : []
  const id = positiveId(data.id) ?? positiveId(batchId)
  return {
    kind: 'batch',
    subtitle: '整批走步机的供应批次族谱',
    qrUrl: id ? batchesApi.productionBatchQrUrl(id) : '',
    qrAlt: '批次二维码',
    codeLabel: '批次码',
    code: String(data.identificationCode || data.batchCode || '-'),
    codeHint: '扫码可查看整批只读信息',
    summary: [
      { label: '产品', value: String(data.productName || '-') },
      { label: '计划台数', value: String(data.plannedQuantity ?? '-') },
      { label: '登记台数', value: registered ? String(registration.registeredQuantity ?? '-') : '未登记' },
      registered
        ? { label: '质量状态', value: '', status: String(registration.qualityStatus || 'ASSEMBLED') }
        : { label: '质量状态', value: '未登记' },
    ],
    sectionLabel: '供应来源',
    sectionTitle: `${consumption.length} 条供应批次消耗`,
    sectionHint: '本批次消耗的供应批次、供应部件与数量',
    items: consumption.map((item, index) => ({
      key: `consumption-${index}`,
      index: String(index + 1),
      title: String(item.partName || item.partCode || '-'),
      detail: `${item.supplierName ? `${item.supplierName} · ` : ''}消耗 ${item.quantityConsumed ?? '-'}`,
      code: String(item.supplierBatchNo || item.supplierInventoryBatchId || '未关联供应批次'),
      note: '',
      qrUrl: '',
      qrAlt: '',
    })),
    emptyText: '暂无供应批次消耗记录',
  }
}

/** Unit genealogy (GET /api/genealogy): a main code down to its parts, or a part code up to its product. */
export function unitGenealogyView(data: AnyRecord): GenealogyView {
  const record: AnyRecord | undefined = Array.isArray(data.records) ? data.records[0] : undefined
  const machine: AnyRecord | undefined = data.machine || record?.machine
  const parts: AnyRecord[] = Array.isArray(record?.parts) ? record.parts : (data.partLabel ? [data.partLabel] : [])
  const machineId = positiveId(machine?.id)
  return {
    kind: 'unit',
    subtitle: data.queryType === 'MACHINE' ? '从产品主码向下查看全部部件' : '从部件码向上查看所属产品',
    qrUrl: machineId ? recordsApi.machineQrUrl(machineId) : '',
    qrAlt: '产品主码二维码',
    codeLabel: '产品主码',
    code: String(machine?.identificationCode || '尚未录入'),
    codeHint: '扫码可再次进入该产品完整族谱',
    summary: [
      { label: '产品', value: String(machine?.productModelName || machine?.model || '-') },
      { label: '生成批次', value: generationBatchLabel(record) },
      { label: '完成日期', value: record ? formatGroupDay(record.completedAt) : '未完成' },
      record
        ? { label: '质量状态', value: '', status: String(record.status || '') }
        : { label: '质量状态', value: '未完成' },
    ],
    sectionLabel: '组成部件',
    sectionTitle: `${parts.length} 个已绑定部件`,
    sectionHint: '每个部件的编码与二维码一一对应',
    items: parts.map((part, index) => {
      const labelId = positiveId(part.id)
      return {
        key: `part-${labelId ?? index}`,
        index: String(part.position || index + 1),
        title: String(part.partName || '-'),
        detail: String(part.supplierName || ''),
        code: String(part.identificationCode || ''),
        note: String(part.batchCode || part.supplierBatchNo || '未关联部件批次'),
        qrUrl: labelId ? recordsApi.partLabelQrUrl(labelId) : '',
        qrAlt: `${part.partName || ''}二维码`,
      }
    }),
    emptyText: '暂无部件记录',
  }
}
