<script setup lang="ts">
/**
 * 供应商详情（管理员）— the supplier's parts (with safety-stock alerts), its
 * inventory batches and the products whose BOM uses its parts.
 *
 * Parts and batches are maintained here: 添加部件 / 编辑, 登记到货批次 /
 * 编辑批次 (the consumed quantity is kept), and each batch's 库存流水
 * (RECEIPT / ISSUE / ADJUSTMENT movements).
 */
import type { AnyRecord } from '@/api/types'
import catalogApi from '@/api/modules/catalog'
import { notifyError } from '@/utils/feedback'
import { formatDate, formatNumber } from '@/utils/format'
import InventoryBatchDialog from './components/InventoryBatchDialog.vue'
import PartFormDialog from './components/PartFormDialog.vue'
import SupplierFormDialog from './components/SupplierFormDialog.vue'
import { movementType, partStockStatus } from './utils'

defineOptions({
  name: 'SupplierDetailPage',
})

const route = useRoute()
const router = useRouter()

const supplierId = computed(() => Number(route.params.id))
const loading = ref(false)
const loaded = ref(false)
const loadFailed = ref(false)
const detail = ref<AnyRecord | null>(null)

const supplier = computed<AnyRecord | null>(() => detail.value?.supplier ?? null)
const parts = computed<AnyRecord[]>(() => detail.value?.parts ?? [])
const batches = computed<AnyRecord[]>(() => detail.value?.batches ?? [])
const usedInProducts = computed<AnyRecord[]>(() => detail.value?.usedInProducts ?? [])
/** The header actions need the loaded detail (parts for the batch form, the supplier for the edit form). */
const actionsDisabled = computed(() => loading.value || !detail.value)

const codeLine = computed(() => {
  const data = supplier.value
  if (!data) {
    return loading.value || !loaded.value ? '正在加载部件、批次与关联产品…' : ''
  }
  return `${data.supplierCode}${data.contact ? ` · ${data.contact}` : ''}${data.phone ? ` · ${data.phone}` : ''}`
})

const summary = computed(() => {
  const data = supplier.value ?? {}
  return [
    { label: '供应部件', value: data.partCount ?? 0, hint: '该供应商维护的部件型号', tone: '' },
    { label: '到货批次', value: data.batchCount ?? 0, hint: '所有部件的供应批次', tone: '' },
    { label: '可用库存', value: formatNumber(data.quantityAvailable) || 0, hint: '生成产品码时自动扣减', tone: Number(data.quantityAvailable) > 0 ? '' : 'danger' },
    { label: '供应商状态', value: data.active ? '启用' : '停用', hint: '停用后不可用于新产品', tone: data.active ? 'success' : '' },
  ]
})

async function load() {
  if (!Number.isInteger(supplierId.value) || supplierId.value <= 0) {
    loaded.value = true
    return
  }
  loading.value = true
  loadFailed.value = false
  try {
    detail.value = await catalogApi.supplier(supplierId.value)
  }
  catch (error) {
    loadFailed.value = detail.value === null
    notifyError(error, '供应商详情加载失败')
  }
  finally {
    loading.value = false
    loaded.value = true
  }
}

function back() {
  router.push({ name: 'suppliers' })
}

function openRelatedProduct(product: AnyRecord) {
  router.push({ name: 'product-detail', params: { id: product.id } })
}

// ---- supplier form -------------------------------------------------------------

const supplierFormVisible = ref(false)

function openSupplierModal() {
  if (supplier.value) {
    supplierFormVisible.value = true
  }
}

// ---- part form ------------------------------------------------------------------

const partFormVisible = ref(false)
const editingPart = ref<AnyRecord | null>(null)

function openPartModal(part: AnyRecord | null = null) {
  if (!detail.value) {
    return
  }
  editingPart.value = part
  partFormVisible.value = true
}

// ---- inventory batch form ----------------------------------------------------

const batchFormVisible = ref(false)
const editingBatch = ref<AnyRecord | null>(null)
const preferredPartId = ref<number | null>(null)

function openInventoryBatchModal(batch: AnyRecord | null = null, partId: number | null = null) {
  if (!detail.value) {
    return
  }
  if (!parts.value.length) {
    notifyError('登记批次前需要先维护部件型号', '请先添加供应部件')
    return
  }
  editingBatch.value = batch
  preferredPartId.value = partId
  batchFormVisible.value = true
}

// ---- inventory movements ---------------------------------------------------------

const movementVisible = ref(false)
const movementBatch = ref<AnyRecord | null>(null)
const movements = ref<AnyRecord[]>([])
const movementLoadingId = ref<number | null>(null)

const movementSummary = computed(() => {
  const batch = movementBatch.value
  if (!batch) {
    return []
  }
  return [
    { label: '累计入库', value: batch.quantityReceived },
    { label: '累计领用', value: batch.quantityConsumed },
    { label: '当前结存', value: batch.quantityAvailable },
    { label: '流水笔数', value: movements.value.length },
  ]
})

async function openInventoryMovements(batch: AnyRecord) {
  if (movementLoadingId.value !== null) {
    return
  }
  movementLoadingId.value = Number(batch.id)
  try {
    movements.value = await catalogApi.inventoryMovements(Number(batch.id))
    movementBatch.value = batch
    movementVisible.value = true
  }
  catch (error) {
    notifyError(error, '库存流水加载失败')
  }
  finally {
    movementLoadingId.value = null
  }
}

function signedQuantity(value: unknown) {
  const number = Number(value)
  return `${number > 0 ? '+' : ''}${formatNumber(value)}`
}

onMounted(load)
</script>

<template>
  <div class="pts-page">
    <FaPageHeader :description="codeLine">
      <template #title>
        <span class="pts-step-label block">供应商详情</span>
        <span id="supplier-detail-name">{{ supplier?.name || '供应商详情' }}</span>
      </template>
      <div class="flex flex-wrap gap-2">
        <ElButton id="supplier-back" @click="back">
          <template #icon>
            <FaIcon name="i-ri:arrow-left-line" />
          </template>
          返回供应商
        </ElButton>
        <ElButton id="edit-supplier" :disabled="actionsDisabled" @click="openSupplierModal">
          <template #icon>
            <FaIcon name="i-ri:edit-line" />
          </template>
          编辑供应商
        </ElButton>
        <ElButton id="add-supplier-part" :disabled="actionsDisabled" @click="openPartModal()">
          <template #icon>
            <FaIcon name="i-ri:add-line" />
          </template>
          添加部件
        </ElButton>
        <ElButton id="add-supplier-batch" type="primary" :disabled="actionsDisabled" @click="openInventoryBatchModal()">
          <template #icon>
            <FaIcon name="i-ri:inbox-archive-line" />
          </template>
          登记到货批次
        </ElButton>
      </div>
    </FaPageHeader>

    <div id="supplier-detail" v-loading="loading" :aria-busy="loading">
      <FaPageMain v-if="loaded && !loading && !detail">
        <div class="pts-empty">
          <FaIcon name="i-ri:error-warning-line" class="text-3xl mb-2" />
          <div class="text-base font-medium">
            {{ loadFailed ? '供应商详情加载失败' : '未找到该供应商' }}
          </div>
          <p class="mt-1 mb-3">
            {{ loadFailed ? '请检查网络后重试。' : '供应商可能不存在，请返回列表重新选择。' }}
          </p>
          <ElButton v-if="loadFailed" @click="load">
            <template #icon>
              <FaIcon name="i-ri:refresh-line" />
            </template>
            重新加载
          </ElButton>
          <ElButton v-else @click="back">
            返回供应商
          </ElButton>
        </div>
      </FaPageMain>

      <template v-if="detail">
        <FaPageMain>
          <div id="supplier-summary-strip" class="pts-metrics">
            <div v-for="item in summary" :key="item.label" class="pts-metric" :data-tone="item.tone || undefined">
              <span class="pts-metric-label">{{ item.label }}</span>
              <span class="pts-metric-value">{{ item.value }}</span>
              <span class="pts-metric-hint">{{ item.hint }}</span>
            </div>
          </div>
        </FaPageMain>

        <FaPageMain>
          <template #title>
            <span class="text-foreground font-medium">关联产品</span>
            <span class="text-xs ml-2">查看该供应商部件被哪些产品使用，以及每套产品的用量</span>
          </template>
          <ElTable id="supplier-product-usage-table" :data="usedInProducts" row-key="id" empty-text="当前没有产品使用该供应商的部件" stripe>
            <ElTableColumn label="产品" min-width="200">
              <template #default="{ row }">
                <span class="pts-cell-main">{{ row.name }}</span>
                <span class="pts-cell-sub pts-code">{{ row.productCode }}</span>
              </template>
            </ElTableColumn>
            <ElTableColumn label="使用部件" min-width="200">
              <template #default="{ row }">
                <span v-for="part in row.parts" :key="part.id" class="pts-cell-sub">{{ part.name }} × {{ part.requiredQuantity }}</span>
              </template>
            </ElTableColumn>
            <ElTableColumn label="每套用量" prop="requiredQuantity" width="100" align="right" />
            <ElTableColumn label="已生成套数" prop="generatedCount" width="110" align="right" />
            <ElTableColumn label="操作" width="110" align="right" fixed="right">
              <template #default="{ row }">
                <ElButton link type="primary" @click="openRelatedProduct(row)">
                  查看产品
                </ElButton>
              </template>
            </ElTableColumn>
          </ElTable>
        </FaPageMain>

        <FaPageMain>
          <template #title>
            <span class="text-foreground font-medium">供应部件</span>
            <span class="text-xs ml-2">同一供应商可维护多个部件和规格</span>
          </template>
          <ElTable id="supplier-part-table" :data="parts" row-key="id" empty-text="尚未添加供应部件" stripe>
            <ElTableColumn label="部件" min-width="180">
              <template #default="{ row }">
                <span class="pts-cell-main">{{ row.name }}</span>
                <span v-if="row.stockStatus === 'OUT'" class="pts-cell-sub pts-text-danger">库存已用尽</span>
                <span v-else-if="row.stockStatus === 'LOW'" class="pts-cell-sub pts-text-warning">低于安全库存</span>
              </template>
            </ElTableColumn>
            <ElTableColumn label="编码" min-width="140">
              <template #default="{ row }">
                <span class="pts-code">{{ row.partCode }}</span>
              </template>
            </ElTableColumn>
            <ElTableColumn label="规格型号" min-width="140">
              <template #default="{ row }">
                {{ row.specification || '-' }}
              </template>
            </ElTableColumn>
            <ElTableColumn label="批次数" prop="batchCount" width="90" align="right" />
            <ElTableColumn label="可用库存" width="130">
              <template #default="{ row }">
                <ElTag :type="partStockStatus(row.stockStatus).type" disable-transitions>
                  {{ formatNumber(row.quantityAvailable) || 0 }}
                </ElTag>
                <span class="pts-cell-sub">安全库存 {{ row.minimumStock }}</span>
              </template>
            </ElTableColumn>
            <ElTableColumn label="操作" width="170" align="right" fixed="right">
              <template #default="{ row }">
                <ElButton link type="primary" @click="openPartModal(row)">
                  编辑
                </ElButton>
                <ElButton link type="primary" @click="openInventoryBatchModal(null, row.id)">
                  登记批次
                </ElButton>
              </template>
            </ElTableColumn>
          </ElTable>
        </FaPageMain>

        <FaPageMain>
          <template #title>
            <span class="text-foreground font-medium">到货批次</span>
            <span class="text-xs ml-2">修改批次数量时，系统会保留已领用数量和变动流水</span>
          </template>
          <ElTable id="supplier-batch-table" :data="batches" row-key="id" empty-text="尚未登记到货批次" stripe>
            <ElTableColumn label="部件 / 批次" min-width="200">
              <template #default="{ row }">
                <span class="pts-cell-main">{{ row.partName }}</span>
                <span class="pts-cell-sub pts-code">{{ row.batchNo }}</span>
              </template>
            </ElTableColumn>
            <ElTableColumn label="生产日期" width="120">
              <template #default="{ row }">
                {{ row.productionDate || '-' }}
              </template>
            </ElTableColumn>
            <ElTableColumn label="到货日期" width="120">
              <template #default="{ row }">
                {{ row.receivedDate || '-' }}
              </template>
            </ElTableColumn>
            <ElTableColumn label="入库" width="90" align="right">
              <template #default="{ row }">
                {{ formatNumber(row.quantityReceived) }}
              </template>
            </ElTableColumn>
            <ElTableColumn label="已领用" width="90" align="right">
              <template #default="{ row }">
                {{ formatNumber(row.quantityConsumed) }}
              </template>
            </ElTableColumn>
            <ElTableColumn label="可用" width="100" align="right">
              <template #default="{ row }">
                <ElTag :type="Number(row.quantityAvailable) > 0 ? 'success' : 'danger'" disable-transitions>
                  {{ formatNumber(row.quantityAvailable) || 0 }}
                </ElTag>
              </template>
            </ElTableColumn>
            <ElTableColumn label="操作" width="180" align="right" fixed="right">
              <template #default="{ row }">
                <ElButton link type="primary" :loading="movementLoadingId === row.id" @click="openInventoryMovements(row)">
                  查看流水
                </ElButton>
                <ElButton link type="primary" @click="openInventoryBatchModal(row)">
                  编辑批次
                </ElButton>
              </template>
            </ElTableColumn>
          </ElTable>
        </FaPageMain>
      </template>
    </div>

    <SupplierFormDialog v-model="supplierFormVisible" :supplier="supplier" @saved="load" />
    <PartFormDialog v-model="partFormVisible" :supplier="supplier" :part="editingPart" @saved="load" />
    <InventoryBatchDialog v-model="batchFormVisible" :parts="parts" :batch="editingBatch" :preferred-part-id="preferredPartId" @saved="load" />

    <ElDialog
      id="inventory-movement-modal"
      v-model="movementVisible"
      :title="movementBatch ? `${movementBatch.partName} · 库存流水` : '库存流水'"
      width="880px"
      append-to-body
    >
      <p id="inventory-movement-subtitle" class="pts-muted text-sm mt-0 mb-4">
        {{ supplier?.name || '' }} · 批次 {{ movementBatch?.batchNo || '' }}
      </p>
      <div id="inventory-movement-summary" class="pts-metrics mb-4">
        <div v-for="item in movementSummary" :key="item.label" class="pts-metric">
          <span class="pts-metric-label">{{ item.label }}</span>
          <span class="pts-metric-value">{{ formatNumber(item.value) || 0 }}</span>
        </div>
      </div>
      <ElTable id="inventory-movement-table" :data="movements" row-key="id" empty-text="暂无库存流水" max-height="420" stripe>
        <ElTableColumn label="时间" width="150">
          <template #default="{ row }">
            {{ formatDate(row.occurredAt) }}
          </template>
        </ElTableColumn>
        <ElTableColumn label="业务类型" min-width="170">
          <template #default="{ row }">
            <ElTag :type="movementType(row.movementType).type" disable-transitions>
              {{ movementType(row.movementType).label }}
            </ElTag>
            <span class="pts-cell-sub mt-1">{{ row.reason || '-' }}</span>
          </template>
        </ElTableColumn>
        <ElTableColumn label="数量变化" width="100" align="right">
          <template #default="{ row }">
            <strong class="pts-code" :class="Number(row.quantityChange) < 0 ? 'pts-text-danger' : 'pts-text-success'">{{ signedQuantity(row.quantityChange) }}</strong>
          </template>
        </ElTableColumn>
        <ElTableColumn label="结存" width="90" align="right">
          <template #default="{ row }">
            {{ formatNumber(row.balanceAfter) }}
          </template>
        </ElTableColumn>
        <ElTableColumn label="关联产品 / 批次" min-width="180">
          <template #default="{ row }">
            <span class="pts-cell-main">{{ row.productName || '-' }}</span>
            <!-- productionBatchCode: not returned by the API yet (batch-level issues only carry productName). -->
            <span v-if="row.productCodeBatch || row.productionBatchCode" class="pts-cell-sub pts-code">{{ row.productCodeBatch || row.productionBatchCode }}</span>
          </template>
        </ElTableColumn>
        <ElTableColumn label="操作人" width="110">
          <template #default="{ row }">
            {{ row.actorName || '系统' }}
          </template>
        </ElTableColumn>
      </ElTable>
    </ElDialog>
  </div>
</template>
