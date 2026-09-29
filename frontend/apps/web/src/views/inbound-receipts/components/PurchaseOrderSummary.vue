<script setup lang="ts">
/**
 * 订单明细 — the selected purchase order's key data (the server's `summary`
 * block: identity, dates, ordered vs received quantity and amount), so the
 * warehouse can confirm what it is receiving before submitting.
 */
import type { AnyRecord } from '@/api/types'
import { formatDate, formatMoney, formatNumber } from '@/utils/format'

defineOptions({
  name: 'PurchaseOrderSummary',
})

const props = defineProps<{
  order: AnyRecord | null
}>()

const rows = computed(() => {
  const summary: AnyRecord = props.order?.summary ?? {}
  const entries: [string, unknown][] = [
    ['SKU', summary.sku],
    ['品名', summary.productName],
    ['店铺', summary.shop],
    ['FNSKU', summary.fnsku],
    ['负责人', summary.owner],
    ['供应商', summary.supplierName],
    ['下单时间', formatDate(summary.orderedAt)],
    ['原计划到货时间', summary.plannedArrivalAt],
    ['实际到货时间', summary.actualArrivalAt ? formatDate(summary.actualArrivalAt) : ''],
    ['单价', formatMoney(summary.unitPrice)],
    ['原采购单数量', formatNumber(summary.orderedQuantity)],
    ['实际到货数量', formatNumber(summary.receivedQuantity)],
    ['采购单金额', formatMoney(summary.orderAmount)],
    ['实际到货金额', formatMoney(summary.receivedAmount)],
  ]
  return entries.map(([label, value]) => ({
    label,
    value: value === '' || value === null || value === undefined ? '-' : String(value),
  }))
})
</script>

<template>
  <dl v-if="order" class="pts-order-summary">
    <div v-for="item in rows" :key="item.label" class="pts-order-summary-item">
      <dt>{{ item.label }}</dt>
      <dd>{{ item.value }}</dd>
    </div>
  </dl>
  <div v-else class="pts-empty pts-order-summary-empty">
    选择采购订单后显示订单明细
  </div>
</template>
