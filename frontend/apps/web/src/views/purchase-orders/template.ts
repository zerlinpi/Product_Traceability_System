/**
 * The Lingxing purchase-order template (采购单模板) as the operator fills it.
 *
 * The columns match PURCHASE_ORDER_EXPORT_COLUMNS in traceability/purchasing.py
 * minus 标识号, which is the system row id. The export keeps the exact template
 * column order, so this list must stay in that order. Ported one-for-one from
 * the previous UI (PURCHASE_ORDER_* in static/app_v2.js).
 */

export const PURCHASE_ORDER_FIELD_COLUMNS = [
  '采购单号', '供应商', '联系人', '采购方', '联系方式', '结算方式', '预付比例',
  '结算账期', '结算描述', '支付方式', '含税', '费用分配方式', '采购币种', '当前汇率',
  '运费', '运费币种', '其他费用', '其他费用币种', '采购员', '质检类型', '单据备注',
  '颜色', '材质', '内含配件', '包装要求', '特殊要求', 'HS海关编码', '交货周期（天数）',
  '采购仓库', '计划编号', 'SKU', '店铺', 'FNSKU', '是否赠品', '单箱数量', '箱数',
  '实际采购量', '含税单价', '税率', '预计到货时间', '产品备注', '更新报价',
  '内含配件(产品)', '包装要求(产品)', '特殊要求（规避专利）', 'HS海关编码(产品)',
  '交货周期（天数）(产品)',
] as const

/** Kept numeric in the exported workbook (same set as the backend). */
export const PURCHASE_ORDER_NUMERIC_COLUMNS = new Set<string>([
  '预付比例', '结算账期', '当前汇率', '运费', '其他费用', '单箱数量', '箱数',
  '实际采购量', '含税单价', '税率',
])

/** Shown up front; everything else lives in the collapsible 更多字段 group. */
export const PURCHASE_ORDER_PRIMARY_COLUMNS = [
  '采购单号', 'SKU', '供应商', '联系人', '联系方式', '采购币种',
  '实际采购量', '含税单价', '税率', '含税', '预计到货时间', '采购仓库',
]

/** Editable defaults pre-filled on a new order (operators can still change them). */
export const PURCHASE_ORDER_FIELD_DEFAULTS: Record<string, string> = {
  采购币种: 'CNY',
  含税: '是',
  费用分配方式: '按数量',
}

const YES_NO = ['是', '否']
const CURRENCIES = ['CNY', 'USD', 'EUR', 'JPY', 'GBP', 'HKD']

/** Controlled inputs: operators pick valid values instead of free-typing. */
export const PURCHASE_ORDER_FIELD_OPTIONS: Record<string, string[]> = {
  含税: YES_NO,
  是否赠品: YES_NO,
  更新报价: YES_NO,
  采购币种: CURRENCIES,
  运费币种: CURRENCIES,
  其他费用币种: CURRENCIES,
  费用分配方式: ['按数量', '按金额', '按体积', '按重量'],
  结算方式: ['预付', '月结', '货到付款', '分期', '其他'],
  支付方式: ['电汇 T/T', '信用证 L/C', '支付宝', '微信', '银行转账', '其他'],
  质检类型: ['全检', '抽检', '免检'],
}

export const PURCHASE_ORDER_DATE_COLUMNS = new Set<string>(['预计到货时间'])

/** The document number column: blank means the server generates one. */
export const PO_NO_COLUMN = '采购单号'

export type FieldKind = 'select' | 'date' | 'number' | 'text'

export interface FieldSpec {
  column: string
  kind: FieldKind
  options: string[]
  placeholder: string
}

function fieldSpec(column: string): FieldSpec {
  const options = PURCHASE_ORDER_FIELD_OPTIONS[column]
  let kind: FieldKind = 'text'
  if (options) {
    kind = 'select'
  }
  else if (PURCHASE_ORDER_DATE_COLUMNS.has(column)) {
    kind = 'date'
  }
  else if (PURCHASE_ORDER_NUMERIC_COLUMNS.has(column)) {
    kind = 'number'
  }
  return {
    column,
    kind,
    options: options ?? [],
    placeholder: column === PO_NO_COLUMN ? '留空自动生成' : '',
  }
}

const ALL_COLUMNS: readonly string[] = PURCHASE_ORDER_FIELD_COLUMNS

export const PRIMARY_FIELDS: FieldSpec[] = PURCHASE_ORDER_PRIMARY_COLUMNS
  .filter(column => ALL_COLUMNS.includes(column))
  .map(fieldSpec)

export const MORE_FIELDS: FieldSpec[] = ALL_COLUMNS
  .filter(column => !PURCHASE_ORDER_PRIMARY_COLUMNS.includes(column))
  .map(fieldSpec)

/** Every template column, blank except for the editable defaults. */
export function defaultFieldValues(): Record<string, string> {
  return Object.fromEntries(ALL_COLUMNS.map(column => [column, PURCHASE_ORDER_FIELD_DEFAULTS[column] ?? '']))
}

/**
 * Form values for an existing order (edit / copy). Values are taken exactly
 * as stored (no defaults), and a copy gets a fresh document number.
 */
export function fieldValuesFromOrder(fields: Record<string, unknown> | null | undefined, { keepPoNo }: { keepPoNo: boolean }) {
  const source = fields ?? {}
  const values: Record<string, string> = {}
  let hasMoreValue = false
  for (const column of ALL_COLUMNS) {
    const raw = source[column]
    const value = raw === null || raw === undefined ? '' : String(raw)
    if (column === PO_NO_COLUMN && !keepPoNo) {
      values[column] = ''
      continue
    }
    values[column] = value
    if (value && !PURCHASE_ORDER_PRIMARY_COLUMNS.includes(column)) {
      hasMoreValue = true
    }
  }
  return { values, hasMoreValue }
}

/** The payload `fields` object: trimmed, and blank columns left out. */
export function fieldsPayload(values: Record<string, string>) {
  const fields: Record<string, string> = {}
  for (const column of ALL_COLUMNS) {
    const value = String(values[column] ?? '').trim()
    if (value !== '') {
      fields[column] = value
    }
  }
  return fields
}
