<script setup lang="ts">
/**
 * 库存同步（管理员、运营）— one-way push of finished-goods stock to Lingxing
 * through 快捷入库: each product's pending Lingxing 收货单 is matched by SKU and
 * fast-received. Sync everything or a ticked subset; a run where only some
 * products succeed is reported as partial success, and every product keeps
 * its own last sync state (and failure reason).
 */
import type { AnyRecord } from '@/api/types'
import { errorMessage } from '@/api'
import operationsApi from '@/api/modules/operations'
import { notifyError, notifySuccess, notifyWarning } from '@/utils/feedback'
import { formatDate, formatNumber, syncStatus } from '@/utils/format'

defineOptions({
  name: 'InventorySyncPage',
})

/** Overall run status; PARTIAL only exists at run level. */
const OVERALL_STATUS: Record<string, { label: string, tone: string }> = {
  PENDING: { label: '待推送', tone: 'warning' },
  PUSHED: { label: '已推送', tone: 'success' },
  FAILED: { label: '推送失败', tone: 'danger' },
  PARTIAL: { label: '部分成功', tone: 'warning' },
}

const loading = ref(false)
const loaded = ref(false)
const loadError = ref('')
const overall = ref<AnyRecord>({})
const items = ref<AnyRecord[]>([])
const selected = ref(new Set<number>())
const syncing = ref<'all' | 'selected' | null>(null)

const metrics = computed(() => {
  const data = overall.value
  const status = OVERALL_STATUS[String(data.syncStatus ?? '')]
  return [
    { label: '同步状态', value: status?.label ?? '尚未同步', hint: '最近一次整体同步结果', tone: status?.tone ?? '', small: false },
    { label: '产品总数', value: String(data.total ?? items.value.length), hint: '参与同步的产品数量', tone: '', small: false },
    { label: '已推送 / 待推送', value: `${data.pushed ?? 0} / ${data.pending ?? 0}`, hint: '各产品同步进度', tone: '', small: false },
    { label: '最近同步时间', value: data.syncedAt ? formatDate(data.syncedAt) : '-', hint: '最近一次同步时间', tone: '', small: true },
  ]
})

const tableSummary = computed(() => (loaded.value
  ? `${items.value.length} 个产品 · 已推送 ${overall.value.pushed ?? 0} · 失败 ${overall.value.failed ?? 0}`
  : '按产品查看当前库存与最近一次同步状态'))

const allSelected = computed(() => items.value.length > 0
  && items.value.every(item => selected.value.has(Number(item.productModelId))))
const someSelected = computed(() => !allSelected.value
  && items.value.some(item => selected.value.has(Number(item.productModelId))))

function productLabel(item: AnyRecord) {
  return item.productName || item.sku || ''
}

function toggleItem(id: number, checked: unknown) {
  const next = new Set(selected.value)
  if (checked) {
    next.add(id)
  }
  else {
    next.delete(id)
  }
  selected.value = next
}

function toggleAll(checked: unknown) {
  selected.value = checked ? new Set(items.value.map(item => Number(item.productModelId))) : new Set()
}

function applyBoard(data: AnyRecord | null | undefined, { keepSelection = false } = {}) {
  overall.value = data?.overall ?? {}
  items.value = data?.items ?? []
  const present = new Set(items.value.map(item => Number(item.productModelId)))
  selected.value = keepSelection
    ? new Set([...selected.value].filter(id => present.has(id)))
    : new Set()
  loaded.value = true
}

async function loadInventorySync({ keepSelection = false } = {}) {
  loading.value = true
  try {
    applyBoard(await operationsApi.inventorySync(), { keepSelection })
    loadError.value = ''
  }
  catch (error) {
    loadError.value = errorMessage(error, '库存同步状态加载失败')
  }
  finally {
    loading.value = false
  }
}

async function syncInventory(selectedOnly: boolean) {
  if (syncing.value) {
    return
  }
  let body: AnyRecord = {}
  if (selectedOnly) {
    const ids = items.value.map(item => Number(item.productModelId)).filter(id => selected.value.has(id))
    if (!ids.length) {
      notifyError('请至少选择一个要同步的产品', '请先勾选产品')
      return
    }
    body = { productModelIds: ids }
  }
  syncing.value = selectedOnly ? 'selected' : 'all'
  try {
    const result = await operationsApi.syncInventory(body)
    applyBoard({ overall: result?.overall, items: result?.items })
    loadError.value = ''
    const failed = (result?.results ?? []).filter((item: AnyRecord) => item.status === 'FAILED').length
    if (result?.syncStatus === 'PARTIAL' || failed) {
      notifyWarning('库存同步部分完成', `成功 ${result?.itemCount ?? 0} 个 · 失败 ${failed} 个`)
    }
    else {
      notifySuccess('库存同步完成', `本次快捷入库 ${result?.itemCount ?? 0} 个产品`)
    }
  }
  catch (error) {
    notifyError(error, '库存同步失败')
    // A failed run still records each product's 推送失败 and reason: show them,
    // keeping the ticked products so the operator can retry.
    await loadInventorySync({ keepSelection: true })
  }
  finally {
    syncing.value = null
  }
}

onMounted(() => loadInventorySync())
</script>

<template>
  <div class="pts-page">
    <FaPageHeader title="库存同步" description="通过领星「快捷入库」接收对应收货单，将成品库存同步至领星">
      <div class="flex flex-wrap gap-2">
        <ElButton
          id="inventory-sync-selected-button"
          :loading="syncing === 'selected'"
          :disabled="syncing === 'all'"
          @click="syncInventory(true)"
        >
          <template #icon>
            <FaIcon name="i-ri:checkbox-multiple-line" />
          </template>
          同步所选
        </ElButton>
        <ElButton
          id="inventory-sync-button"
          type="primary"
          :loading="syncing === 'all'"
          :disabled="syncing === 'selected'"
          @click="syncInventory(false)"
        >
          <template #icon>
            <FaIcon name="i-ri:refresh-line" />
          </template>
          全部同步
        </ElButton>
      </div>
    </FaPageHeader>

    <FaPageMain title="同步概况">
      <div id="inventory-sync-status" v-loading="loading && !loaded" aria-live="polite">
        <div v-if="loadError && !loaded" class="pts-empty pts-text-danger">
          {{ loadError }}
        </div>
        <div v-else-if="!loaded" class="pts-empty">
          尚未执行本次库存同步
        </div>
        <template v-else>
          <div class="pts-metrics">
            <div v-for="item in metrics" :key="item.label" class="pts-metric" :data-tone="item.tone || undefined">
              <span class="pts-metric-label">{{ item.label }}</span>
              <span class="pts-metric-value" :class="{ 'text-lg!': item.small }">{{ item.value }}</span>
              <span class="pts-metric-hint">{{ item.hint }}</span>
            </div>
          </div>
          <ElAlert v-if="overall.error" type="warning" :closable="false" show-icon class="mt-3" :title="`上次同步失败：${overall.error}`" />
          <ElAlert v-if="loadError" type="error" :closable="false" show-icon class="mt-3" :title="loadError" />
        </template>
      </div>
    </FaPageMain>

    <FaPageMain title="成品库存同步状态">
      <p id="inventory-sync-summary" class="pts-muted text-sm mt-0 mb-3">
        {{ tableSummary }}
      </p>
      <ElTable id="inventory-sync-table" v-loading="loading" :data="items" row-key="productModelId" empty-text="暂无成品库存，扫码入库后在此显示" stripe>
        <ElTableColumn width="52" align="center">
          <template #header>
            <ElCheckbox
              id="inventory-sync-check-all"
              :model-value="allSelected"
              :indeterminate="someSelected"
              :disabled="!items.length"
              aria-label="全选产品"
              @change="toggleAll"
            />
          </template>
          <template #default="{ row }">
            <ElCheckbox
              :model-value="selected.has(Number(row.productModelId))"
              :aria-label="`选择产品 ${productLabel(row)}`"
              @change="toggleItem(Number(row.productModelId), $event)"
            />
          </template>
        </ElTableColumn>
        <ElTableColumn label="产品" min-width="200">
          <template #default="{ row }">
            <span class="pts-cell-main">{{ row.productName || '-' }}</span>
            <span class="pts-cell-sub pts-code">{{ row.sku || '' }}</span>
          </template>
        </ElTableColumn>
        <ElTableColumn label="当前库存" width="110">
          <template #default="{ row }">
            {{ formatNumber(row.quantity ?? 0) }}
          </template>
        </ElTableColumn>
        <ElTableColumn label="已同步数量" width="110">
          <template #default="{ row }">
            {{ formatNumber(row.syncedQuantity ?? 0) }}
          </template>
        </ElTableColumn>
        <ElTableColumn label="同步状态" min-width="180">
          <template #default="{ row }">
            <ElTag :type="syncStatus(row.syncStatus).type" disable-transitions>
              {{ syncStatus(row.syncStatus).label }}
            </ElTag>
            <span v-if="row.pushError" class="pts-cell-sub pts-text-warning mt-1">{{ row.pushError }}</span>
          </template>
        </ElTableColumn>
        <ElTableColumn label="领星单号" min-width="160">
          <template #default="{ row }">
            <span class="pts-code">{{ row.lingxingId || '-' }}</span>
          </template>
        </ElTableColumn>
        <ElTableColumn label="最近同步时间" width="150">
          <template #default="{ row }">
            {{ row.syncedAt ? formatDate(row.syncedAt) : '-' }}
          </template>
        </ElTableColumn>
      </ElTable>
    </FaPageMain>

    <div class="mx-4">
      <ElAlert
        type="info"
        :closable="false"
        show-icon
        title="同步前检查"
        description="请先由管理员在系统设置中保存领星凭据。同步时会先按 SKU 匹配「待收货」的领星收货单，再执行快捷入库；未找到对应收货单的产品会同步失败并提示。凭据或接口未配置时，系统会在网络请求前停止，不修改本地库存。"
      />
    </div>
  </div>
</template>
