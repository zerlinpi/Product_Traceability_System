<script setup lang="ts">
/**
 * 数据概览（管理员）— the board follows the current batch flow
 * (采购单 → 生产订单/溯源码 → 批次登记 → 质量放行 → 成品入库 → 库存同步):
 * metrics, 今日待办 in flow order, the batch quality queue (same dialog as
 * 批次质量处理), recently generated production batches, stock alerts and the
 * latest audit events. Legacy per-unit records still waiting for quality
 * handling get their own queue with selection and bulk handling.
 */
import type { AnyRecord } from '@/api/types'
import type { TagType } from '@/utils/format'
import batchesApi from '@/api/modules/batches'
import recordsApi, { RECORD_STATUS_BULK_LIMIT } from '@/api/modules/records'
import { notifyError, notifyInfo } from '@/utils/feedback'
import { auditEventDescription, auditEventLabel, formatDate, formatNumber, qualityStatus } from '@/utils/format'
import RecordQualityDialog from '../trace/components/RecordQualityDialog.vue'
import { recordMachineLabel } from '../trace/helpers'

defineOptions({
  name: 'DashboardPage',
})

type Tone = 'neutral' | 'success' | 'warning' | 'danger'
type TaskTone = 'danger' | 'warning' | 'info'

interface DashboardTask {
  key: string
  icon: string
  tone: TaskTone
  title: string
  detail: string
  source: string
  count: number
  urgency: '高' | '中' | '低'
  view: string
}

const TASK_TAG: Record<TaskTone, TagType> = {
  danger: 'danger',
  warning: 'warning',
  info: 'info',
}

const router = useRouter()

const loading = ref(false)
const dashboard = ref<AnyRecord | null>(null)
const batches = ref<AnyRecord[]>([])
const refreshedLabel = ref('数据更新中')

const counts = computed<AnyRecord>(() => dashboard.value?.counts ?? {})
const batchQueue = computed<AnyRecord[]>(() => dashboard.value?.batchQualityQueue ?? [])
const unitQueue = computed<AnyRecord[]>(() => dashboard.value?.qualityQueue ?? [])
const stockAlerts = computed<AnyRecord[]>(() => dashboard.value?.stockAlerts ?? [])
const activity = computed<AnyRecord[]>(() => dashboard.value?.recentActivity ?? [])
const recentBatches = computed(() => batches.value.slice(0, 8))

function count(key: string) {
  return Number(counts.value[key] ?? 0) || 0
}

const metrics = computed(() => {
  const ready = dashboard.value !== null
  const registered = count('registeredBatches')
  const hold = count('batchHold')
  const assembled = count('batchAssembled')
  const low = count('lowStockParts')
  const out = count('outOfStockParts')
  const passRate = registered ? `${((count('batchPassed') / registered) * 100).toFixed(1)}%` : '--'
  // Until the first load completes, show placeholders instead of zeros.
  const value = (text: string) => (ready ? text : '-')
  const hint = (text: string) => (ready ? text : '数据更新中')
  return [
    { label: '今日生成批次', value: value(formatNumber(count('todayBatches'))), hint: '今日生成的生产批次', tone: 'neutral' as Tone },
    { label: '批次合格率', value: value(passRate), hint: hint(`已登记 ${registered} 批`), tone: 'success' as Tone },
    { label: '待处理暂扣', value: value(formatNumber(hold)), hint: '暂扣批次，不可入库', tone: (hold ? 'danger' : 'neutral') as Tone },
    { label: '待检批次', value: value(formatNumber(assembled)), hint: '等待质量放行', tone: (assembled ? 'warning' : 'neutral') as Tone },
    { label: '成品库存', value: value(formatNumber(count('finishedGoodsOnHand'))), hint: hint(`${count('pendingStockIn')} 个生产订单待入库`), tone: 'neutral' as Tone },
    { label: '库存预警（项）', value: value(formatNumber(low + out)), hint: hint(`${out} 项缺货`), tone: (out ? 'danger' : low ? 'warning' : 'neutral') as Tone },
  ]
})

// 待办 follows the flow order so the backlog reads as "which step is stuck".
const tasks = computed<DashboardTask[]>(() => {
  const list: DashboardTask[] = []
  const hold = count('batchHold')
  const low = count('lowStockParts')
  const out = count('outOfStockParts')
  if (hold) {
    list.push({ key: 'hold', icon: 'i-ri:alarm-warning-line', tone: 'danger', title: '暂扣批次待处理', detail: '暂扣批次不可入库，需复核后闭环', source: '批次质量处理', count: hold, urgency: '高', view: 'batch-quality' })
  }
  if (out || low) {
    list.push({ key: 'stock', icon: 'i-ri:building-4-line', tone: out ? 'danger' : 'warning', title: '库存预警', detail: out ? '存在缺货部件，请及时补充库存' : '部件库存低于安全库存', source: '供应商库存', count: low + out, urgency: out ? '高' : '中', view: 'suppliers' })
  }
  if (count('pendingProductionOrders')) {
    list.push({ key: 'production-orders', icon: 'i-ri:clipboard-line', tone: 'info', title: '待生成生产订单', detail: '运营已下采购单，等待生成溯源码（第 1 步）', source: '生产订单', count: count('pendingProductionOrders'), urgency: '中', view: 'production-orders' })
  }
  if (count('unregisteredBatches')) {
    list.push({ key: 'batch-entry', icon: 'i-ri:scan-2-line', tone: 'info', title: '待批次登记', detail: '已生成溯源码但尚未扫码登记（第 2 步）', source: '现场批次登记', count: count('unregisteredBatches'), urgency: '中', view: 'batch-entry' })
  }
  if (count('batchAssembled')) {
    list.push({ key: 'batch-quality', icon: 'i-ri:shield-check-line', tone: 'warning', title: '待质量放行', detail: '已登记批次等待合格放行（第 3 步）', source: '批次质量处理', count: count('batchAssembled'), urgency: '中', view: 'batch-quality' })
  }
  if (count('pendingStockIn')) {
    list.push({ key: 'scan-gun', icon: 'i-ri:barcode-box-line', tone: 'info', title: '待成品入库', detail: '生产订单未达计划入库量（第 4 步）', source: '扫码枪入库', count: count('pendingStockIn'), urgency: '中', view: 'scan-gun' })
  }
  if (count('inventorySyncPending')) {
    list.push({ key: 'inventory-sync', icon: 'i-ri:refresh-line', tone: 'info', title: '待库存同步', detail: '成品库存尚未同步至领星', source: '库存同步', count: count('inventorySyncPending'), urgency: '低', view: 'inventory-sync' })
  }
  return list.slice(0, 5)
})

async function loadDashboard() {
  loading.value = true
  try {
    const [data, batchList] = await Promise.all([recordsApi.dashboard(), batchesApi.productionBatches()])
    dashboard.value = data ?? {}
    batches.value = batchList ?? []
    refreshedLabel.value = `数据截至：${formatDate(new Date().toISOString())}`
    unitSelectedIds.value = []
  }
  catch (error) {
    notifyError(error, '页面加载失败')
  }
  finally {
    loading.value = false
  }
}

function goView(view: string) {
  router.push({ name: view })
}

function openSupplier(supplierId: unknown) {
  const id = Number(supplierId)
  if (Number.isInteger(id) && id > 0) {
    router.push({ name: 'supplier-detail', params: { id } })
  }
}

function openBatchDetail(batchId: unknown) {
  const id = Number(batchId)
  if (Number.isInteger(id) && id > 0) {
    router.push({ name: 'batch-gen-detail', params: { id } })
  }
}

// ---- batch quality queue ------------------------------------------------------------

const batchQualityVisible = ref(false)
const batchQualityRecord = ref<AnyRecord | null>(null)

function batchQualityStatus(record: AnyRecord) {
  return String(record.qualityStatus || 'ASSEMBLED')
}

function openBatchQuality(record: AnyRecord) {
  batchQualityRecord.value = record
  batchQualityVisible.value = true
}

// ---- legacy per-unit quality queue ------------------------------------------------

const unitSelectedIds = ref<number[]>([])
const unitSelectedSet = computed(() => new Set(unitSelectedIds.value))
const unitAllSelected = computed(() => unitQueue.value.length > 0 && unitSelectedIds.value.length === unitQueue.value.length)
const unitPartiallySelected = computed(() => unitSelectedIds.value.length > 0 && unitSelectedIds.value.length < unitQueue.value.length)
const qualityVisible = ref(false)
const qualityRecords = ref<AnyRecord[]>([])

function toggleUnitSelection(id: unknown, checked: boolean) {
  const value = Number(id)
  const next = unitSelectedIds.value.filter(item => item !== value)
  if (checked) {
    next.push(value)
  }
  unitSelectedIds.value = next
}

function setQualitySelection(checked: boolean) {
  unitSelectedIds.value = checked ? unitQueue.value.map(record => Number(record.id)) : []
}

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
  const list = unitQueue.value.filter(record => unitSelectedSet.value.has(Number(record.id)))
  if (list.length > RECORD_STATUS_BULK_LIMIT) {
    notifyError(`单次最多批量处理 ${RECORD_STATUS_BULK_LIMIT} 条质量记录`, '无法批量处理')
    return
  }
  openRecordQualitySelection(list)
}

// ---- recent activity --------------------------------------------------------------------

const activityVisible = ref(false)
const activityEvent = ref<AnyRecord | null>(null)

function activityActor(event: AnyRecord) {
  return String(event.actorDisplayName || event.operatorName || event.actorUsername || '系统')
}

function activityTime(event: AnyRecord) {
  return formatDate(event.occurredAt).slice(11)
}

function showActivityHelp() {
  notifyInfo('最近操作', '这里展示最新 8 条关键操作，便于快速核对责任人与时间。')
}

function showActivityDetail(event: AnyRecord) {
  activityEvent.value = event
  activityVisible.value = true
}

onMounted(loadDashboard)
</script>

<template>
  <div class="pts-page">
    <FaPageHeader title="数据概览" description="查看当前产品、二维码与录入状态">
      <div class="flex flex-wrap gap-3 items-center">
        <span id="dashboard-refreshed-at" class="pts-muted text-xs" aria-live="polite">{{ refreshedLabel }}</span>
        <ElButton :loading="loading" @click="loadDashboard">
          <template #icon>
            <FaIcon name="i-ri:refresh-line" />
          </template>
          刷新
        </ElButton>
        <ElButton type="primary" @click="goView('products')">
          <template #icon>
            <FaIcon name="i-ri:qr-code-line" />
          </template>
          生成产品二维码
        </ElButton>
      </div>
    </FaPageHeader>

    <div class="px-4">
      <div id="dashboard-metrics" class="pts-metrics">
        <div v-for="item in metrics" :key="item.label" class="pts-metric" :data-tone="item.tone">
          <span class="pts-metric-label">{{ item.label }}</span>
          <span class="pts-metric-value">{{ item.value }}</span>
          <span class="pts-metric-hint">{{ item.hint }}</span>
        </div>
      </div>

      <div class="mt-4 gap-4 grid items-start xl:grid-cols-[minmax(0,1fr)_356px]">
        <div class="flex flex-col gap-4 min-w-0">
          <!-- 今日待办 -->
          <section class="pts-panel" aria-labelledby="dashboard-tasks-title">
            <header class="pts-panel-heading">
              <div>
                <h2 id="dashboard-tasks-title">
                  今日待办
                </h2>
                <p>集中处理质量、库存和录入任务</p>
              </div>
              <ElButton link type="primary" @click="goView('trace')">
                查看更多
                <FaIcon name="i-ri:arrow-right-s-line" class="ml-1" />
              </ElButton>
            </header>
            <ElTable id="dashboard-task-list" v-loading="loading" :data="tasks" row-key="key" empty-text="今日暂无待办任务">
              <ElTableColumn label="类型" width="70">
                <template #default="{ row }">
                  <span class="pts-task-type" :data-tone="row.tone" aria-hidden="true">
                    <FaIcon :name="row.icon" />
                  </span>
                </template>
              </ElTableColumn>
              <ElTableColumn label="事项" min-width="240">
                <template #default="{ row }">
                  <span class="pts-cell-main">{{ row.title }}</span>
                  <span class="pts-cell-sub">{{ row.detail }}</span>
                </template>
              </ElTableColumn>
              <ElTableColumn label="来源" prop="source" min-width="120" />
              <ElTableColumn label="数量" width="80">
                <template #default="{ row }">
                  {{ formatNumber(row.count) }}
                </template>
              </ElTableColumn>
              <ElTableColumn label="紧急度" width="90">
                <template #default="{ row }">
                  <ElTag :type="TASK_TAG[row.tone as TaskTone]" disable-transitions>
                    {{ row.urgency }}
                  </ElTag>
                </template>
              </ElTableColumn>
              <ElTableColumn label="操作" width="110" align="right">
                <template #default="{ row }">
                  <ElButton size="small" :aria-label="`去处理：${row.title}`" @click="goView(row.view)">
                    去处理
                  </ElButton>
                </template>
              </ElTableColumn>
            </ElTable>
          </section>

          <!-- 待处理批次质量 -->
          <section class="pts-panel" aria-labelledby="dashboard-quality-title">
            <header class="pts-panel-heading">
              <div>
                <h2 id="dashboard-quality-title">
                  待处理批次质量
                </h2>
                <p>放行待检批次，闭环暂扣批次</p>
              </div>
              <ElButton link type="primary" @click="goView('batch-quality')">
                查看全部
                <FaIcon name="i-ri:arrow-right-s-line" class="ml-1" />
              </ElButton>
            </header>
            <ElTable id="dashboard-quality-queue" v-loading="loading" :data="batchQueue" row-key="id" empty-text="当前没有待处理批次">
              <ElTableColumn label="批次 / 产品" min-width="220">
                <template #default="{ row }">
                  <span class="pts-code pts-cell-main">{{ row.batchCode || '-' }}</span>
                  <span class="pts-cell-sub">{{ row.productName || '-' }}</span>
                </template>
              </ElTableColumn>
              <ElTableColumn label="登记台数" width="100">
                <template #default="{ row }">
                  {{ row.registeredQuantity ?? '-' }}
                </template>
              </ElTableColumn>
              <ElTableColumn label="状态" width="100">
                <template #default="{ row }">
                  <ElTag :type="qualityStatus(batchQualityStatus(row)).type" disable-transitions>
                    {{ qualityStatus(batchQualityStatus(row)).label }}
                  </ElTag>
                </template>
              </ElTableColumn>
              <ElTableColumn label="操作人" width="120">
                <template #default="{ row }">
                  {{ row.operatorName || '-' }}
                </template>
              </ElTableColumn>
              <ElTableColumn label="操作" width="100" align="right">
                <template #default="{ row }">
                  <ElButton v-if="batchQualityStatus(row) === 'ASSEMBLED'" size="small" type="primary" plain :aria-label="`处理批次 ${row.batchCode || ''}`" @click="openBatchQuality(row)">
                    处理
                  </ElButton>
                  <span v-else class="pts-cell-sub">已暂扣</span>
                </template>
              </ElTableColumn>
            </ElTable>
          </section>

          <!-- 历史逐台质量待办（旧版逐台录入记录，仅在仍有待处理时显示） -->
          <section v-if="unitQueue.length" class="pts-panel" aria-labelledby="dashboard-unit-quality-title">
            <header class="pts-panel-heading">
              <div>
                <h2 id="dashboard-unit-quality-title">
                  历史逐台质量待办
                </h2>
                <p>逐台录入记录中待检或暂扣的产品，可勾选后批量处理</p>
              </div>
              <ElButton link type="primary" @click="goView('trace')">
                查看全部
                <FaIcon name="i-ri:arrow-right-s-line" class="ml-1" />
              </ElButton>
            </header>
            <div class="pts-bulk-toolbar">
              <ElCheckbox
                id="dashboard-quality-select-all"
                :model-value="unitAllSelected"
                :indeterminate="unitPartiallySelected"
                @change="setQualitySelection(Boolean($event))"
              >
                全选当前列表
              </ElCheckbox>
              <span id="dashboard-quality-selected" aria-live="polite">已选择 {{ unitSelectedIds.length }} 条</span>
              <ElButton id="dashboard-quality-bulk-button" size="small" class="pts-bulk-action" :disabled="!unitSelectedIds.length" @click="openBulkQuality">
                <template #icon>
                  <FaIcon name="i-ri:checkbox-multiple-line" />
                </template>
                批量处理
              </ElButton>
            </div>
            <ElTable id="dashboard-unit-quality-queue" :data="unitQueue" row-key="id" empty-text="当前没有待处理记录">
              <ElTableColumn width="52">
                <template #header>
                  <span class="sr-only">选择</span>
                </template>
                <template #default="{ row }">
                  <ElCheckbox
                    :model-value="unitSelectedSet.has(Number(row.id))"
                    :aria-label="`选择质量记录 ${row.machine?.sn || row.id}`"
                    @change="toggleUnitSelection(row.id, Boolean($event))"
                  />
                </template>
              </ElTableColumn>
              <ElTableColumn label="产品 / SN" min-width="220">
                <template #default="{ row }">
                  <span class="pts-cell-main">{{ row.machine?.productModelName || row.machine?.model || '-' }}</span>
                  <span class="pts-cell-sub pts-code">{{ row.machine?.sn || '' }}</span>
                </template>
              </ElTableColumn>
              <ElTableColumn label="状态" width="150">
                <template #default="{ row }">
                  <ElTag :type="qualityStatus(row.status).type" disable-transitions>
                    {{ qualityStatus(row.status).label }}
                  </ElTag>
                  <span v-if="row.statusReason" class="pts-cell-sub mt-1">{{ row.statusReason }}</span>
                </template>
              </ElTableColumn>
              <ElTableColumn label="操作人" width="120">
                <template #default="{ row }">
                  {{ row.operatorName || '-' }}
                </template>
              </ElTableColumn>
              <ElTableColumn label="完成时间" width="150">
                <template #default="{ row }">
                  {{ formatDate(row.completedAt) }}
                </template>
              </ElTableColumn>
              <ElTableColumn label="操作" width="100" align="right">
                <template #default="{ row }">
                  <ElButton size="small" type="primary" plain :aria-label="`处理 ${recordMachineLabel(row)}`" @click="openRecordQuality(row)">
                    处理
                  </ElButton>
                </template>
              </ElTableColumn>
            </ElTable>
          </section>

          <!-- 最近生成的生产批次 -->
          <section class="pts-panel" aria-labelledby="dashboard-codes-title">
            <header class="pts-panel-heading">
              <div>
                <h2 id="dashboard-codes-title">
                  最近生成的生产批次
                </h2>
                <p>按生成时间查看最新批次溯源码</p>
              </div>
              <ElButton link type="primary" @click="goView('batch-gen')">
                查看全部
                <FaIcon name="i-ri:arrow-right-s-line" class="ml-1" />
              </ElButton>
            </header>
            <ElTable id="dashboard-code-sets" v-loading="loading" :data="recentBatches" row-key="id" empty-text="尚未生成生产批次">
              <ElTableColumn label="产品" min-width="180">
                <template #default="{ row }">
                  <span class="pts-cell-main">{{ row.productName || '-' }}</span>
                  <span class="pts-cell-sub">{{ row.productModelCode || '' }}</span>
                </template>
              </ElTableColumn>
              <ElTableColumn label="批次码" min-width="220">
                <template #default="{ row }">
                  <ElButton link type="primary" class="pts-code" :aria-label="`查看批次 ${row.batchCode || ''} 详情`" @click="openBatchDetail(row.id)">
                    {{ row.batchCode || '-' }}
                  </ElButton>
                </template>
              </ElTableColumn>
              <ElTableColumn label="计划台数" width="100">
                <template #default="{ row }">
                  {{ row.plannedQuantity ?? '-' }}
                </template>
              </ElTableColumn>
              <ElTableColumn label="生成时间" width="150">
                <template #default="{ row }">
                  {{ formatDate(row.generatedAt) }}
                </template>
              </ElTableColumn>
            </ElTable>
          </section>

          <!-- 库存预警 -->
          <section class="pts-panel" aria-labelledby="dashboard-stock-title">
            <header class="pts-panel-heading">
              <div>
                <h2 id="dashboard-stock-title">
                  库存预警
                </h2>
                <p>按部件安全库存阈值自动识别</p>
              </div>
              <ElButton link type="primary" @click="goView('suppliers')">
                查看全部
                <FaIcon name="i-ri:arrow-right-s-line" class="ml-1" />
              </ElButton>
            </header>
            <div id="dashboard-stock-alerts" v-loading="loading && !dashboard" class="pts-alert-list">
              <button
                v-for="part in stockAlerts"
                :key="part.id"
                type="button"
                class="pts-alert-item"
                :aria-label="`${part.name}：${part.stockStatus === 'OUT' ? '缺货' : '低库存'}，可用 ${part.quantityAvailable}，查看供应商`"
                @click="openSupplier(part.supplierId)"
              >
                <span class="pts-alert-severity" :data-level="part.stockStatus === 'OUT' ? 'out' : 'low'">{{ part.stockStatus === 'OUT' ? '缺货' : '低库存' }}</span>
                <span class="pts-alert-copy">
                  <strong>{{ part.name }}</strong>
                  <span>{{ part.supplierName }} · {{ part.partCode }}</span>
                </span>
                <span class="pts-alert-balance">
                  <b>{{ formatNumber(part.quantityAvailable) }}</b>
                  <small>安全库存 {{ formatNumber(part.minimumStock) }}</small>
                </span>
              </button>
              <div v-if="!stockAlerts.length" class="pts-empty">
                {{ dashboard ? '所有启用部件库存正常' : '数据更新中' }}
              </div>
            </div>
          </section>
        </div>

        <!-- 最近操作 -->
        <section class="pts-panel" aria-labelledby="dashboard-activity-title">
          <header class="pts-panel-heading">
            <div>
              <h2 id="dashboard-activity-title">
                最近操作
              </h2>
              <p>关键业务动作与责任人</p>
            </div>
          </header>
          <ul id="dashboard-activity" class="pts-activity-list">
            <li v-for="event in activity" :key="event.id ?? event.eventId" class="pts-activity-item">
              <span class="pts-activity-marker" aria-hidden="true" />
              <div class="pts-activity-copy">
                <div class="pts-activity-line">
                  <time :datetime="event.occurredAt">{{ activityTime(event) }}</time>
                  <span>{{ activityActor(event) }}</span>
                </div>
                <p>{{ auditEventDescription(event) }}</p>
              </div>
              <ElButton size="small" :aria-label="`查看操作：${auditEventLabel(event.eventType)}`" @click="showActivityDetail(event)">
                查看
              </ElButton>
            </li>
            <li v-if="!activity.length" class="pts-empty">
              {{ dashboard ? '暂无操作记录' : '数据更新中' }}
            </li>
          </ul>
          <button type="button" class="pts-activity-more" @click="showActivityHelp">
            查看更多操作
          </button>
        </section>
      </div>
    </div>

    <PtsBatchQualityDialog v-model="batchQualityVisible" :record="batchQualityRecord" @done="loadDashboard" />
    <RecordQualityDialog v-model="qualityVisible" :records="qualityRecords" @done="loadDashboard" />

    <ElDialog v-model="activityVisible" :title="activityEvent ? auditEventLabel(activityEvent.eventType) : '操作详情'" width="460px" class="pts-dialog-fluid" append-to-body>
      <template v-if="activityEvent">
        <p class="text-sm mt-0 mb-4">
          {{ auditEventDescription(activityEvent) }}
        </p>
        <dl class="pts-kv">
          <dt>操作人</dt>
          <dd>{{ activityActor(activityEvent) }}</dd>
          <dt>时间</dt>
          <dd>{{ formatDate(activityEvent.occurredAt) }}</dd>
          <template v-if="activityEvent.stationName">
            <dt>工位</dt>
            <dd>{{ activityEvent.stationName }}</dd>
          </template>
          <template v-if="activityEvent.objectCode">
            <dt>对象</dt>
            <dd class="pts-code">
              {{ activityEvent.objectCode }}
            </dd>
          </template>
          <template v-if="activityEvent.relatedObjectCode">
            <dt>关联对象</dt>
            <dd class="pts-code">
              {{ activityEvent.relatedObjectCode }}
            </dd>
          </template>
          <template v-if="activityEvent.reason">
            <dt>原因</dt>
            <dd>{{ activityEvent.reason }}</dd>
          </template>
        </dl>
      </template>
      <template #footer>
        <ElButton @click="activityVisible = false">
          关闭
        </ElButton>
      </template>
    </ElDialog>
  </div>
</template>
