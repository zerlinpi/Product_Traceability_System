<script setup lang="ts">
/**
 * 扫码查询（全部角色）— read-only lookup of a production batch by its QR code:
 * product, planned / registered quantity, quality state and the supplier
 * batches it consumed. Never writes anything.
 */
import type { AnyRecord } from '@/api/types'
import batchesApi from '@/api/modules/batches'
import { focusWithRetry, useScanCapture } from '@/composables/scanner'
import { formatDate, qualityStatus } from '@/utils/format'
import { notifyError } from '@/utils/feedback'

defineOptions({
  name: 'BatchTracePage',
})

const codeInput = ref<{ input?: HTMLInputElement, select: () => void }>()
const code = ref('')
const loading = ref(false)
const data = ref<AnyRecord | null>(null)

const summary = computed(() => {
  const value = data.value
  if (!value) {
    return []
  }
  return [
    { label: '批次码', value: value.batchCode || '-', hint: '整批唯一二维码码值', mono: true },
    { label: '产品型号', value: value.productName || '-', hint: '本批走步机型号' },
    { label: '计划台数', value: value.plannedQuantity ?? '-', hint: '计划生产数量' },
    { label: '登记台数', value: value.registered ? (value.registeredQuantity ?? '-') : '未登记', hint: '已扫码登记数量' },
    { label: '生成时间', value: formatDate(value.generatedAt), hint: '批次生成时间' },
    { label: '质量状态', value: value.registered ? qualityStatus(value.qualityStatus).label : '未登记', hint: '批次级质量状态' },
  ]
})
const consumption = computed<AnyRecord[]>(() => data.value?.reverseTrace ?? [])

function scanElement() {
  return codeInput.value?.input
}

function focusTrace() {
  focusWithRetry(scanElement, () => !loading.value)
}

async function submitBatchTrace() {
  const value = code.value.trim()
  if (!value || loading.value) {
    focusTrace()
    return
  }
  loading.value = true
  try {
    data.value = await batchesApi.batchTraceQuery(value)
  }
  catch (error) {
    data.value = null
    notifyError(error, '查询失败')
  }
  finally {
    loading.value = false
    await nextTick()
    codeInput.value?.select()
    focusTrace()
  }
}

useScanCapture({
  input: scanElement,
  busy: loading,
  onSubmit: submitBatchTrace,
  refocus: focusTrace,
})

onMounted(focusTrace)
onActivated(focusTrace)
</script>

<template>
  <div class="pts-page">
    <FaPageHeader title="扫码查询" description="扫描批次二维码查看只读产品信息，不产生任何登记或数据变更" />
    <FaPageMain>
      <form id="batch-trace-form" class="flex flex-wrap gap-3 items-center" autocomplete="off" @submit.prevent="submitBatchTrace">
        <ElInput
          ref="codeInput"
          v-model="code"
          name="code"
          class="pts-scan-box max-w-xl flex-1"
          placeholder="扫描或输入批次二维码"
          maxlength="120"
          aria-label="批次二维码"
        />
        <ElButton type="primary" size="large" native-type="submit" class="h-[52px]!" :loading="loading">
          查询
        </ElButton>
      </form>
    </FaPageMain>
    <template v-if="data">
      <FaPageMain>
        <div class="pts-metrics">
          <div v-for="item in summary" :key="item.label" class="pts-metric">
            <span class="pts-metric-label">{{ item.label }}</span>
            <span class="pts-metric-value" :class="{ 'pts-code text-base!': item.mono }">{{ item.value }}</span>
            <span class="pts-metric-hint">{{ item.hint }}</span>
          </div>
        </div>
      </FaPageMain>
      <FaPageMain title="供应来源 · 供应批次消耗">
        <p class="pts-muted text-sm mt-0 mb-3">
          该生产批次消耗的供应批次清单
        </p>
        <ElTable :data="consumption" empty-text="暂无供应批次消耗记录" stripe>
          <ElTableColumn label="供应批次" min-width="180">
            <template #default="{ row }">
              <span class="pts-code">{{ row.supplierBatchNo || row.supplierInventoryBatchId || '-' }}</span>
            </template>
          </ElTableColumn>
          <ElTableColumn label="供应部件" min-width="160">
            <template #default="{ row }">
              <span class="pts-cell-main">{{ row.partName || '-' }}</span>
              <span class="pts-cell-sub pts-code">{{ row.partCode || '' }}</span>
            </template>
          </ElTableColumn>
          <ElTableColumn label="供应商" min-width="140">
            <template #default="{ row }">
              {{ row.supplierName || '-' }}
            </template>
          </ElTableColumn>
          <ElTableColumn label="消耗数量" prop="quantityConsumed" width="120" align="right" />
        </ElTable>
      </FaPageMain>
    </template>
  </div>
</template>
