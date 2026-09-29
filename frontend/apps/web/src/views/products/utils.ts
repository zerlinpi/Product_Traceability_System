/**
 * Shared helpers for 产品管理 / 我的产品: the 产品资料 presentation rules, the
 * BOM grouping and the stock state used by the list filter.
 *
 * The profile column list itself comes from the server
 * (GET /api/product-attribute-columns) so the client never hardcodes the
 * template; the constants below only decide how a column is presented.
 */
import type { ProductAttributeMeta } from '@/api/modules/catalog'
import type { AnyRecord } from '@/api/types'
import type { TagType } from '@/utils/format'
import catalogApi from '@/api/modules/catalog'

/** Columns shown up front in the form; the long tail sits under 更多产品资料. */
export const PRODUCT_ATTRIBUTE_PRIMARY_COLUMNS = [
  '主图',
  'SPU',
  '款名',
  '型号',
  '单位',
  '品牌',
  '一级分类',
  '二级分类',
  '产品类型',
  '供应商名称',
  '采购员',
  '产品负责人',
  '采购交期',
  '最小采购量',
  '币种',
  '含税',
  '税率',
  '单价',
  '含税单价',
  '采购成本(CNY)',
  '产品材质',
]

export const PRODUCT_ATTRIBUTE_NUMERIC_COLUMNS = new Set([
  '单位加工费',
  '关联单品成本',
  '采购成本(CNY)',
  '单品规格长',
  '单品规格宽',
  '单品规格高',
  '单品净重',
  '单品毛重',
  '包装规格长',
  '包装规格宽',
  '包装规格高',
  '外箱规格长',
  '外箱规格宽',
  '外箱规格高',
  '单箱重量',
  '单箱数量(pcs)',
  '税率',
  '最小采购量',
  '单价',
  '含税单价',
  '报关单价',
  '默认清关单价',
  '默认清关税率',
  '全部国家头程费用(含税)',
])

export const PRODUCT_ATTRIBUTE_TEXTAREA_COLUMNS = new Set([
  '产品描述',
  '产品描述(纯文本)',
  '加工备注',
  '采购备注',
  '报价备注',
  '其他申报要素',
  '配货备注',
  '默认清关备注',
])

const PRODUCT_CURRENCIES = ['CNY', 'USD', 'EUR', 'JPY', 'GBP', 'HKD']
const LENGTH_UNITS = ['cm', 'mm', 'm', 'inch']
const WEIGHT_UNITS = ['g', 'kg', 'lb']

export const PRODUCT_ATTRIBUTE_OPTIONS: Record<string, string[]> = {
  含税: ['是', '否'],
  币种: PRODUCT_CURRENCIES,
  报关单价币种: PRODUCT_CURRENCIES,
  默认清关单价币种: PRODUCT_CURRENCIES,
  全部国家头程费用币种: PRODUCT_CURRENCIES,
  默认质检方式: ['全检', '抽检', '免检'],
  品牌类型: ['自主品牌', '他人品牌', '无品牌'],
  单品规格单位: LENGTH_UNITS,
  包装规格单位: LENGTH_UNITS,
  外箱规格单位: LENGTH_UNITS,
  单品净重单位: WEIGHT_UNITS,
  单品毛重单位: WEIGHT_UNITS,
  单箱重量单位: WEIGHT_UNITS,
}

export const PRODUCT_ATTRIBUTE_DATE_COLUMNS = new Set(['交货期', '交期'])

/** The profile column holding the list thumbnail. */
export const PRODUCT_THUMB_COLUMN = '主图'

/** Upload limit of POST /api/product-images. */
export const PRODUCT_IMAGE_MAX_BYTES = 5 * 1024 * 1024

export type AttributeFieldKind = 'image' | 'select' | 'textarea' | 'date' | 'number' | 'text'

export function attributeFieldKind(column: string, imageColumns: string[]): AttributeFieldKind {
  if (imageColumns.includes(column)) {
    return 'image'
  }
  if (PRODUCT_ATTRIBUTE_OPTIONS[column]) {
    return 'select'
  }
  if (PRODUCT_ATTRIBUTE_TEXTAREA_COLUMNS.has(column)) {
    return 'textarea'
  }
  if (PRODUCT_ATTRIBUTE_DATE_COLUMNS.has(column)) {
    return 'date'
  }
  if (PRODUCT_ATTRIBUTE_NUMERIC_COLUMNS.has(column)) {
    return 'number'
  }
  return 'text'
}

/**
 * A stored picture reference is only rendered when it is a same-origin path
 * (the upload endpoint returns `/api/product-images/<name>`). Anything else a
 * client might have written into the column is shown as text, never loaded.
 */
export function safeImageUrl(value: unknown): string {
  const text = String(value ?? '').trim()
  return /^\/(?![/\\])/.test(text) ? text : ''
}

export function productThumbUrl(product: AnyRecord): string {
  return safeImageUrl(product.attributes?.[PRODUCT_THUMB_COLUMN])
}

// ---- 产品资料 template ---------------------------------------------------

const FALLBACK_META: ProductAttributeMeta = { columns: [], derived: [], imageColumns: [PRODUCT_THUMB_COLUMN] }
let cachedMeta: ProductAttributeMeta | null = null
let pendingMeta: Promise<ProductAttributeMeta> | null = null

/**
 * The profile template, fetched once per page load. A failed request falls
 * back to an empty template (the form then only asks for the name) and is
 * retried the next time a form opens.
 */
export function loadProductAttributeMeta(): Promise<ProductAttributeMeta> {
  if (cachedMeta) {
    return Promise.resolve(cachedMeta)
  }
  if (!pendingMeta) {
    pendingMeta = catalogApi.productAttributeColumns()
      .then((meta) => {
        cachedMeta = {
          columns: meta?.columns ?? [],
          derived: meta?.derived ?? [],
          imageColumns: meta?.imageColumns?.length ? meta.imageColumns : [PRODUCT_THUMB_COLUMN],
        }
        return cachedMeta
      })
      .catch(() => FALLBACK_META)
      .finally(() => {
        pendingMeta = null
      })
  }
  return pendingMeta
}

/**
 * The profile columns that carry a value, in template order (falling back to
 * the stored key order when the template is unavailable).
 */
export function filledAttributes(product: AnyRecord | null, meta: ProductAttributeMeta) {
  const attributes: AnyRecord = product?.attributes ?? {}
  const columns = meta.columns.length ? meta.columns : Object.keys(attributes)
  return columns
    .filter(column => attributes[column] !== null && attributes[column] !== undefined && String(attributes[column]).trim() !== '')
    .map((column) => {
      const value = String(attributes[column])
      const image = meta.imageColumns.includes(column) ? safeImageUrl(value) : ''
      return { column, value, image }
    })
}

// ---- BOM ---------------------------------------------------------------------

export interface GroupedComponent extends AnyRecord {
  quantity: number
}

/**
 * Trace-plan slots are stored one per physical unit ("电机 1", "电机 2"); the
 * UI shows consecutive slots of the same part and batch as one row × quantity.
 */
export function componentsByName(product: AnyRecord | null): GroupedComponent[] {
  const grouped: GroupedComponent[] = []
  for (const item of (product?.components ?? []) as AnyRecord[]) {
    const last = grouped.at(-1)
    if (last && last.partTypeId === item.partTypeId && last.inventoryBatchId === item.inventoryBatchId) {
      last.quantity += 1
    }
    else {
      grouped.push({ ...item, quantity: 1 })
    }
  }
  return grouped
}

export function componentLabel(item: AnyRecord): string {
  return item.partName || String(item.slotName ?? '').replace(/ \d+$/, '')
}

// ---- stock state -------------------------------------------------------------

export type StockState = 'OUT' | 'LOW' | 'NORMAL'

export const STOCK_STATE: Record<StockState, { label: string, type: TagType }> = {
  OUT: { label: '不可生成', type: 'danger' },
  LOW: { label: '库存紧张', type: 'warning' },
  NORMAL: { label: '库存正常', type: 'success' },
}

/**
 * OUT when no set can be generated (no bound batch or a batch ran dry), LOW
 * when a bound batch is at or below its part's safety stock.
 */
export function stockState(product: AnyRecord): StockState {
  if (product.availableUnits === null || product.availableUnits === undefined || product.availableUnits <= 0) {
    return 'OUT'
  }
  const low = ((product.components ?? []) as AnyRecord[]).some(item => item.minimumStock > 0
    && item.inventoryQuantityAvailable > 0
    && item.inventoryQuantityAvailable <= item.minimumStock)
  return low ? 'LOW' : 'NORMAL'
}

/** `YYYYMMDD` of today (local time), as used in generated set codes. */
export function todayCode(): string {
  const now = new Date()
  return `${now.getFullYear()}${String(now.getMonth() + 1).padStart(2, '0')}${String(now.getDate()).padStart(2, '0')}`
}
