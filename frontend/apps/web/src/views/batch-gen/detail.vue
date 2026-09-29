<script setup lang="ts">
/**
 * 生产批次详情 — the batch QR code, its registration / quality state and the
 * reverse trace to the supplier batches it consumed.
 */
import type { AnyRecord } from '@/api/types'
import batchesApi from '@/api/modules/batches'
import { formatDate, qualityStatus } from '@/utils/format'
import { notifyError } from '@/utils/feedback'

defineOptions({
  name: 'BatchGenDetailPage',
})

const route = useRoute()
const router = useRouter()

const loading = ref(false)
const batch = ref<AnyRecord | null>(null)
const batchId = computed(() => Number(route.params.id))

const registration = computed(() => batch.value?.registration ?? {})
const registered = computed(() => Boolean(registration.value.registered))
const consumption = computed<AnyRecord[]>(() => batch.value?.reverseTrace ?? [])
const qrUrl = computed(() => batch.value?.downloadUrl || batchesApi.productionBatchQrUrl(batchId.value))

const summary = computed(() => {
  const data = batch.value ?? {}
  return [
    { label: '批次码', value: data.batchCode || '-', hint: '整批唯一二维码码值' },
    { label: '计划台数', value: data.plannedQuantity ?? '-', hint: '本批走步机计划数量' },
    { label: '登记台数', value: registered.value ? (registration.value.registeredQuantity ?? '-') : '未登记', hint: '扫码登记的实际台数' },
    { label: '质量状态', value: registered.value ? qualityStatus(registration.value.qualityStatus).label : '未登记', hint: '批次级质量状态' },
  ]
})

async function load() {
  if (!Number.isInteger(batchId.value) || batchId.value <= 0) {
    return
  }
  loading.value = true
  try {
    batch.value = await batchesApi.productionBatch(batchId.value)
  }
  catch (error) {
    notifyError(error, '批次详情加载失败')
  }
  finally {
    loading.value = false
  }
}

function back() {
  router.push({ name: 'batch-gen' })
}

watch(batchId, load)
onMounted(load)
</script>

<template>
  <div class="pts-page">
    <FaPageHeader :description="batch ? `${batch.productName || '-'} · 计划 ${batch.plannedQuantity ?? '-'} 台 · ${formatDate(batch.generatedAt)}` : '生产批次详情'">
      <template #title>
        <span class="pts-code text-2xl">{{ batch?.batchCode || '生产批次详情' }}</span>
      </template>
      <div class="flex gap-2">
        <ElButton @click="back">
          <template #icon>
            <FaIcon name="i-ri:arrow-left-line" />
          </template>
          返回批次列表
        </ElButton>
        <ElButton type="primary" tag="a" :href="qrUrl" target="_blank" rel="noopener">
          <template #icon>
            <FaIcon name="i-ri:download-2-line" />
          </template>
          下载批次二维码
        </ElButton>
      </div>
    </FaPageHeader>
    <div v-loading="loading">
      <FaPageMain>
        <div class="pts-metrics">
          <div v-for="item in summary" :key="item.label" class="pts-metric">
            <span class="pts-metric-label">{{ item.label }}</span>
            <span class="pts-metric-value" :class="{ 'pts-code text-base!': item.label === '批次码' }">{{ item.value }}</span>
            <span class="pts-metric-hint">{{ item.hint }}</span>
          </div>
        </div>
        <div v-if="batch" class="mt-5 flex flex-wrap gap-6 items-start">
          <img :src="qrUrl" alt="批次二维码" class="pts-qr">
          <dl class="pts-kv">
            <dt>产品型号</dt>
            <dd>{{ batch.productName || '-' }}</dd>
            <dt>前缀</dt>
            <dd>{{ batch.prefix || '-' }}</dd>
            <dt>生成人</dt>
            <dd>{{ batch.generatedBy || '-' }}</dd>
            <dt>生成时间</dt>
            <dd>{{ formatDate(batch.generatedAt) }}</dd>
            <template v-if="registered">
              <dt>登记时间</dt>
              <dd>{{ formatDate(registration.registeredAt) }}</dd>
              <dt>登记人</dt>
              <dd>{{ registration.operatorName || '-' }}</dd>
            </template>
          </dl>
        </div>
      </FaPageMain>
      <FaPageMain title="供应来源 · 供应批次消耗">
        <p class="pts-muted text-sm mb-3">
          本批次消耗的供应批次、供应部件与消耗数量
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
    </div>
  </div>
</template>
