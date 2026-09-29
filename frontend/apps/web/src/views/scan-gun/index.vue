<script setup lang="ts">
/**
 * 扫码枪入库（管理员、仓管）— scan a production QR code, confirm the quantity,
 * and the finished goods enter stock (flow step 4).
 *
 * Focus contract (README「扫码枪现场验收」):
 * - on entry the production-QR box is focused;
 * - a successful lookup moves focus to the quantity box (pre-filled with the
 *   outstanding quantity) so confirming is one keystroke;
 * - after the stock-in is confirmed the page resets and focus returns to the
 *   QR box; a failed lookup selects the QR text so the next scan replaces it;
 * - a batch blocked by the quality gate (暂扣 / not released) never shows the
 *   confirm form at all.
 * The stock-in write is idempotent (scope `scan-gun.inbound`).
 */
import type { AnyRecord } from '@/api/types'
import operationsApi from '@/api/modules/operations'
import { focusWithRetry, useScanCapture } from '@/composables/scanner'
import { formatDate, formatNumber, qualityStatus } from '@/utils/format'
import { notifyError, notifySuccess } from '@/utils/feedback'

defineOptions({
  name: 'ScanGunPage',
})

type Tone = 'neutral' | 'success' | 'error'

const codeInput = ref<{ input?: HTMLInputElement, select: () => void }>()
const quantityInput = ref<{ focus: () => void, $el?: HTMLElement }>()
const code = ref('')
const looking = ref(false)
const submitting = ref(false)
const order = ref<AnyRecord | null>(null)
const inboundQuantity = ref<number | undefined>()
const feedback = reactive<{ tone: Tone, title: string, message: string }>({
  tone: 'neutral',
  title: '等待扫码',
  message: '请扫描生产订单二维码',
})
const records = ref<AnyRecord[]>([])
const recordsLoading = ref(false)
const recordsError = ref('')

const progress = computed<AnyRecord>(() => order.value?.progress ?? {})
const blocked = computed(() => progress.value.canStockIn === false)
const showInboundForm = computed(() => order.value !== null && !blocked.value)
const registrationLabel = computed(() => progress.value.registered
  ? `已登记 ${progress.value.registeredQuantity ?? '-'} 台 · ${qualityStatus(progress.value.qualityStatus || 'ASSEMBLED').label}`
  : '尚未批次登记')
const receivedLabel = computed(() => `已入库 ${progress.value.receivedQuantity ?? 0} / 计划 ${order.value?.quantity ?? '-'}`)

function scanElement() {
  return codeInput.value?.input
}

function quantityElement() {
  return quantityInput.value?.$el?.querySelector('input') ?? null
}

function focusScanGun() {
  focusWithRetry(scanElement, () => !looking.value && !submitting.value)
}

function focusQuantity() {
  focusWithRetry(() => quantityElement(), () => showInboundForm.value)
  nextTick(() => quantityElement()?.select())
}

function refocus() {
  if (showInboundForm.value) {
    focusQuantity()
  }
  else {
    focusScanGun()
  }
}

function setFeedback(tone: Tone, title: string, message: string) {
  feedback.tone = tone
  feedback.title = title
  feedback.message = message
}

// Recent 成品扫码入库 history, so the operator sees the record right after
// confirming instead of only a toast.
async function loadScanGunRecords() {
  recordsLoading.value = true
  recordsError.value = ''
  try {
    records.value = await operationsApi.inboundScanRecords(50)
  }
  catch (error) {
    recordsError.value = error instanceof Error ? error.message : String(error)
  }
  finally {
    recordsLoading.value = false
  }
}

function loadScanGun() {
  order.value = null
  code.value = ''
  inboundQuantity.value = undefined
  loadScanGunRecords()
  focusScanGun()
}

async function submitScanGunLookup() {
  const value = code.value.trim()
  if (!value || looking.value) {
    focusScanGun()
    return
  }
  looking.value = true
  try {
    const data = await operationsApi.scanGunLookup(value)
    order.value = data
    const state = data.progress ?? {}
    if (state.canStockIn === false) {
      setFeedback('error', '该批次暂不可入库', state.stockInBlockedReason || '请先完成批次登记与质量放行')
      await nextTick()
      codeInput.value?.select()
      return
    }
    setFeedback('success', '已找到生产订单', `${data.poNo ?? ''} · ${data.productName ?? ''}`)
    // Default to what is still outstanding so the common case is one keystroke.
    inboundQuantity.value = state.remainingQuantity || undefined
    await nextTick()
    focusQuantity()
  }
  catch (error) {
    order.value = null
    setFeedback('error', '二维码无效', error instanceof Error ? error.message : String(error))
    await nextTick()
    codeInput.value?.select()
  }
  finally {
    looking.value = false
  }
}

async function submitScanGunInbound() {
  if (!order.value || submitting.value) {
    return
  }
  const quantity = Number(inboundQuantity.value)
  if (!Number.isInteger(quantity) || quantity < 1) {
    notifyError('入库数量须为 1 至 999999 之间的整数', '无法入库')
    focusQuantity()
    return
  }
  submitting.value = true
  try {
    const result = await operationsApi.scanGunInbound({ productionOrderId: Number(order.value.id), quantity })
    notifySuccess('成品入库成功', `最新库存 ${formatNumber(result.onHand)}`)
    setFeedback('success', '入库完成', `${order.value.productName ?? ''} 入库 ${quantity} 件，最新库存 ${formatNumber(result.onHand)}`)
    submitting.value = false
    loadScanGun()
  }
  catch (error) {
    notifyError(error, '成品入库失败')
  }
  finally {
    submitting.value = false
  }
}

useScanCapture({
  input: scanElement,
  busy: computed(() => looking.value || submitting.value),
  onSubmit: submitScanGunLookup,
  refocus,
})

onMounted(() => {
  setFeedback('neutral', '等待扫码', '请扫描生产订单二维码')
  loadScanGun()
})
onActivated(focusScanGun)
</script>

<template>
  <div class="pts-page">
    <FaPageHeader title="扫码枪入库" description="扫描生产二维码并登记成品库存" />
    <div class="px-4 gap-4 grid lg:grid-cols-[minmax(0,1.5fr)_minmax(0,1fr)]">
      <section class="p-5 border rounded-lg bg-card">
        <span class="pts-step-label">成品入库 · 流程第 4 步</span>
        <h2 class="text-lg font-semibold m-0">
          扫描生产二维码
        </h2>
        <p class="pts-muted text-sm mt-2 mb-0">
          扫码枪回车后自动查询生产订单，焦点会保持在扫码框。扫码后会显示该批次的登记与质量状态、已入库数量；暂扣批次不可入库。
        </p>
        <form id="scan-gun-lookup-form" class="mt-4 flex gap-3" autocomplete="off" @submit.prevent="submitScanGunLookup">
          <ElInput
            id="scan-gun-code"
            ref="codeInput"
            v-model="code"
            name="code"
            class="pts-scan-box flex-1"
            placeholder="扫描或输入生产二维码"
            maxlength="120"
            aria-label="生产二维码输入"
          />
          <ElButton type="primary" size="large" native-type="submit" class="h-[52px]!" :loading="looking">
            查询
          </ElButton>
        </form>
        <div id="scan-gun-feedback" class="pts-scan-feedback" :data-tone="feedback.tone" role="status" aria-live="polite" aria-atomic="true">
          <strong>{{ feedback.title }}</strong>
          <span>{{ feedback.message }}</span>
        </div>
      </section>
      <aside class="p-5 border rounded-lg bg-card">
        <span class="pts-step-label">入库确认</span>
        <h2 class="text-lg font-semibold m-0 mb-3">
          生产订单
        </h2>
        <div id="scan-gun-order">
          <div v-if="order" class="space-y-1">
            <div class="text-base font-semibold">
              {{ order.productName }}
            </div>
            <div class="pts-muted text-sm">
              {{ order.productModelCode }} · 计划 {{ order.quantity }} 件<template v-if="order.isExternal">
                · 外采
              </template>
            </div>
            <div class="pts-code text-sm">
              {{ order.productionQrCode }}
            </div>
            <div class="pts-cell-sub">
              {{ registrationLabel }}
            </div>
            <div class="pts-cell-sub">
              {{ receivedLabel }}
            </div>
            <div v-if="progress.fullyReceived" class="pts-text-warning text-sm">
              该生产订单已按计划入库完毕，请确认是否重复扫码
            </div>
            <div v-if="blocked" class="pts-text-danger text-sm">
              {{ progress.stockInBlockedReason }}
            </div>
          </div>
          <div v-else class="pts-empty">
            扫码后显示订单与产品
          </div>
        </div>
        <form v-if="showInboundForm" id="scan-gun-inbound-form" class="mt-4" @submit.prevent="submitScanGunInbound">
          <label class="text-sm mb-1 block" for="scan-gun-quantity">入库数量</label>
          <ElInputNumber
            id="scan-gun-quantity"
            ref="quantityInput"
            v-model="inboundQuantity"
            name="quantity"
            :min="1"
            :max="999999"
            :step="1"
            step-strictly
            controls-position="right"
            class="w-full!"
          />
          <ElButton type="primary" native-type="submit" class="mt-3 w-full" :loading="submitting">
            确认入库
          </ElButton>
        </form>
      </aside>
    </div>
    <FaPageMain title="成品入库记录">
      <p class="pts-muted text-sm mt-0 mb-3">
        最近 50 条扫码入库明细，含入库后的成品库存
      </p>
      <ElTable id="scan-gun-record-table" v-loading="recordsLoading" :data="records" :empty-text="recordsError || '暂无成品入库记录，扫描生产二维码后在此显示'" stripe>
        <ElTableColumn label="入库记录" width="100">
          <template #default="{ row }">
            #{{ row.id }}
          </template>
        </ElTableColumn>
        <ElTableColumn label="产品" min-width="180">
          <template #default="{ row }">
            <span class="pts-cell-main">{{ row.productName || '-' }}</span>
            <span class="pts-cell-sub">{{ row.productModelCode || '' }}</span>
          </template>
        </ElTableColumn>
        <ElTableColumn label="生产二维码 / 采购单" min-width="220">
          <template #default="{ row }">
            <span class="pts-cell-main pts-code">{{ row.productionQrCode || '-' }}</span>
            <span class="pts-cell-sub">{{ row.poNo || '-' }}</span>
          </template>
        </ElTableColumn>
        <ElTableColumn label="入库数量" width="100">
          <template #default="{ row }">
            {{ formatNumber(row.quantity) }}
          </template>
        </ElTableColumn>
        <ElTableColumn label="入库后库存" width="110">
          <template #default="{ row }">
            {{ row.onHand == null ? '-' : formatNumber(row.onHand) }}
          </template>
        </ElTableColumn>
        <ElTableColumn label="操作人" prop="operatorName" width="110" />
        <ElTableColumn label="入库时间" width="150">
          <template #default="{ row }">
            {{ formatDate(row.receivedAt) }}
          </template>
        </ElTableColumn>
      </ElTable>
    </FaPageMain>
  </div>
</template>
