<script setup lang="ts">
/**
 * 生产订单（管理员、仓管）— flow step 1: tick the purchase orders operations
 * submitted and generate one production order per order in one go, each
 * minting a unique production QR code; or mark them 外采 (externally
 * procured) and generate. The batch call is idempotent (scope
 * `production-orders.batch`) and reports created / skipped (already
 * generated) / failed per purchase order.
 *
 * The list shows where each order stands in the warehouse flow
 * (批次登记 → 质量放行 → 入库) and opens the production QR for printing.
 */
import type { AnyRecord } from '@/api/types'
import type { TagType } from '@/utils/format'
import operationsApi from '@/api/modules/operations'
import { notifyError, notifySuccess } from '@/utils/feedback'
import { formatDate, formatNumber, qualityStatus } from '@/utils/format'
import ProductionQrDialog from './components/ProductionQrDialog.vue'

defineOptions({
  name: 'ProductionOrdersPage',
})

const loading = ref(false)
const loaded = ref(false)
const purchaseOrders = ref<AnyRecord[]>([])
const productionOrders = ref<AnyRecord[]>([])

async function loadProductionOrders() {
  loading.value = true
  try {
    const [orders, list] = await Promise.all([operationsApi.purchaseOrders(), operationsApi.productionOrders()])
    purchaseOrders.value = orders
    productionOrders.value = list
    selected.value = new Set()
    loaded.value = true
  }
  catch (error) {
    notifyError(error, '生产订单加载失败')
  }
  finally {
    loading.value = false
  }
}

// ---- pending purchase orders --------------------------------------------------

const selected = ref(new Set<number>())
const generating = ref<'internal' | 'external' | null>(null)

/** Purchase orders that have no production order yet. */
const pendingOrders = computed(() => {
  const used = new Set(productionOrders.value.map(item => Number(item.purchaseOrderId)))
  return purchaseOrders.value.filter(item => !used.has(Number(item.id)))
})
const pendingSummary = computed(() => (loaded.value
  ? `${pendingOrders.value.length} 张采购单待生成生产订单`
  : '勾选采购单后批量生成生产订单，每单生成唯一生产二维码'))
const allSelected = computed(() => pendingOrders.value.length > 0
  && pendingOrders.value.every(item => selected.value.has(Number(item.id))))
const someSelected = computed(() => !allSelected.value
  && pendingOrders.value.some(item => selected.value.has(Number(item.id))))

function toggleOrder(id: number, checked: unknown) {
  const next = new Set(selected.value)
  if (checked) {
    next.add(id)
  }
  else {
    next.delete(id)
  }
  selected.value = next
}

function toggleAll(checked: unknown) {
  selected.value = checked ? new Set(pendingOrders.value.map(item => Number(item.id))) : new Set()
}

function pendingName(order: AnyRecord) {
  return order.summary?.productName || order.productModelName || order.partName || '-'
}

function pendingSku(order: AnyRecord) {
  return order.summary?.sku || order.productModelCode || order.partCode || ''
}

function pendingPlanned(order: AnyRecord) {
  const planned = order.summary?.orderedQuantity ?? order.quantity
  return planned === null || planned === undefined || planned === '' ? '-' : formatNumber(planned)
}

function pendingReceived(order: AnyRecord) {
  const received = order.summary?.receivedQuantity
  return received === null || received === undefined ? '-' : formatNumber(received)
}

async function generateProductionOrders(external: boolean) {
  if (generating.value) {
    return
  }
  const ids = pendingOrders.value.map(item => Number(item.id)).filter(id => selected.value.has(id))
  if (!ids.length) {
    notifyError('请至少选择一张待生成的采购订单', '请先勾选采购单')
    return
  }
  generating.value = external ? 'external' : 'internal'
  try {
    const result = await operationsApi.createProductionOrders({ purchaseOrderIds: ids, external })
    await loadProductionOrders()
    const created = (result?.created ?? []).length
    const skipped = (result?.skipped ?? []).length
    const failed = result?.failed ?? []
    const parts = [`生成 ${created} 单`]
    if (skipped) {
      parts.push(`跳过 ${skipped} 单（已生成）`)
    }
    if (failed.length) {
      parts.push(`失败 ${failed.length} 单`)
    }
    const title = external ? '外采生产订单已生成' : '生产订单已生成'
    if (failed.length) {
      notifyError(parts.join(' · '), title)
      notifyError(failed.map(item => `${item.poNo || item.purchaseOrderId}：${item.message}`).join('；'), '部分采购单生成失败')
    }
    else {
      notifySuccess(title, parts.join(' · '))
    }
  }
  catch (error) {
    notifyError(error, external ? '外采生产订单生成失败' : '生产订单生成失败')
  }
  finally {
    generating.value = null
  }
}

// ---- production order list ------------------------------------------------------

interface FlowState {
  stage: string
  tone: TagType
  detail: string
  blocked: string
}

// Where a production order sits in the warehouse flow: 批次登记 → 质量放行 → 入库.
function flowState(order: AnyRecord): FlowState {
  const progress: AnyRecord = order.progress ?? {}
  const received = progress.receivedQuantity ?? 0
  const planned = order.quantity ?? 0
  let stage = '待登记'
  let tone: TagType = 'info'
  if (progress.fullyReceived) {
    stage = '入库完成'
    tone = 'success'
  }
  else if (received > 0) {
    stage = '部分入库'
    tone = 'warning'
  }
  else if (progress.registered) {
    stage = '待入库'
    tone = 'warning'
  }
  const registration = progress.registered
    ? (progress.qualityStatus ? qualityStatus(progress.qualityStatus).label : '待检')
    : '未登记'
  return {
    stage,
    tone,
    detail: `入库 ${received} / ${planned} · ${registration}`,
    blocked: progress.canStockIn === false ? String(progress.stockInBlockedReason || '') : '',
  }
}

function qrUrl(order: AnyRecord) {
  return operationsApi.productionOrderQrUrl(Number(order.id))
}

const qrVisible = ref(false)
const qrOrder = ref<AnyRecord | null>(null)

function openProductionQr(order: AnyRecord) {
  qrOrder.value = order
  qrVisible.value = true
}

onMounted(loadProductionOrders)
</script>

<template>
  <div class="pts-page">
    <FaPageHeader title="生产订单" description="流程第 1 步：勾选运营提交的采购单批量生成生产订单，每单生成唯一溯源码；也可标记为外采直接生成" />

    <FaPageMain title="待生成采购单">
      <div class="pts-toolbar">
        <span id="pending-po-summary" class="pts-muted text-sm">{{ pendingSummary }}</span>
        <div class="flex flex-wrap gap-2">
          <ElButton
            type="primary"
            :loading="generating === 'internal'"
            :disabled="generating === 'external'"
            @click="generateProductionOrders(false)"
          >
            <template #icon>
              <FaIcon name="i-ri:qr-code-line" />
            </template>
            批量生成生产订单
          </ElButton>
          <ElButton
            :loading="generating === 'external'"
            :disabled="generating === 'internal'"
            @click="generateProductionOrders(true)"
          >
            <template #icon>
              <FaIcon name="i-ri:truck-line" />
            </template>
            标记为外采并生成
          </ElButton>
        </div>
      </div>
      <ElTable id="pending-purchase-order-table" v-loading="loading" :data="pendingOrders" row-key="id" empty-text="暂无待生成的采购订单" stripe>
        <ElTableColumn width="52" align="center">
          <template #header>
            <ElCheckbox
              id="pending-po-check-all"
              :model-value="allSelected"
              :indeterminate="someSelected"
              :disabled="!pendingOrders.length"
              aria-label="全选采购单"
              @change="toggleAll"
            />
          </template>
          <template #default="{ row }">
            <ElCheckbox
              :model-value="selected.has(Number(row.id))"
              :aria-label="`选择采购单 ${row.poNo}`"
              @change="toggleOrder(Number(row.id), $event)"
            />
          </template>
        </ElTableColumn>
        <ElTableColumn label="采购单" min-width="200">
          <template #default="{ row }">
            <span class="pts-cell-main pts-code">{{ row.poNo }}</span>
            <span class="pts-cell-sub">采购人：{{ row.createdBy || '-' }}</span>
          </template>
        </ElTableColumn>
        <ElTableColumn label="产品" min-width="200">
          <template #default="{ row }">
            <span class="pts-cell-main">{{ pendingName(row) }}</span>
            <span class="pts-cell-sub pts-code">{{ pendingSku(row) }}</span>
          </template>
        </ElTableColumn>
        <ElTableColumn label="计划数量" width="110">
          <template #default="{ row }">
            {{ pendingPlanned(row) }}
          </template>
        </ElTableColumn>
        <ElTableColumn label="实际到货" width="110">
          <template #default="{ row }">
            {{ pendingReceived(row) }}
          </template>
        </ElTableColumn>
      </ElTable>
    </FaPageMain>

    <FaPageMain title="生产订单列表">
      <ElTable id="production-order-table" v-loading="loading" :data="productionOrders" row-key="id" empty-text="暂无生产订单" stripe>
        <ElTableColumn label="生产订单" width="100">
          <template #default="{ row }">
            #{{ row.id }}
          </template>
        </ElTableColumn>
        <ElTableColumn label="来源采购单" min-width="170">
          <template #default="{ row }">
            <span class="pts-code">{{ row.poNo || '-' }}</span>
          </template>
        </ElTableColumn>
        <ElTableColumn label="产品型号" min-width="170">
          <template #default="{ row }">
            <span class="pts-cell-main">{{ row.productName || '-' }}</span>
            <span class="pts-cell-sub pts-code">{{ row.productModelCode || '' }}</span>
          </template>
        </ElTableColumn>
        <ElTableColumn label="类型" width="80">
          <template #default="{ row }">
            <ElTag :type="row.isExternal ? 'warning' : 'info'" disable-transitions>
              {{ row.isExternal ? '外采' : '自产' }}
            </ElTag>
          </template>
        </ElTableColumn>
        <ElTableColumn label="数量" width="80">
          <template #default="{ row }">
            {{ formatNumber(row.quantity) }}
          </template>
        </ElTableColumn>
        <ElTableColumn label="流程进度" min-width="200">
          <template #default="{ row }">
            <ElTag :type="flowState(row).tone" disable-transitions>
              {{ flowState(row).stage }}
            </ElTag>
            <span class="pts-cell-sub mt-1">{{ flowState(row).detail }}</span>
            <span v-if="flowState(row).blocked" class="pts-cell-sub pts-text-warning">{{ flowState(row).blocked }}</span>
          </template>
        </ElTableColumn>
        <ElTableColumn label="生产二维码" min-width="210">
          <template #default="{ row }">
            <div class="flex gap-2 items-center">
              <button
                type="button"
                class="pts-qr-thumb-button"
                title="查看 / 打印生产二维码"
                :aria-label="`查看 / 打印生产二维码 ${row.productionQrCode}`"
                @click="openProductionQr(row)"
              >
                <img class="pts-qr-thumb" :src="qrUrl(row)" alt="" loading="lazy">
              </button>
              <span class="pts-cell-sub pts-code">{{ row.productionQrCode }}</span>
            </div>
          </template>
        </ElTableColumn>
        <ElTableColumn label="生成时间" width="150">
          <template #default="{ row }">
            {{ formatDate(row.createdAt) }}
          </template>
        </ElTableColumn>
        <ElTableColumn label="操作" width="130" align="right" fixed="right">
          <template #default="{ row }">
            <ElButton size="small" @click="openProductionQr(row)">
              <template #icon>
                <FaIcon name="i-ri:printer-line" />
              </template>
              查看 / 打印
            </ElButton>
          </template>
        </ElTableColumn>
      </ElTable>
    </FaPageMain>

    <div class="mx-4">
      <ElAlert
        type="info"
        :closable="false"
        show-icon
        title="后续步骤"
        description="生成生产订单后：① 打印生产二维码随批流转；② 到「现场批次登记」扫码登记整批；③ 由管理员在「批次质量处理」放行或暂扣；④ 到「扫码枪入库」扫码登记成品库存。暂扣的批次不能入库；开启「质量放行」后需先放行才能入库。"
      />
    </div>

    <ProductionQrDialog v-model="qrVisible" :order="qrOrder" />
  </div>
</template>
