<script setup lang="ts">
/**
 * 批次质量处理（管理员）— flow step 3: release (合格) or hold (暂扣) a whole
 * registered batch. Only 待检 registrations can be handled; a held batch can
 * never be stocked in.
 */
import type { AnyRecord } from '@/api/types'
import { useDebounceFn } from '@vueuse/core'
import batchesApi from '@/api/modules/batches'
import { formatDate, qualityStatus } from '@/utils/format'
import { notifyError } from '@/utils/feedback'

defineOptions({
  name: 'BatchQualityPage',
})

const loading = ref(false)
const records = ref<AnyRecord[]>([])
const filters = reactive({
  batchCode: '',
  from: '',
  to: '',
})
const appliedSummary = ref('显示全部批次登记记录')

const dialogVisible = ref(false)
const selected = ref<AnyRecord | null>(null)

function isActionable(record: AnyRecord) {
  return (record.qualityStatus || 'ASSEMBLED') === 'ASSEMBLED'
}

async function load() {
  const { batchCode, from, to } = filters
  if (from && to && from > to) {
    notifyError('开始时间不能晚于结束时间', '筛选时间无效')
    return
  }
  loading.value = true
  try {
    records.value = await batchesApi.batchTraceRecords({
      batchCode: batchCode.trim(),
      from,
      // Stored timestamps carry a time of day; make the end date inclusive.
      to: to ? `${to}T23:59:59.999999` : '',
    })
    const summary = [`${records.value.length} 条记录`]
    if (batchCode.trim()) {
      summary.push(`批次码 ${batchCode.trim()}`)
    }
    if (from || to) {
      summary.push(`${from || '最早'} 至 ${to || '今天'}`)
    }
    appliedSummary.value = summary.join(' · ')
  }
  catch (error) {
    notifyError(error, '批次登记记录加载失败')
  }
  finally {
    loading.value = false
  }
}

const loadDebounced = useDebounceFn(load, 300)
watch(() => filters.batchCode, () => loadDebounced())
watch(() => [filters.from, filters.to], () => load())

function resetFilters() {
  filters.batchCode = ''
  filters.from = ''
  filters.to = ''
}

function openBatchQuality(record: AnyRecord) {
  selected.value = record
  dialogVisible.value = true
}

onMounted(load)
</script>

<template>
  <div class="pts-page">
    <FaPageHeader title="批次质量处理" description="流程第 3 步：对整批走步机执行合格放行或暂扣；仅待检记录可处理。暂扣批次不可入库" />
    <FaPageMain>
      <div class="pts-toolbar">
        <div class="pts-toolbar-filters">
          <ElInput id="batch-quality-code-filter" v-model="filters.batchCode" placeholder="按批次码筛选（完整码值）" clearable class="w-64" aria-label="批次码" />
          <ElDatePicker id="batch-quality-from" v-model="filters.from" type="date" value-format="YYYY-MM-DD" placeholder="开始时间" class="w-40!" aria-label="开始时间" />
          <ElDatePicker id="batch-quality-to" v-model="filters.to" type="date" value-format="YYYY-MM-DD" placeholder="结束时间" class="w-40!" aria-label="结束时间" />
          <ElButton id="batch-quality-reset" @click="resetFilters">
            重置筛选
          </ElButton>
        </div>
        <span id="batch-quality-summary" class="pts-muted text-sm">{{ appliedSummary }}</span>
      </div>
      <ElTable id="batch-quality-table" v-loading="loading" :data="records" row-key="id" empty-text="当前筛选条件下没有批次登记记录" stripe>
        <ElTableColumn label="批次码" min-width="220">
          <template #default="{ row }">
            <span class="pts-code pts-cell-main">{{ row.batchCode || '-' }}</span>
          </template>
        </ElTableColumn>
        <ElTableColumn label="产品型号" prop="productName" min-width="160" show-overflow-tooltip />
        <ElTableColumn label="登记台数" prop="registeredQuantity" width="100" />
        <ElTableColumn label="质量状态" min-width="160">
          <template #default="{ row }">
            <ElTag :type="qualityStatus(row.qualityStatus || 'ASSEMBLED').type" disable-transitions>
              {{ qualityStatus(row.qualityStatus || 'ASSEMBLED').label }}
            </ElTag>
            <span v-if="row.statusReason" class="pts-cell-sub mt-1">{{ row.statusReason }}</span>
          </template>
        </ElTableColumn>
        <ElTableColumn label="操作人" prop="operatorName" width="110" />
        <ElTableColumn label="生成时间" width="150">
          <template #default="{ row }">
            {{ formatDate(row.generatedAt) }}
          </template>
        </ElTableColumn>
        <ElTableColumn label="操作" width="120" align="right" fixed="right">
          <template #default="{ row }">
            <ElButton v-if="isActionable(row)" size="small" type="primary" plain @click="openBatchQuality(row)">
              质量处理
            </ElButton>
            <span v-else class="pts-cell-sub">已处理</span>
          </template>
        </ElTableColumn>
      </ElTable>
    </FaPageMain>
    <PtsBatchQualityDialog v-model="dialogVisible" :record="selected" @done="load" />
  </div>
</template>
