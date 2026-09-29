<script setup lang="ts">
/**
 * 现场批次登记（管理员、仓管）— scan the production batch QR code once to
 * register the whole batch of treadmills. No per-unit or per-part scanning.
 *
 * Scan-gun behaviour (see composables/scanner.ts):
 * - the scan box is focused on entry and after every attempt, success or not;
 * - the gun's Enter (main or numpad) submits; keystrokes arriving while focus
 *   is on a neutral element are routed into the scan box;
 * - the write is idempotent (scope `batch-entry.scan`): a double-fired scan or
 *   a retry after a network drop replays instead of registering twice.
 */
import type { AnyRecord } from '@/api/types'
import batchesApi from '@/api/modules/batches'
import { focusWithRetry, getStationId, useScanCapture } from '@/composables/scanner'
import { formatDate, qualityStatus } from '@/utils/format'

defineOptions({
  name: 'BatchEntryPage',
})

type Tone = 'neutral' | 'success' | 'error'

const appAccountStore = useAppAccountStore()

const codeInput = ref<{ input?: HTMLInputElement }>()
const code = ref('')
const quantity = ref<number | undefined>()
const busy = ref(false)
const feedback = reactive<{ tone: Tone, title: string, message: string }>({
  tone: 'neutral',
  title: '尚未开始',
  message: '请扫描批次二维码',
})
const result = ref<AnyRecord | null>(null)

const resultRows = computed(() => {
  const data = result.value
  if (!data) {
    return []
  }
  return [
    ['批次码', data.batchCode || '-'],
    ['产品型号', data.productModelName || data.productName || '-'],
    ['计划台数', data.plannedQuantity ?? '-'],
    ['登记台数', data.registeredQuantity ?? '-'],
    ['质量状态', qualityStatus(data.qualityStatus || 'ASSEMBLED').label],
    ['登记时间', formatDate(data.registeredAt)],
    ['操作人', data.operatorName || '-'],
  ] as [string, string | number][]
})

function scanElement() {
  return codeInput.value?.input
}

function focusBatchEntry() {
  focusWithRetry(scanElement, () => !busy.value)
}

function setFeedback(tone: Tone, title: string, message: string) {
  feedback.tone = tone
  feedback.title = title
  feedback.message = message
}

async function submitBatchEntry() {
  if (busy.value) {
    return
  }
  const value = code.value.trim()
  if (!value) {
    focusBatchEntry()
    return
  }
  const body: AnyRecord = {
    stationId: getStationId(),
    stationName: '自动录入终端',
    operatorName: appAccountStore.account,
    code: value,
  }
  if (quantity.value !== undefined && quantity.value !== null) {
    body.quantity = Number(quantity.value)
  }
  busy.value = true
  try {
    const data = await batchesApi.batchEntryScan(body)
    code.value = ''
    quantity.value = undefined
    result.value = data
    setFeedback('success', '整批登记成功', `${data.batchCode || ''} 已登记 ${data.registeredQuantity ?? ''} 台`)
  }
  catch (error) {
    setFeedback('error', '登记未通过', error instanceof Error ? error.message : String(error))
    if (navigator.vibrate) {
      navigator.vibrate([120, 60, 120])
    }
  }
  finally {
    busy.value = false
    focusBatchEntry()
  }
}

function resetBatchEntry() {
  code.value = ''
  quantity.value = undefined
  result.value = null
  setFeedback('neutral', '尚未开始', '请扫描批次二维码')
  focusBatchEntry()
}

// Scanner keystrokes that land outside the scan box (focus on a button or the
// page body) are routed into it; Enter submits.
const { handleKeydown: handleBatchEntryKeydown } = useScanCapture({
  input: scanElement,
  busy,
  onSubmit: submitBatchEntry,
  refocus: focusBatchEntry,
})
defineExpose({ handleBatchEntryKeydown })

onMounted(resetBatchEntry)
onActivated(focusBatchEntry)
</script>

<template>
  <div class="pts-page">
    <FaPageHeader title="现场批次登记" description="扫描批次二维码一次性登记整批走步机" />
    <div class="px-4 gap-4 grid lg:grid-cols-[minmax(0,1.5fr)_minmax(0,1fr)]">
      <section class="p-5 border rounded-lg bg-card">
        <div class="flex gap-4 items-start justify-between">
          <div>
            <span class="pts-step-label">批次扫码登记 · 流程第 2 步</span>
            <h2 class="text-lg font-semibold m-0">
              扫描批次二维码
            </h2>
            <p class="pts-muted text-sm mt-2 mb-0">
              扫描该生产批次的二维码即可一次性登记整批走步机，无需逐台、逐部件扫码。登记后由管理员在「批次质量处理」放行，再到「扫码枪入库」入库。
            </p>
          </div>
          <ElButton id="batch-entry-reset" text type="danger" @click="resetBatchEntry">
            清空
          </ElButton>
        </div>
        <form id="batch-entry-form" class="mt-4 flex gap-3" autocomplete="off" @submit.prevent="submitBatchEntry">
          <ElInput
            id="batch-entry-code"
            ref="codeInput"
            v-model="code"
            name="code"
            class="pts-scan-box flex-1"
            placeholder="扫描或输入批次二维码"
            maxlength="120"
            aria-label="批次二维码输入"
          />
          <ElButton type="primary" size="large" native-type="submit" class="h-[52px]!" :loading="busy">
            登记整批
          </ElButton>
        </form>
        <div class="mt-4 max-w-sm">
          <label class="text-sm mb-1 block" for="batch-entry-quantity">登记台数（可选修正）</label>
          <ElInputNumber id="batch-entry-quantity" v-model="quantity" :min="1" :step="1" step-strictly controls-position="right" placeholder="留空则按计划台数登记" class="w-full!" />
          <p class="pts-muted text-xs mt-1 mb-0">
            修正值须为 1 至该批次计划台数之间的整数
          </p>
        </div>
        <div
          id="batch-entry-feedback"
          class="pts-scan-feedback"
          :data-tone="feedback.tone"
          role="status"
          aria-live="polite"
          aria-atomic="true"
          tabindex="-1"
        >
          <strong>{{ feedback.title }}</strong>
          <span>{{ feedback.message }}</span>
        </div>
      </section>
      <aside class="p-5 border rounded-lg bg-card">
        <span class="pts-step-label">最近登记</span>
        <h2 class="text-lg font-semibold m-0 mb-3">
          登记结果
        </h2>
        <div id="batch-entry-result">
          <dl v-if="result" class="pts-kv">
            <template v-for="[label, value] in resultRows" :key="label">
              <dt>{{ label }}</dt>
              <dd :class="{ 'pts-code': label === '批次码' }">
                {{ value }}
              </dd>
            </template>
          </dl>
          <div v-else class="pts-empty">
            扫码登记成功后在此显示批次信息
          </div>
        </div>
      </aside>
    </div>
  </div>
</template>
