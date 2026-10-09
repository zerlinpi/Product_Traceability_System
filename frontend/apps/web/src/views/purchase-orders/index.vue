<script setup lang="ts">
/**
 * 采购订单（管理员、运营）— operations fill the Lingxing purchase-order
 * template (采购单模板) directly, linked to one of their products ("和产品挂钩").
 *
 * - Create is idempotent (scope `purchase-orders.create`); edit / copy reuse
 *   the same form. A pushed order is locked (no 编辑), and the document number
 *   (采购单号) can never change after creation — both mirror the server rules.
 * - 推送领星 (采购单下单) is enabled only when the purchase-order endpoint is
 *   configured (GET /api/lingxing/status); a failed push is recorded by the
 *   server as 推送失败 with its reason, shown under the status.
 * - 工厂进度 (production order generated / cumulative stock-in) is looked up
 *   for admins only, as before: operations work the procurement side.
 * - 供应收货同步: warehouse receipts that operations push to Lingxing (推送入库).
 */
import type { FormInstance, FormRules } from 'element-plus'
import type { AnyRecord } from '@/api/types'
import { errorMessage } from '@/api'
import catalogApi from '@/api/modules/catalog'
import operationsApi from '@/api/modules/operations'
import { confirmAction, notifyError, notifySuccess } from '@/utils/feedback'
import { formatDate, formatNumber, syncStatus } from '@/utils/format'
import FieldGrid from './components/FieldGrid.vue'
import { defaultFieldValues, fieldsPayload, fieldValuesFromOrder, MORE_FIELDS, PO_NO_COLUMN, PRIMARY_FIELDS } from './template'
import PtsListWindowNotice from '../../components/PtsListWindowNotice.vue'

defineOptions({
  name: 'PurchaseOrdersPage',
})

const appAccountStore = useAppAccountStore()

// ---- purchase-order list ------------------------------------------------------

const loading = ref(false)
const orders = ref<AnyRecord[]>([])
const progressById = ref<Record<number, AnyRecord | null>>({})
const products = ref<AnyRecord[]>([])
/** GET /api/lingxing/status; null when it could not be read. */
const lingxingStatus = ref<AnyRecord | null>(null)
const pushingOrders = ref(new Set<number>())

// 采购单下单 only needs its own endpoint, not the inbound / inventory ones.
const pushReady = computed(() => Boolean(
  lingxingStatus.value?.endpointsConfigured?.purchaseOrder
  ?? lingxingStatus.value?.writeEndpointsConfigured,
))
const exportAllUrl = operationsApi.purchaseOrdersExportUrl()

let loadSequence = 0

async function loadPurchaseOrders() {
  const sequence = ++loadSequence
  loading.value = true
  try {
    const [productList, status, list] = await Promise.all([
      catalogApi.products().catch((error) => {
        notifyError(error, '产品列表加载失败')
        return null
      }),
      // Push availability only gates the 推送领星 button; the list still loads.
      operationsApi.lingxingStatus().catch(() => null),
      operationsApi.purchaseOrders(),
    ])
    // Only admins act on factory progress; skip the per-order lookups for
    // operations (procurement view) to avoid noise and needless requests.
    const progress: Record<number, AnyRecord | null> = {}
    if (appAccountStore.isAdmin) {
      await Promise.all(list.map(async (order) => {
        progress[order.id] = await operationsApi.factoryProgress(Number(order.id)).catch(() => null)
      }))
    }
    if (sequence !== loadSequence) {
      return
    }
    if (productList) {
      products.value = productList
    }
    lingxingStatus.value = status
    orders.value = list
    progressById.value = progress
  }
  catch (error) {
    if (sequence === loadSequence) {
      notifyError(error, '采购订单加载失败')
    }
  }
  finally {
    if (sequence === loadSequence) {
      loading.value = false
    }
  }
}

function orderFields(order: AnyRecord): AnyRecord {
  return order.fields && typeof order.fields === 'object' ? order.fields : {}
}

function productLabel(order: AnyRecord) {
  if (order.productModelName) {
    return `${order.productModelName}${order.productModelCode ? ` · ${order.productModelCode}` : ''}`
  }
  if (order.partName) {
    return `${order.partName}${order.partCode ? ` · ${order.partCode}` : ''}`
  }
  return orderFields(order).SKU || '-'
}

function supplierLabel(order: AnyRecord) {
  return orderFields(order)['供应商'] || order.supplierName || ''
}

function quantityLabel(order: AnyRecord) {
  const value = orderFields(order)['实际采购量']
  if (value !== null && value !== undefined && value !== '') {
    return String(value)
  }
  return order.quantity !== null && order.quantity !== undefined ? String(order.quantity) : '-'
}

function isPushed(order: AnyRecord) {
  return order.syncStatus === 'PUSHED'
}

async function pushPurchaseOrder(order: AnyRecord) {
  const id = Number(order.id)
  if (pushingOrders.value.has(id)) {
    return
  }
  pushingOrders.value.add(id)
  try {
    const result = await operationsApi.pushPurchaseOrder(id)
    await loadAll()
    notifySuccess('采购单已下单', result?.lingxingPoId ? `领星采购单号 ${result.lingxingPoId} 已转为待到货` : '领星同步完成')
  }
  catch (error) {
    notifyError(error, '采购订单推送失败')
    // The server records the failure (推送失败 + reason): show it right away.
    loadPurchaseOrders()
  }
  finally {
    pushingOrders.value.delete(id)
  }
}

async function deletePurchaseOrder(order: AnyRecord) {
  const confirmed = await confirmAction(`确定删除采购订单「${order.poNo}」吗？此操作不可恢复。`, {
    title: '删除采购订单',
    confirmText: '删除',
    danger: true,
  })
  if (!confirmed) {
    return
  }
  try {
    await operationsApi.deletePurchaseOrder(Number(order.id))
    if (editingId.value === Number(order.id)) {
      resetForm()
    }
    await loadAll()
    notifySuccess('采购订单已删除')
  }
  catch (error) {
    notifyError(error, '采购订单删除失败')
  }
}

// ---- create / edit / copy form ------------------------------------------------

type FormMode = 'create' | 'edit' | 'copy'

const formSection = ref<HTMLElement>()
const formRef = ref<FormInstance>()
const mode = ref<FormMode>('create')
/** The order being edited or copied (for the mode label). */
const modeOrder = ref<AnyRecord | null>(null)
const editingId = ref<number | null>(null)
const submitting = ref(false)
const form = reactive({
  productModelId: undefined as number | undefined,
})
const fieldValues = ref<Record<string, string>>(defaultFieldValues())
/** Active panels of the 更多字段 collapse (`['more']` = open). */
const moreOpen = ref<string[]>([])

const rules: FormRules = {
  productModelId: [{ required: true, message: '请选择关联产品', trigger: 'change' }],
}

const formTitle = computed(() => ({ create: '新建采购订单', edit: '编辑采购订单', copy: '复制新建采购订单' })[mode.value])
const modeLabel = computed(() => {
  if (mode.value === 'edit' && modeOrder.value) {
    return `正在编辑 ${modeOrder.value.poNo}`
  }
  if (mode.value === 'copy' && modeOrder.value) {
    return `按 ${modeOrder.value.poNo} 复制新建`
  }
  return ''
})
const submitLabel = computed(() => (mode.value === 'edit' ? '保存修改' : '创建采购订单'))
const lockedColumns = computed(() => (mode.value === 'edit' ? [PO_NO_COLUMN] : []))

const activeProducts = computed(() => products.value.filter(item => item.active !== false))
const productPlaceholder = computed(() => {
  if (activeProducts.value.length) {
    return '请选择关联产品'
  }
  return `请先在“${appAccountStore.isOperations ? '我的产品' : '产品管理'}”中添加产品`
})
const productOptions = computed(() => {
  const options = activeProducts.value.map(item => ({
    value: Number(item.id),
    label: `${item.name} · ${item.productCode}`,
  }))
  // An order being edited / copied may link a product that is no longer in
  // the active list: keep it selectable under its own name instead of
  // showing a bare id.
  const current = form.productModelId
  const order = modeOrder.value
  if (current !== undefined && !options.some(option => option.value === current)) {
    const linked = order && Number(order.productModelId) === current && order.productModelName
      ? `${order.productModelName}${order.productModelCode ? ` · ${order.productModelCode}` : ''}`
      : `产品 #${current}`
    options.push({ value: current, label: linked })
  }
  return options
})

function resetForm() {
  mode.value = 'create'
  modeOrder.value = null
  editingId.value = null
  form.productModelId = undefined
  // Rebuild the field values so they return to their editable defaults.
  fieldValues.value = defaultFieldValues()
  moreOpen.value = []
  nextTick(() => formRef.value?.clearValidate())
}

function fillPurchaseOrderForm(order: AnyRecord, editing: boolean) {
  const { values, hasMoreValue } = fieldValuesFromOrder(orderFields(order), { keepPoNo: editing })
  form.productModelId = order.productModelId ? Number(order.productModelId) : undefined
  fieldValues.value = values
  moreOpen.value = hasMoreValue ? ['more'] : []
  editingId.value = editing ? Number(order.id) : null
  mode.value = editing ? 'edit' : 'copy'
  modeOrder.value = order
  nextTick(() => {
    formRef.value?.clearValidate()
    formSection.value?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  })
}

function editPurchaseOrder(order: AnyRecord) {
  fillPurchaseOrderForm(order, true)
}

function copyPurchaseOrder(order: AnyRecord) {
  fillPurchaseOrderForm(order, false)
  notifySuccess('已复制采购单内容', '已按所选订单预填，可修改后创建新单')
}

function cancelPurchaseOrderEdit() {
  resetForm()
}

async function submitPurchaseOrder() {
  if (!formRef.value || submitting.value) {
    return
  }
  const valid = await formRef.value.validate().catch(() => false)
  if (!valid || form.productModelId === undefined) {
    return
  }
  const editing = editingId.value
  submitting.value = true
  try {
    const body = { productModelId: form.productModelId, fields: fieldsPayload(fieldValues.value) }
    if (editing !== null) {
      await operationsApi.updatePurchaseOrder(editing, body)
    }
    else {
      await operationsApi.createPurchaseOrder(body)
    }
    resetForm()
    await loadAll()
    notifySuccess(editing !== null ? '采购订单已更新' : '采购订单已创建')
  }
  catch (error) {
    notifyError(error, editing !== null ? '采购订单更新失败' : '采购订单创建失败')
  }
  finally {
    submitting.value = false
  }
}

// ---- 供应收货同步 (supplier receipts pushed to Lingxing) ----------------------

const inboundLoading = ref(false)
const inboundReceipts = ref<AnyRecord[]>([])
const inboundError = ref('')
const pushingReceipts = ref(new Set<number>())

async function loadInboundReceipts() {
  inboundLoading.value = true
  try {
    inboundReceipts.value = await operationsApi.inboundReceipts()
    inboundError.value = ''
  }
  catch (error) {
    inboundReceipts.value = []
    inboundError.value = errorMessage(error, '供应收货记录加载失败')
  }
  finally {
    inboundLoading.value = false
  }
}

async function pushInboundReceipt(receipt: AnyRecord) {
  const id = Number(receipt.id)
  if (pushingReceipts.value.has(id)) {
    return
  }
  pushingReceipts.value.add(id)
  try {
    const result = await operationsApi.pushInboundReceipt(id)
    await loadAll()
    notifySuccess('供应收货已推送', result?.lingxingInboundId || '领星入库同步完成')
  }
  catch (error) {
    notifyError(error, '供应收货推送失败')
    // A failed push is recorded as 推送失败 on the receipt.
    loadInboundReceipts()
  }
  finally {
    pushingReceipts.value.delete(id)
  }
}

function loadAll() {
  return Promise.all([loadPurchaseOrders(), loadInboundReceipts()])
}

onMounted(loadAll)
</script>

<template>
  <div class="pts-page">
    <FaPageHeader title="采购订单" description="创建本地采购单，查看领星同步状态与工厂进度">
      <ElButton id="po-export-all" tag="a" :href="exportAllUrl" target="_blank" rel="noopener">
        <template #icon>
          <FaIcon name="i-ri:file-excel-2-line" />
        </template>
        导出全部为 Excel
      </ElButton>
    </FaPageHeader>

    <div ref="formSection" class="scroll-mt-4">
      <FaPageMain :title="formTitle">
        <PtsListWindowNotice url="/api/purchase-orders" />
        <ElForm id="purchase-order-form" ref="formRef" :model="form" :rules="rules" label-position="top" @submit.prevent="submitPurchaseOrder">
          <div class="gap-x-5 grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-3">
            <ElFormItem label="关联产品" prop="productModelId">
              <ElSelect v-model="form.productModelId" name="productModelId" :placeholder="productPlaceholder" filterable class="w-full">
                <ElOption v-for="option in productOptions" :key="option.value" :label="option.label" :value="option.value" />
              </ElSelect>
            </ElFormItem>
          </div>
          <div class="mb-3 flex flex-wrap gap-x-3 gap-y-1 items-baseline">
            <strong class="text-sm font-semibold">采购单字段</strong>
            <span class="pts-muted text-xs">按采购单模板逐项填写；导出的 Excel 列与模板完全一致，留空的字段导出为空</span>
          </div>
          <div id="purchase-order-fields">
            <FieldGrid v-model="fieldValues" :fields="PRIMARY_FIELDS" :locked-columns="lockedColumns" />
            <ElCollapse v-model="moreOpen" class="pts-po-more">
              <ElCollapseItem name="more" :title="`更多字段（选填，共 ${MORE_FIELDS.length} 项）`">
                <FieldGrid v-model="fieldValues" :fields="MORE_FIELDS" />
              </ElCollapseItem>
            </ElCollapse>
          </div>
          <div class="mt-4 flex flex-wrap gap-2 items-center justify-end">
            <span id="po-form-mode" class="pts-muted text-sm mr-auto" aria-live="polite">{{ modeLabel }}</span>
            <ElButton v-if="mode !== 'create'" id="po-cancel-edit" @click="cancelPurchaseOrderEdit">
              取消编辑
            </ElButton>
            <ElButton id="po-submit" type="primary" native-type="submit" :loading="submitting">
              <template #icon>
                <FaIcon v-if="mode === 'edit'" name="i-ri:save-line" />
                <FaIcon v-else name="i-ri:add-line" />
              </template>
              {{ submitLabel }}
            </ElButton>
          </div>
        </ElForm>
      </FaPageMain>
    </div>

    <FaPageMain title="采购订单列表">
      <ElAlert
        v-if="lingxingStatus && !pushReady"
        type="warning"
        :closable="false"
        show-icon
        class="mb-3"
        title="领星采购订单接口未配置"
        description="请先在系统设置中配置领星采购订单接口，配置后即可推送领星（采购单下单）"
      />
      <ElTable id="purchase-order-table" v-loading="loading" :data="orders" row-key="id" empty-text="暂无采购订单" stripe>
        <ElTableColumn label="采购单号 / 采购人" min-width="200">
          <template #default="{ row }">
            <span class="pts-cell-main pts-code">{{ row.poNo }}</span>
            <span class="pts-cell-sub">采购人：{{ row.createdBy || '-' }}</span>
          </template>
        </ElTableColumn>
        <ElTableColumn label="产品 / 供应商" min-width="200">
          <template #default="{ row }">
            <span class="pts-cell-main">{{ productLabel(row) }}</span>
            <span class="pts-cell-sub">{{ supplierLabel(row) }}</span>
          </template>
        </ElTableColumn>
        <ElTableColumn label="采购量" width="100">
          <template #default="{ row }">
            {{ quantityLabel(row) }}
          </template>
        </ElTableColumn>
        <ElTableColumn label="同步状态" min-width="170">
          <template #default="{ row }">
            <ElTag :type="syncStatus(row.syncStatus).type" disable-transitions>
              {{ syncStatus(row.syncStatus).label }}
            </ElTag>
            <span v-if="row.pushError" class="pts-cell-sub pts-text-warning mt-1">{{ row.pushError }}</span>
          </template>
        </ElTableColumn>
        <ElTableColumn v-if="appAccountStore.isAdmin" label="工厂进度" min-width="140">
          <template #default="{ row }">
            <template v-if="progressById[row.id]">
              <span class="pts-cell-main">{{ progressById[row.id]?.productionOrderGenerated ? '已生成生产订单' : '未生成' }}</span>
              <span class="pts-cell-sub">累计入库 {{ progressById[row.id]?.latestQuantity ?? 0 }}</span>
            </template>
            <span v-else class="pts-cell-sub">—</span>
          </template>
        </ElTableColumn>
        <ElTableColumn label="创建时间" width="150">
          <template #default="{ row }">
            {{ formatDate(row.createdAt) }}
          </template>
        </ElTableColumn>
        <ElTableColumn label="操作" width="330" align="right" fixed="right">
          <template #default="{ row }">
            <template v-if="!isPushed(row)">
              <ElButton
                v-if="pushReady"
                size="small"
                :loading="pushingOrders.has(Number(row.id))"
                @click="pushPurchaseOrder(row)"
              >
                <template #icon>
                  <FaIcon name="i-ri:send-plane-line" />
                </template>
                推送领星
              </ElButton>
              <ElButton v-else size="small" disabled title="请先在系统设置中配置领星采购订单接口">
                <template #icon>
                  <FaIcon name="i-ri:send-plane-line" />
                </template>
                推送领星
              </ElButton>
              <ElButton link type="primary" :aria-label="`编辑采购订单 ${row.poNo}`" @click="editPurchaseOrder(row)">
                编辑
              </ElButton>
            </template>
            <ElButton link type="primary" :aria-label="`复制采购订单 ${row.poNo}`" @click="copyPurchaseOrder(row)">
              复制
            </ElButton>
            <ElButton link type="primary" tag="a" :href="operationsApi.purchaseOrderExportUrl(Number(row.id))" target="_blank" rel="noopener" :aria-label="`导出采购订单 ${row.poNo}`">
              导出
            </ElButton>
            <ElButton link type="danger" :aria-label="`删除采购订单 ${row.poNo}`" @click="deletePurchaseOrder(row)">
              删除
            </ElButton>
          </template>
        </ElTableColumn>
      </ElTable>
    </FaPageMain>

    <FaPageMain title="供应收货同步">
      <p class="pts-muted text-sm mt-0 mb-3">
        仓管登记收货后，由运营推送领星入库
      </p>
      <ElTable id="operations-inbound-table" v-loading="inboundLoading" :data="inboundReceipts" row-key="id" :empty-text="inboundError || '暂无供应收货记录'" stripe>
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
        <ElTableColumn label="商品" min-width="160">
          <template #default="{ row }">
            {{ row.partName || '-' }}
          </template>
        </ElTableColumn>
        <ElTableColumn label="数量" width="90">
          <template #default="{ row }">
            {{ formatNumber(row.quantity) }}
          </template>
        </ElTableColumn>
        <ElTableColumn label="同步状态" min-width="160">
          <template #default="{ row }">
            <ElTag :type="syncStatus(row.syncStatus).type" disable-transitions>
              {{ syncStatus(row.syncStatus).label }}
            </ElTag>
            <span v-if="row.pushError" class="pts-cell-sub pts-text-warning mt-1">{{ row.pushError }}</span>
          </template>
        </ElTableColumn>
        <ElTableColumn label="收货时间" width="150">
          <template #default="{ row }">
            {{ formatDate(row.receivedAt) }}
          </template>
        </ElTableColumn>
        <ElTableColumn label="操作" width="130" align="right" fixed="right">
          <template #default="{ row }">
            <ElButton
              v-if="row.syncStatus !== 'PUSHED'"
              size="small"
              :loading="pushingReceipts.has(Number(row.id))"
              :aria-label="`推送入库 收货记录 #${row.id}`"
              @click="pushInboundReceipt(row)"
            >
              <template #icon>
                <FaIcon name="i-ri:upload-cloud-2-line" />
              </template>
              推送入库
            </ElButton>
          </template>
        </ElTableColumn>
      </ElTable>
    </FaPageMain>
  </div>
</template>
