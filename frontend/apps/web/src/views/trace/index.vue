<script setup lang="ts">
/**
 * 产品查询（管理员）— pick a product, then browse:
 * - its production batches (批次模型): filters by generation date / quality
 *   status / keyword, rows grouped by generation day, QR download and the
 *   batch genealogy (reverse trace to the supplier batches it consumed);
 * - its legacy per-unit records (历史逐台记录): see components/UnitRecords.vue.
 *
 * The genealogy box accepts a batch code (raw or the `PTS:B:` QR payload);
 * anything else is resolved as a legacy per-unit main code or part code via
 * GET /api/genealogy, showing the main QR and every bound part.
 */
import type { AnyRecord } from '@/api/types'
import type { GenealogyView } from './helpers'
import { useDebounceFn } from '@vueuse/core'
import { ApiError } from '@/api'
import batchesApi from '@/api/modules/batches'
import catalogApi from '@/api/modules/catalog'
import recordsApi from '@/api/modules/records'
import { notifyError, notifyWarning } from '@/utils/feedback'
import { formatDate, qualityStatus } from '@/utils/format'
import GenealogyDialog from './components/GenealogyDialog.vue'
import UnitRecords from './components/UnitRecords.vue'
import {
  batchGenealogyView,
  dateGroupRowClass,
  dateGroupSpan,
  dayKey,
  groupByDay,
  groupedRowKey,
  isGroupRow,
  unitGenealogyView,
} from './helpers'

defineOptions({
  name: 'TracePage',
})

const router = useRouter()

const STATUS_OPTIONS = [
  { value: '', label: '全部状态' },
  { value: 'ASSEMBLED', label: '待检' },
  { value: 'PASSED', label: '合格' },
  { value: 'HOLD', label: '暂扣' },
]
/** Leaf columns of the production batch table, for the date header rows. */
const BATCH_COLUMN_COUNT = 7

const loading = ref(false)
const products = ref<AnyRecord[]>([])
const batches = ref<AnyRecord[]>([])
const traceProductId = ref<number | null>(null)
const activeTab = ref<'batches' | 'records'>('batches')
const unitRecordCount = ref<number | null>(null)
const unitRecords = ref<{ load: () => Promise<void> }>()

const traceProduct = computed(() => products.value.find(item => item.id === traceProductId.value) ?? null)
const traceBatches = computed(() => batches.value.filter(batch => batch.productModelId === traceProductId.value))

function traceBatchCount(productId: unknown) {
  return batches.value.filter(batch => batch.productModelId === productId).length
}

function batchRegistered(batch: AnyRecord) {
  return Boolean(batch.registered ?? batch.registeredQuantity ?? batch.entry)
}

function batchQrUrl(batch: AnyRecord) {
  return batchesApi.productionBatchQrUrl(Number(batch.id))
}

async function loadTrace() {
  loading.value = true
  try {
    const [productList, batchList] = await Promise.all([catalogApi.products(), batchesApi.productionBatches()])
    products.value = productList ?? []
    batches.value = batchList ?? []
    if (traceProductId.value !== null && !traceProduct.value) {
      renderTraceProducts()
    }
    else {
      // 刷新 inside a product also refreshes its per-unit records.
      unitRecords.value?.load()
    }
  }
  catch (error) {
    notifyError(error, '页面加载失败')
  }
  finally {
    loading.value = false
  }
}

// ---- product step ---------------------------------------------------------------

function renderTraceProducts() {
  traceProductId.value = null
}

function openTraceProduct(productId: number) {
  if (!products.value.some(item => item.id === productId)) {
    return
  }
  resetTraceFilters()
  unitRecordCount.value = null
  activeTab.value = 'batches'
  traceProductId.value = productId
}

/** 产品管理 detail of the product being browsed. */
function openRelatedProduct(productId: number) {
  router.push({ name: 'product-detail', params: { id: productId } })
}

// ---- production batch filters -----------------------------------------------------

const batchFilters = reactive({
  dateFrom: '' as string | null,
  dateTo: '' as string | null,
  status: '',
  search: '',
})
/** The filters currently applied to the table (an invalid range is not applied). */
const appliedFilters = reactive({
  dateFrom: '',
  dateTo: '',
  status: '',
  search: '',
})

function applyBatchFilters() {
  const dateFrom = batchFilters.dateFrom || ''
  const dateTo = batchFilters.dateTo || ''
  if (dateFrom && dateTo && dateFrom > dateTo) {
    notifyError('开始日期不能晚于结束日期', '筛选日期无效')
    return
  }
  appliedFilters.dateFrom = dateFrom
  appliedFilters.dateTo = dateTo
  appliedFilters.status = batchFilters.status || ''
  appliedFilters.search = batchFilters.search.trim().toLowerCase()
}

const applyBatchFiltersDebounced = useDebounceFn(applyBatchFilters, 240)

watch(() => [batchFilters.dateFrom, batchFilters.dateTo, batchFilters.status], applyBatchFilters)
watch(() => batchFilters.search, () => applyBatchFiltersDebounced())

function resetTraceFilters() {
  batchFilters.dateFrom = ''
  batchFilters.dateTo = ''
  batchFilters.status = ''
  batchFilters.search = ''
  appliedFilters.dateFrom = ''
  appliedFilters.dateTo = ''
  appliedFilters.status = ''
  appliedFilters.search = ''
}

const filteredBatches = computed(() => {
  const { dateFrom, dateTo, status, search } = appliedFilters
  return traceBatches.value.filter((batch) => {
    if (search && !`${batch.batchCode || ''} ${batch.productName || ''} ${batch.prefix || ''}`.toLowerCase().includes(search)) {
      return false
    }
    if (status && (batch.qualityStatus || '') !== status) {
      return false
    }
    const day = dayKey(batch.generatedAt)
    if (dateFrom && day && day < dateFrom) {
      return false
    }
    if (dateTo && day && day > dateTo) {
      return false
    }
    return true
  })
})

const batchRows = computed(() => groupByDay(filteredBatches.value, batch => batch.generatedAt))
const batchRowKey = groupedRowKey('batch')
const batchSpanMethod = dateGroupSpan(BATCH_COLUMN_COUNT)

const batchSummary = computed(() => {
  const { dateFrom, dateTo, status } = appliedFilters
  const parts = [`${filteredBatches.value.length} 个生产批次`]
  if (dateFrom || dateTo) {
    parts.push(`${dateFrom || '最早'} 至 ${dateTo || '今天'}`)
  }
  if (status) {
    parts.push(qualityStatus(status).label)
  }
  return parts.join(' · ')
})

// ---- genealogy --------------------------------------------------------------------

const genealogyInput = ref<{ focus: () => void, select: () => void }>()
const genealogyCode = ref('')
const genealogyVisible = ref(false)
const genealogyView = ref<GenealogyView | null>(null)
/** What is loading: 'search', `batch-<id>` or a unit code. */
const genealogyBusy = ref('')

async function openBatchGenealogy(batchId: number, busyKey = `batch-${batchId}`) {
  if (genealogyBusy.value) {
    return
  }
  genealogyBusy.value = busyKey
  try {
    const data = await batchesApi.productionBatch(batchId)
    genealogyView.value = batchGenealogyView(data ?? {}, batchId)
    genealogyVisible.value = true
  }
  catch (error) {
    notifyError(error, '族谱查询失败')
  }
  finally {
    genealogyBusy.value = ''
  }
}

async function openGenealogy(code: string, busyKey = code, notFoundTitle = '') {
  if (genealogyBusy.value || !code) {
    return
  }
  genealogyBusy.value = busyKey
  try {
    const data = await recordsApi.genealogy(code)
    genealogyView.value = unitGenealogyView(data ?? {})
    genealogyVisible.value = true
  }
  catch (error) {
    const notFound = error instanceof ApiError && error.status === 404
    notifyError(error, notFound && notFoundTitle ? notFoundTitle : '族谱查询失败')
  }
  finally {
    genealogyBusy.value = ''
  }
}

// The lookup resolves a scanned/typed batch code (raw or the PTS:B:… payload)
// to a production batch; anything else is tried as a legacy main / part code.
async function submitGenealogy() {
  const raw = genealogyCode.value.trim()
  if (!raw) {
    notifyWarning('请扫描或输入要查询的码值')
    genealogyInput.value?.focus()
    return
  }
  const batchPayload = /^PTS:B:/i.test(raw)
  const code = raw.replace(/^PTS:B:/i, '').trim()
  const batch = batches.value.find(item => String(item.batchCode || '').toLowerCase() === code.toLowerCase())
  if (batch) {
    await openBatchGenealogy(Number(batch.id), 'search')
  }
  else if (batchPayload) {
    notifyError('请检查批次码或扫描批次二维码', '未找到该批次')
  }
  else {
    await openGenealogy(code, 'search', '未找到该批次')
  }
  genealogyInput.value?.select()
}

onMounted(loadTrace)
</script>

<template>
  <div class="pts-page">
    <FaPageHeader title="产品查询" description="按产品查看生产批次与供应批次族谱">
      <ElButton :loading="loading" @click="loadTrace">
        <template #icon>
          <FaIcon name="i-ri:refresh-line" />
        </template>
        刷新
      </ElButton>
    </FaPageHeader>

    <!-- step 1: choose a product -->
    <FaPageMain v-if="!traceProduct" id="trace-product-step">
      <div class="mb-4">
        <h2 class="text-base font-semibold m-0">
          选择产品
        </h2>
        <p class="pts-muted text-sm mt-1 mb-0">
          先选择产品，再查看该产品的生产记录和产品族谱
        </p>
      </div>
      <div v-loading="loading">
        <div v-if="products.length" id="trace-product-grid" class="pts-product-grid">
          <button
            v-for="product in products"
            :key="product.id"
            type="button"
            class="pts-product-card"
            :aria-label="`查看 ${product.name} 的生产批次`"
            @click="openTraceProduct(product.id)"
          >
            <h3>{{ product.name }}</h3>
            <p>{{ product.componentCount ?? 0 }} 个部件 · {{ traceBatchCount(product.id) }} 个生产批次</p>
            <strong>
              查看生产批次
              <FaIcon name="i-ri:arrow-right-line" />
            </strong>
          </button>
        </div>
        <div v-else class="pts-empty">
          {{ loading ? '加载中…' : '尚未添加产品' }}
        </div>
      </div>
    </FaPageMain>

    <!-- step 2: the product's batches, records and genealogy -->
    <div v-else id="trace-detail">
      <FaPageMain>
        <div class="flex flex-wrap gap-4 items-center justify-between">
          <div class="flex gap-3 min-w-0 items-center">
            <ElButton id="trace-back" @click="renderTraceProducts">
              <template #icon>
                <FaIcon name="i-ri:arrow-left-line" />
              </template>
              返回产品列表
            </ElButton>
            <div class="min-w-0">
              <h2 id="trace-product-name" class="text-lg font-semibold m-0">
                {{ traceProduct.name }}
              </h2>
              <p class="pts-muted text-sm mt-1 mb-0">
                按产品查看生产批次与供应批次族谱
              </p>
            </div>
          </div>
          <ElButton link type="primary" @click="openRelatedProduct(traceProduct.id)">
            查看产品资料
            <FaIcon name="i-ri:arrow-right-s-line" class="ml-1" />
          </ElButton>
        </div>
      </FaPageMain>

      <FaPageMain>
        <div class="flex flex-wrap gap-4 items-center justify-between">
          <div>
            <strong class="text-sm">批次族谱</strong>
            <p class="pts-muted text-sm mt-1 mb-0">
              输入或扫描批次二维码内容；历史产品主码、部件码同样可以查询
            </p>
          </div>
          <form id="genealogy-form" class="flex flex-1 gap-2 max-w-xl min-w-72" autocomplete="off" @submit.prevent="submitGenealogy">
            <ElInput
              ref="genealogyInput"
              v-model="genealogyCode"
              name="code"
              maxlength="120"
              clearable
              class="flex-1"
              placeholder="扫描或输入批次二维码内容"
              aria-label="批次二维码内容"
            >
              <template #prefix>
                <FaIcon name="i-ri:qr-scan-2-line" />
              </template>
            </ElInput>
            <ElButton native-type="submit" :loading="genealogyBusy === 'search'">
              <template #icon>
                <FaIcon name="i-ri:search-line" />
              </template>
              查询族谱
            </ElButton>
          </form>
        </div>
      </FaPageMain>

      <FaPageMain>
        <ElTabs v-model="activeTab">
          <ElTabPane name="batches" :label="`生产批次（${traceBatches.length}）`">
            <p class="pts-muted text-sm mt-0 mb-3">
              按质量状态与生成日期定位，进入批次供应批次族谱
            </p>
            <div class="pts-filter-bar">
              <div class="pts-filter-field">
                <label for="trace-date-from">开始日期</label>
                <ElDatePicker id="trace-date-from" v-model="batchFilters.dateFrom" type="date" value-format="YYYY-MM-DD" placeholder="开始日期" class="w-40!" aria-label="开始日期" />
              </div>
              <div class="pts-filter-field">
                <label for="trace-date-to">结束日期</label>
                <ElDatePicker id="trace-date-to" v-model="batchFilters.dateTo" type="date" value-format="YYYY-MM-DD" placeholder="结束日期" class="w-40!" aria-label="结束日期" />
              </div>
              <div class="pts-filter-field">
                <label for="trace-status-filter">质量状态</label>
                <ElSelect id="trace-status-filter" v-model="batchFilters.status" :empty-values="[null, undefined]" class="w-32!" aria-label="筛选质量状态">
                  <ElOption v-for="option in STATUS_OPTIONS" :key="option.value" :label="option.label" :value="option.value" />
                </ElSelect>
              </div>
              <div class="pts-filter-field pts-filter-grow">
                <label for="trace-record-search">关键词</label>
                <ElInput id="trace-record-search" v-model="batchFilters.search" clearable placeholder="批次码或产品" aria-label="关键词">
                  <template #prefix>
                    <FaIcon name="i-ri:search-line" />
                  </template>
                </ElInput>
              </div>
              <ElButton id="trace-filter-reset" @click="resetTraceFilters">
                <template #icon>
                  <FaIcon name="i-ri:restart-line" />
                </template>
                重置筛选
              </ElButton>
            </div>
            <p id="trace-filter-summary" class="pts-muted text-sm mt-0 mb-3" aria-live="polite">
              {{ batchSummary }}
            </p>
            <ElTable
              id="trace-record-table"
              v-loading="loading"
              :data="batchRows"
              :row-key="batchRowKey"
              :span-method="batchSpanMethod"
              :row-class-name="dateGroupRowClass"
              empty-text="当前筛选条件下没有生产批次"
            >
              <ElTableColumn label="批次码" min-width="230">
                <template #default="{ row }">
                  <div v-if="isGroupRow(row)" class="pts-date-group">
                    <strong>{{ row.label }}</strong>
                    <span>{{ row.count }} 个批次</span>
                  </div>
                  <span v-else class="pts-code pts-cell-main">{{ row.batchCode || '-' }}</span>
                </template>
              </ElTableColumn>
              <ElTableColumn label="产品" min-width="180">
                <template #default="{ row }">
                  <span class="pts-cell-main">{{ row.productName || '-' }}</span>
                  <span class="pts-cell-sub pts-code">{{ row.productModelCode || '' }}</span>
                </template>
              </ElTableColumn>
              <ElTableColumn label="计划台数" width="100">
                <template #default="{ row }">
                  {{ row.plannedQuantity ?? '-' }}
                </template>
              </ElTableColumn>
              <ElTableColumn label="登记台数" width="100">
                <template #default="{ row }">
                  {{ batchRegistered(row) ? (row.registeredQuantity ?? '-') : '未登记' }}
                </template>
              </ElTableColumn>
              <ElTableColumn label="质量状态" width="110">
                <template #default="{ row }">
                  <ElTag v-if="batchRegistered(row)" :type="qualityStatus(row.qualityStatus || 'ASSEMBLED').type" disable-transitions>
                    {{ qualityStatus(row.qualityStatus || 'ASSEMBLED').label }}
                  </ElTag>
                  <ElTag v-else type="info" disable-transitions>
                    未登记
                  </ElTag>
                </template>
              </ElTableColumn>
              <ElTableColumn label="生成时间" width="150">
                <template #default="{ row }">
                  {{ formatDate(row.generatedAt) }}
                </template>
              </ElTableColumn>
              <ElTableColumn label="操作" width="210" align="right">
                <template #default="{ row }">
                  <ElButton link type="primary" tag="a" :href="batchQrUrl(row)" target="_blank" rel="noopener">
                    <template #icon>
                      <FaIcon name="i-ri:download-2-line" />
                    </template>
                    下载二维码
                  </ElButton>
                  <ElButton link type="primary" :loading="genealogyBusy === `batch-${row.id}`" @click="openBatchGenealogy(Number(row.id))">
                    <template #icon>
                      <FaIcon name="i-ri:git-merge-line" />
                    </template>
                    查看族谱
                  </ElButton>
                </template>
              </ElTableColumn>
            </ElTable>
          </ElTabPane>
          <ElTabPane name="records" :label="unitRecordCount === null ? '逐台录入记录' : `逐台录入记录（${unitRecordCount}）`">
            <p class="pts-muted text-sm mt-0 mb-3">
              历史逐台录入的产品主码与部件码：可查看族谱、处理质量状态，或校对、删除录入记录
            </p>
            <UnitRecords
              ref="unitRecords"
              :key="traceProduct.id"
              :product-id="traceProduct.id"
              :genealogy-busy="genealogyBusy"
              @genealogy="openGenealogy"
              @loaded="unitRecordCount = $event"
            />
          </ElTabPane>
        </ElTabs>
      </FaPageMain>
    </div>

    <GenealogyDialog v-model="genealogyVisible" :view="genealogyView" />
  </div>
</template>
