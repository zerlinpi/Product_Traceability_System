<script setup lang="ts">
/**
 * 逐台录入记录（历史）— the retired per-unit trace records of one product,
 * still served by GET /api/records for historical and non-treadmill data.
 *
 * - filters: 生成批次 / 完成日期范围 / 质量状态 / 关键词, applied by the server;
 * - rows grouped by completion day, newest first;
 * - quality handling for one record or a checked selection (bulk ≤ 200);
 * - 修改 (扫码修改部件码与备注) and 删除 (原因必填) per record;
 * - 族谱 asks the trace page to open the unit genealogy of the main code.
 */
import type { AnyRecord } from '@/api/types'
import { useDebounceFn } from '@vueuse/core'
import { errorMessage } from '@/api'
import catalogApi from '@/api/modules/catalog'
import recordsApi, { RECORD_STATUS_BULK_LIMIT } from '@/api/modules/records'
import { notifyError } from '@/utils/feedback'
import { formatDate, qualityStatus } from '@/utils/format'
import { dateGroupRowClass, dateGroupSpan, generationBatchLabel, groupByDay, groupedRowKey, isGroupRow } from '../helpers'
import RecordDeleteDialog from './RecordDeleteDialog.vue'
import RecordEditDialog from './RecordEditDialog.vue'
import RecordQualityDialog from './RecordQualityDialog.vue'

defineOptions({
  name: 'TraceUnitRecords',
})

const props = defineProps<{
  productId: number
  /** Code whose genealogy is loading, to show a spinner on that row. */
  genealogyBusy?: string
}>()

const emit = defineEmits<{
  genealogy: [code: string]
  loaded: [count: number]
}>()

const STATUS_OPTIONS = [
  { value: '', label: '全部状态' },
  { value: 'ASSEMBLED', label: '待检' },
  { value: 'PASSED', label: '合格' },
  { value: 'HOLD', label: '暂扣' },
]
/** GET /api/records returns at most this many rows. */
const SERVER_LIMIT = 500
/** Leaf columns of the table, for the date header rows. */
const COLUMN_COUNT = 9

const loading = ref(false)
const loadError = ref('')
const records = ref<AnyRecord[]>([])
const generationBatches = ref<AnyRecord[]>([])
const appliedSummary = ref('')
const filters = reactive({
  generationBatchId: '' as number | '',
  dateFrom: '' as string | null,
  dateTo: '' as string | null,
  status: '',
  search: '',
})
const selectedIds = ref<number[]>([])

const rows = computed(() => groupByDay(records.value, record => record.completedAt))
const rowKey = groupedRowKey('record')
const spanMethod = dateGroupSpan(COLUMN_COUNT)
const selectedSet = computed(() => new Set(selectedIds.value))
const allSelected = computed(() => records.value.length > 0 && selectedIds.value.length === records.value.length)
const partiallySelected = computed(() => selectedIds.value.length > 0 && selectedIds.value.length < records.value.length)

function machineCode(record: AnyRecord) {
  return String(record.machine?.identificationCode || record.machine?.sn || '')
}

function buildSummary(count: number) {
  const parts = [`${count} 条录入记录`]
  const batch = generationBatches.value.find(item => item.id === filters.generationBatchId)
  if (batch) {
    parts.push(`生成批次 ${batch.batchCode}`)
  }
  if (filters.dateFrom || filters.dateTo) {
    parts.push(`完成日期 ${filters.dateFrom || '最早'} 至 ${filters.dateTo || '今天'}`)
  }
  if (filters.status) {
    parts.push(qualityStatus(filters.status).label)
  }
  if (count >= SERVER_LIMIT) {
    parts.push(`仅显示最近 ${SERVER_LIMIT} 条`)
  }
  return parts.join(' · ')
}

let loadSequence = 0

async function load() {
  const dateFrom = filters.dateFrom || ''
  const dateTo = filters.dateTo || ''
  if (dateFrom && dateTo && dateFrom > dateTo) {
    notifyError('开始日期不能晚于结束日期', '筛选日期无效')
    return
  }
  const sequence = ++loadSequence
  loading.value = true
  try {
    const list = await recordsApi.records({
      productModelId: props.productId,
      generationBatchId: filters.generationBatchId,
      status: filters.status,
      search: filters.search.trim(),
      dateFrom,
      dateTo,
    })
    if (sequence !== loadSequence) {
      return
    }
    records.value = list ?? []
    selectedIds.value = []
    loadError.value = ''
    appliedSummary.value = buildSummary(records.value.length)
    emit('loaded', records.value.length)
  }
  catch (error) {
    if (sequence !== loadSequence) {
      return
    }
    loadError.value = errorMessage(error, '逐台录入记录加载失败')
    notifyError(error, '逐台录入记录加载失败')
  }
  finally {
    if (sequence === loadSequence) {
      loading.value = false
    }
  }
}

async function loadGenerationBatches() {
  try {
    generationBatches.value = (await catalogApi.productCodeBatches(props.productId)) ?? []
  }
  catch (error) {
    notifyError(error, '生成批次加载失败')
  }
}

const loadDebounced = useDebounceFn(load, 240)

// Keyword typing is debounced; every other filter reloads at once.
watch(
  () => [filters.generationBatchId, filters.dateFrom || '', filters.dateTo || '', filters.status, filters.search.trim()] as const,
  (next, previous) => {
    if (next.every((value, index) => value === previous[index])) {
      return
    }
    const onlySearch = next.slice(0, 4).every((value, index) => value === previous[index])
    if (onlySearch) {
      loadDebounced()
    }
    else {
      load()
    }
  },
)

function resetFilters() {
  filters.generationBatchId = ''
  filters.dateFrom = ''
  filters.dateTo = ''
  filters.status = ''
  filters.search = ''
}

// ---- selection ------------------------------------------------------------------

function isSelected(id: unknown) {
  return selectedSet.value.has(Number(id))
}

function toggleSelection(id: unknown, checked: boolean) {
  const value = Number(id)
  const next = selectedIds.value.filter(item => item !== value)
  if (checked) {
    next.push(value)
  }
  selectedIds.value = next
}

function setQualitySelection(checked: boolean) {
  selectedIds.value = checked ? records.value.map(record => Number(record.id)) : []
}

// ---- actions ----------------------------------------------------------------------

const qualityVisible = ref(false)
const qualityRecords = ref<AnyRecord[]>([])
const editVisible = ref(false)
const editingRecord = ref<AnyRecord | null>(null)
const deleteVisible = ref(false)
const deletingRecord = ref<AnyRecord | null>(null)

function openRecordQualitySelection(list: AnyRecord[]) {
  if (!list.length) {
    return
  }
  qualityRecords.value = list
  qualityVisible.value = true
}

function openRecordQuality(record: AnyRecord) {
  openRecordQualitySelection([record])
}

function openBulkQuality() {
  const list = records.value.filter(record => selectedSet.value.has(Number(record.id)))
  if (list.length > RECORD_STATUS_BULK_LIMIT) {
    notifyError(`单次最多批量处理 ${RECORD_STATUS_BULK_LIMIT} 条质量记录`, '无法批量处理')
    return
  }
  openRecordQualitySelection(list)
}

function openRecordEdit(record: AnyRecord) {
  editingRecord.value = record
  editVisible.value = true
}

function openRecordDelete(record: AnyRecord) {
  deletingRecord.value = record
  deleteVisible.value = true
}

function openRecordGenealogy(record: AnyRecord) {
  const code = machineCode(record)
  if (code) {
    emit('genealogy', code)
  }
}

defineExpose({ load })

onMounted(() => {
  loadGenerationBatches()
  load()
})
</script>

<template>
  <div>
    <div class="pts-filter-bar">
      <div class="pts-filter-field">
        <label for="trace-unit-batch-filter">生成批次</label>
        <ElSelect
          id="trace-unit-batch-filter"
          v-model="filters.generationBatchId"
          :empty-values="[null, undefined]"
          filterable
          class="w-60!"
          aria-label="筛选生成批次"
        >
          <ElOption label="全部生成批次" value="" />
          <ElOption v-for="batch in generationBatches" :key="batch.id" :label="batch.batchCode" :value="batch.id">
            <span class="pts-code">{{ batch.batchCode }}</span>
            <span class="pts-muted text-xs ml-2">{{ batch.quantity }} 套</span>
          </ElOption>
        </ElSelect>
      </div>
      <div class="pts-filter-field">
        <label for="trace-unit-date-from">完成开始日期</label>
        <ElDatePicker id="trace-unit-date-from" v-model="filters.dateFrom" type="date" value-format="YYYY-MM-DD" placeholder="开始日期" class="w-40!" aria-label="完成开始日期" />
      </div>
      <div class="pts-filter-field">
        <label for="trace-unit-date-to">完成结束日期</label>
        <ElDatePicker id="trace-unit-date-to" v-model="filters.dateTo" type="date" value-format="YYYY-MM-DD" placeholder="结束日期" class="w-40!" aria-label="完成结束日期" />
      </div>
      <div class="pts-filter-field">
        <label for="trace-unit-status-filter">质量状态</label>
        <ElSelect id="trace-unit-status-filter" v-model="filters.status" :empty-values="[null, undefined]" class="w-32!" aria-label="筛选质量状态">
          <ElOption v-for="option in STATUS_OPTIONS" :key="option.value" :label="option.label" :value="option.value" />
        </ElSelect>
      </div>
      <div class="pts-filter-field pts-filter-grow">
        <label for="trace-unit-search">关键词</label>
        <ElInput id="trace-unit-search" v-model="filters.search" maxlength="100" clearable placeholder="SN、主码、部件码或操作人" aria-label="关键词">
          <template #prefix>
            <FaIcon name="i-ri:search-line" />
          </template>
        </ElInput>
      </div>
      <ElButton id="trace-unit-filter-reset" @click="resetFilters">
        <template #icon>
          <FaIcon name="i-ri:restart-line" />
        </template>
        重置筛选
      </ElButton>
    </div>
    <p id="trace-unit-filter-summary" class="pts-muted text-sm mt-0 mb-3" aria-live="polite">
      {{ appliedSummary || '加载中…' }}
    </p>
    <div class="pts-bulk-toolbar">
      <ElCheckbox
        id="trace-quality-select-all"
        :model-value="allSelected"
        :indeterminate="partiallySelected"
        :disabled="!records.length"
        @change="setQualitySelection(Boolean($event))"
      >
        全选当前列表
      </ElCheckbox>
      <span id="trace-quality-selected" aria-live="polite">已选择 {{ selectedIds.length }} 条</span>
      <ElButton id="trace-quality-bulk-button" size="small" class="pts-bulk-action" :disabled="!selectedIds.length" @click="openBulkQuality">
        <template #icon>
          <FaIcon name="i-ri:checkbox-multiple-line" />
        </template>
        批量处理
      </ElButton>
    </div>
    <ElTable
      id="trace-unit-record-table"
      v-loading="loading"
      :data="rows"
      :row-key="rowKey"
      :span-method="spanMethod"
      :row-class-name="dateGroupRowClass"
      :empty-text="loadError || '当前筛选条件下没有逐台录入记录'"
    >
      <ElTableColumn width="52">
        <template #header>
          <span class="sr-only">选择</span>
        </template>
        <template #default="{ row }">
          <div v-if="isGroupRow(row)" class="pts-date-group">
            <strong>{{ row.label }}</strong>
            <span>{{ row.count }} 条记录</span>
          </div>
          <ElCheckbox
            v-else
            :model-value="isSelected(row.id)"
            :aria-label="`选择质量记录 ${row.machine?.sn || row.id}`"
            @change="toggleSelection(row.id, Boolean($event))"
          />
        </template>
      </ElTableColumn>
      <ElTableColumn label="产品主码 / SN" min-width="220">
        <template #default="{ row }">
          <span class="pts-code pts-cell-main">{{ row.machine?.identificationCode || '-' }}</span>
          <span class="pts-cell-sub pts-code">{{ row.machine?.sn || '' }}</span>
        </template>
      </ElTableColumn>
      <ElTableColumn label="记录单号" min-width="160">
        <template #default="{ row }">
          <span class="pts-code">{{ row.traceNo || '-' }}</span>
        </template>
      </ElTableColumn>
      <ElTableColumn label="生成批次" min-width="170">
        <template #default="{ row }">
          <span :class="{ 'pts-code': row.generationBatch }">{{ generationBatchLabel(row) }}</span>
        </template>
      </ElTableColumn>
      <ElTableColumn label="部件" width="80">
        <template #default="{ row }">
          {{ (row.parts || []).length }} 个
        </template>
      </ElTableColumn>
      <ElTableColumn label="质量状态" width="150">
        <template #default="{ row }">
          <ElTag :type="qualityStatus(row.status).type" disable-transitions>
            {{ qualityStatus(row.status).label }}
          </ElTag>
          <span v-if="row.statusReason" class="pts-cell-sub mt-1">{{ row.statusReason }}</span>
        </template>
      </ElTableColumn>
      <ElTableColumn label="操作人" width="130">
        <template #default="{ row }">
          <span class="pts-cell-main">{{ row.operatorName || '-' }}</span>
          <span v-if="row.stationName" class="pts-cell-sub">{{ row.stationName }}</span>
        </template>
      </ElTableColumn>
      <ElTableColumn label="完成时间" width="150">
        <template #default="{ row }">
          {{ formatDate(row.completedAt) }}
        </template>
      </ElTableColumn>
      <ElTableColumn label="操作" width="290" align="right">
        <template #default="{ row }">
          <ElButton link type="primary" :loading="!!genealogyBusy && genealogyBusy === machineCode(row)" @click="openRecordGenealogy(row)">
            <template #icon>
              <FaIcon name="i-ri:node-tree" />
            </template>
            族谱
          </ElButton>
          <ElButton link type="primary" @click="openRecordQuality(row)">
            <template #icon>
              <FaIcon name="i-ri:shield-check-line" />
            </template>
            质量处理
          </ElButton>
          <ElButton link type="primary" @click="openRecordEdit(row)">
            <template #icon>
              <FaIcon name="i-ri:edit-line" />
            </template>
            修改
          </ElButton>
          <ElButton link type="danger" @click="openRecordDelete(row)">
            <template #icon>
              <FaIcon name="i-ri:delete-bin-line" />
            </template>
            删除
          </ElButton>
        </template>
      </ElTableColumn>
    </ElTable>

    <RecordQualityDialog v-model="qualityVisible" :records="qualityRecords" @done="load" />
    <RecordEditDialog v-model="editVisible" :record="editingRecord" @done="load" />
    <RecordDeleteDialog v-model="deleteVisible" :record="deletingRecord" @done="load" />
  </div>
</template>
