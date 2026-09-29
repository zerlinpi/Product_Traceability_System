<script setup lang="ts">
/**
 * 生产二维码 — view, download (SVG) and print one production order's QR label.
 *
 * Printing goes through utils/print.ts: print CSS shows only the label, so
 * the authenticated same-origin QR image prints without opening a window
 * (the CSP forbids writing HTML into one).
 */
import type { AnyRecord } from '@/api/types'
import operationsApi from '@/api/modules/operations'
import { printElement } from '@/utils/print'

defineOptions({
  name: 'ProductionQrDialog',
})

const props = defineProps<{
  order: AnyRecord | null
}>()

const visible = defineModel<boolean>({ default: false })

const printArea = ref<HTMLElement>()

const external = computed(() => Boolean(props.order?.isExternal))
const qrUrl = computed(() => (props.order ? operationsApi.productionOrderQrUrl(Number(props.order.id)) : ''))
const subtitle = computed(() => {
  const order = props.order
  if (!order) {
    return ''
  }
  return `${order.poNo ?? ''} · 计划 ${order.quantity ?? '-'} 件${external.value ? ' · 外采' : ''}`
})
const productLine = computed(() => {
  const order = props.order
  if (!order) {
    return ''
  }
  return `${order.productName ?? ''} · ${order.productModelCode ?? ''}${external.value ? ' · 外采' : ''}`
})
const downloadName = computed(() => `${props.order?.productionQrCode || 'production-qr'}.svg`)

function printProductionQr() {
  printElement(printArea.value)
}
</script>

<template>
  <ElDialog v-model="visible" title="生产二维码" width="440px" :close-on-click-modal="false" append-to-body>
    <p id="production-qr-sub" class="pts-muted text-sm mt-0 mb-4">
      {{ subtitle }}
    </p>
    <div v-if="order" id="production-qr-print" ref="printArea" class="pts-label pts-production-qr-label">
      <img id="production-qr-image" :src="qrUrl" alt="生产二维码">
      <strong id="production-qr-product">{{ productLine }}</strong>
      <span id="production-qr-code" class="pts-label-code">{{ order.productionQrCode }}</span>
    </div>
    <template #footer>
      <ElButton id="production-qr-download" tag="a" :href="qrUrl" :download="downloadName" target="_blank" rel="noopener">
        <template #icon>
          <FaIcon name="i-ri:download-2-line" />
        </template>
        下载二维码
      </ElButton>
      <ElButton id="production-qr-print-btn" type="primary" @click="printProductionQr">
        <template #icon>
          <FaIcon name="i-ri:printer-line" />
        </template>
        打印
      </ElButton>
    </template>
  </ElDialog>
</template>
