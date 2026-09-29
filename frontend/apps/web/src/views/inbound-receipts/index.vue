<script setup lang="ts">
/**
 * 供应收货（管理员、仓管）— register supplier goods arriving against a
 * purchase order. This is booked separately from finished-goods stock-in
 * (扫码枪入库); operations later push each receipt to Lingxing from
 * 采购订单 → 供应收货同步 (推送入库).
 */
import type { FormInstance, FormRules } from 'element-plus'
import type { AnyRecord } from '@/api/types'
import operationsApi from '@/api/modules/operations'
import { notifyError, notifySuccess } from '@/utils/feedback'
import { formatDate, formatNumber, syncStatus } from '@/utils/format'
import PurchaseOrderSummary from './components/PurchaseOrderSummary.vue'

defineOptions({
  name: 'InboundReceiptsPage',
})

const loading = ref(false)
const purchaseOrders = ref<AnyRecord[]>([])
const receipts = ref<AnyRecord[]>([])

const formRef = ref<FormInstance>()
const submitting = ref(false)
const form = reactive({
  purchaseOrderId: undefined as number | undefined,
  quantity: undefined as number | null | undefined,
})
const rules: FormRules = {
  purchaseOrderId: [{ required: true, message: '请选择采购订单', trigger: 'change' }],
  quantity: [{ required: true, message: '请填写收货数量（1-999999 之间的整数）', trigger: 'blur' }],
}

const selectedOrder = computed(() => purchaseOrders.value.find(item => Number(item.id) === form.purchaseOrderId) ?? null)

// Free-form orders have no linked 商品 (part), so fall back to the product /
// SKU instead of printing an empty name.
function purchaseOrderOptionLabel(order: AnyRecord) {
  const summary: AnyRecord = order.summary ?? {}
  const name = summary.productName || order.productModelName || order.partName || ''
  const sku = summary.sku || order.productModelCode || order.partCode || ''
  const parts = [order.poNo]
  if (name) {
    parts.push(name)
  }
  else if (sku) {
    parts.push(sku)
  }
  return parts.join(' · ')
}

function partLabel(receipt: AnyRecord) {
  return [receipt.partName, receipt.partCode].filter(Boolean).join(' · ')
}

async function loadInboundReceipts() {
  loading.value = true
  try {
    const [orders, list] = await Promise.all([operationsApi.purchaseOrders(), operationsApi.inboundReceipts()])
    purchaseOrders.value = orders
    receipts.value = list
    // Keep the selected order when it still exists.
    if (form.purchaseOrderId !== undefined && !orders.some(item => Number(item.id) === form.purchaseOrderId)) {
      form.purchaseOrderId = undefined
    }
  }
  catch (error) {
    notifyError(error, '供应收货加载失败')
  }
  finally {
    loading.value = false
  }
}

async function submitInboundReceipt() {
  if (!formRef.value || submitting.value) {
    return
  }
  const valid = await formRef.value.validate().catch(() => false)
  if (!valid || form.purchaseOrderId === undefined || form.quantity === null || form.quantity === undefined) {
    return
  }
  submitting.value = true
  try {
    await operationsApi.createInboundReceipt({ purchaseOrderId: form.purchaseOrderId, quantity: Number(form.quantity) })
    form.quantity = undefined
    nextTick(() => formRef.value?.clearValidate('quantity'))
    await loadInboundReceipts()
    notifySuccess('供应收货已登记')
  }
  catch (error) {
    notifyError(error, '供应收货登记失败')
  }
  finally {
    submitting.value = false
  }
}

onMounted(loadInboundReceipts)
</script>

<template>
  <div class="pts-page">
    <FaPageHeader title="供应收货" description="针对采购订单登记供应到货，和成品扫码入库分别记账" />
    <FaPageMain title="登记供应收货">
      <ElForm id="inbound-receipt-form" ref="formRef" :model="form" :rules="rules" label-position="top" @submit.prevent="submitInboundReceipt">
        <div class="gap-x-5 grid grid-cols-1 md:grid-cols-2">
          <ElFormItem label="采购订单" prop="purchaseOrderId">
            <ElSelect v-model="form.purchaseOrderId" name="purchaseOrderId" placeholder="请选择采购订单" filterable class="w-full">
              <ElOption v-for="order in purchaseOrders" :key="order.id" :label="purchaseOrderOptionLabel(order)" :value="Number(order.id)" />
            </ElSelect>
          </ElFormItem>
          <ElFormItem label="收货数量" prop="quantity">
            <ElInputNumber
              v-model="form.quantity"
              name="quantity"
              :min="1"
              :max="999999"
              :step="1"
              step-strictly
              controls-position="right"
              placeholder="1 - 999999"
              class="w-full!"
            />
          </ElFormItem>
        </div>
        <div class="mb-3 flex flex-wrap gap-x-3 gap-y-1 items-baseline">
          <strong class="text-sm font-semibold">订单明细</strong>
          <span class="pts-muted text-xs">核对采购单信息与到货数量、金额</span>
        </div>
        <PurchaseOrderSummary id="inbound-receipt-summary" :order="selectedOrder" />
        <div class="mt-4 flex justify-end">
          <ElButton type="primary" native-type="submit" :loading="submitting">
            <template #icon>
              <FaIcon name="i-ri:inbox-archive-line" />
            </template>
            登记收货
          </ElButton>
        </div>
      </ElForm>
    </FaPageMain>

    <FaPageMain title="供应收货记录">
      <ElTable id="inbound-receipt-table" v-loading="loading" :data="receipts" row-key="id" empty-text="暂无供应收货记录" stripe>
        <ElTableColumn label="收货记录" width="100">
          <template #default="{ row }">
            #{{ row.id }}
          </template>
        </ElTableColumn>
        <ElTableColumn label="采购单号" min-width="180">
          <template #default="{ row }">
            <span class="pts-code">{{ row.poNo || '-' }}</span>
          </template>
        </ElTableColumn>
        <ElTableColumn label="供应商 / 商品" min-width="200">
          <template #default="{ row }">
            <span class="pts-cell-main">{{ row.supplierName || '-' }}</span>
            <span class="pts-cell-sub">{{ partLabel(row) }}</span>
          </template>
        </ElTableColumn>
        <ElTableColumn label="数量" width="90">
          <template #default="{ row }">
            {{ formatNumber(row.quantity) }}
          </template>
        </ElTableColumn>
        <ElTableColumn label="收货人" width="120">
          <template #default="{ row }">
            {{ row.receiver || '-' }}
          </template>
        </ElTableColumn>
        <ElTableColumn label="同步状态" width="110">
          <template #default="{ row }">
            <ElTag :type="syncStatus(row.syncStatus).type" disable-transitions>
              {{ syncStatus(row.syncStatus).label }}
            </ElTag>
          </template>
        </ElTableColumn>
        <ElTableColumn label="收货时间" width="150">
          <template #default="{ row }">
            {{ formatDate(row.receivedAt) }}
          </template>
        </ElTableColumn>
      </ElTable>
    </FaPageMain>
  </div>
</template>
