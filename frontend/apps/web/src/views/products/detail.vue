<script setup lang="ts">
/**
 * 产品详情.
 *
 * ADMIN: summary, 产品资料, the BOM with its bound supplier batches and the
 * product's 二维码生成批次. A generation batch opens its QR page view — every
 * code set's main code and part codes, 40 sets per page — printable per set
 * and downloadable as zips. Edit, generate codes and delete from the header.
 *
 * OPERATIONS (their own simple products): 产品资料, edit and delete only. The
 * BOM / code-batch endpoints are admin-only and are never called for them.
 *
 * `?batch=<id>` opens that generation batch directly (used right after codes
 * were generated from the product list).
 */
import type { ProductAttributeMeta, ProductCodeBatchPage } from '@/api/modules/catalog'
import type { AnyRecord } from '@/api/types'
import catalogApi from '@/api/modules/catalog'
import { confirmAction, notifyError, notifySuccess } from '@/utils/feedback'
import { formatDate, padSequence } from '@/utils/format'
import { printElement } from '@/utils/print'
import CodeGenerateDialog from './components/CodeGenerateDialog.vue'
import ProductFormDialog from './components/ProductFormDialog.vue'
import { componentsByName, filledAttributes, loadProductAttributeMeta, PRODUCT_THUMB_COLUMN } from './utils'

defineOptions({
  name: 'ProductDetailPage',
})

const BATCH_PAGE_SIZE = 40

const route = useRoute()
const router = useRouter()
const appAccountStore = useAppAccountStore()
const isAdmin = computed(() => appAccountStore.isAdmin)

const productId = computed(() => Number(route.params.id))
const loading = ref(false)
const loaded = ref(false)
const loadFailed = ref(false)
const product = ref<AnyRecord | null>(null)
const meta = ref<ProductAttributeMeta>({ columns: [], derived: [], imageColumns: [PRODUCT_THUMB_COLUMN] })
const codeBatches = ref<AnyRecord[]>([])

const components = computed(() => componentsByName(product.value))
const attributes = computed(() => filledAttributes(product.value, meta.value))
const canGenerate = computed(() => Boolean(product.value?.componentCount))
const hasCodes = computed(() => Number(product.value?.generatedCount || 0) > 0)
const zipUrl = computed(() => catalogApi.productQrcodesZipUrl(productId.value))

const summary = computed(() => {
  const data = product.value ?? {}
  return [
    { label: '产品部件', value: data.componentCount ?? 0, hint: '每套需要扫描的实物部件' },
    { label: '生成批次', value: data.generationBatchCount ?? 0, hint: '按每次生成任务归档' },
    { label: '已生成套数', value: data.generatedCount ?? 0, hint: '产品主码总数' },
    { label: '当前可生成', value: data.availableUnits ?? '未绑定', hint: '由最少可用部件批次决定' },
  ]
})

function scrollToTop() {
  document.documentElement.scrollTop = 0
}

async function load() {
  if (!Number.isInteger(productId.value) || productId.value <= 0) {
    loaded.value = true
    return
  }
  loading.value = true
  loadFailed.value = false
  try {
    const [list, attributeMeta] = await Promise.all([
      catalogApi.products(),
      loadProductAttributeMeta(),
    ])
    meta.value = attributeMeta
    product.value = list.find(item => Number(item.id) === productId.value) ?? null
    if (product.value && isAdmin.value) {
      codeBatches.value = await catalogApi.productCodeBatches(productId.value)
    }
  }
  catch (error) {
    loadFailed.value = product.value === null
    notifyError(error, '产品详情加载失败')
  }
  finally {
    loading.value = false
    loaded.value = true
  }
}

function backToList() {
  router.push({ name: 'products' })
}

// ---- generation batch QR view -------------------------------------------------

const batchPage = ref<ProductCodeBatchPage | null>(null)
const batchLoading = ref(false)
/** The generation batch being opened from the table (per-row loading state). */
const openingBatchId = ref<number | null>(null)
const batchSection = ref<HTMLElement>()

const batchMeta = computed(() => {
  const batch = batchPage.value?.batch
  return batch ? `${batch.productName || product.value?.name || ''} · ${batch.quantity} 套 · ${formatDate(batch.generatedAt)}` : ''
})
const batchSummary = computed(() => {
  const pagination = batchPage.value?.pagination
  return pagination ? `第 ${pagination.page} / ${pagination.totalPages} 页，共 ${pagination.total} 套` : ''
})

async function openProductBatch(batchId: number, page = 1, errorTitle = '批次二维码加载失败') {
  if (!isAdmin.value || batchLoading.value) {
    return
  }
  batchLoading.value = true
  openingBatchId.value = batchId
  try {
    batchPage.value = await catalogApi.productCodeBatch(batchId, page, BATCH_PAGE_SIZE)
    scrollToTop()
  }
  catch (error) {
    notifyError(error, errorTitle)
  }
  finally {
    batchLoading.value = false
    openingBatchId.value = null
  }
}

function changeBatchPage(page: number) {
  const batchId = Number(batchPage.value?.batch.id)
  if (batchId) {
    openProductBatch(batchId, page, '批次加载失败')
  }
}

function backToProduct() {
  batchPage.value = null
  scrollToTop()
  load()
}

function printCodeSet(codeSetId: number) {
  printElement(batchSection.value?.querySelector<HTMLElement>(`[data-code-set="${codeSetId}"]`))
}

// ---- edit / generate / delete ----------------------------------------------------

const formVisible = ref(false)
const codeVisible = ref(false)
const deleting = ref(false)

function openEdit() {
  if (product.value) {
    formVisible.value = true
  }
}

function openCodeModal() {
  if (product.value) {
    codeVisible.value = true
  }
}

function onProductSaved() {
  load()
}

async function onCodesGenerated(sets: AnyRecord[]) {
  await load()
  const batchId = Number(sets[0]?.generationBatchId)
  if (batchId) {
    await openProductBatch(batchId, 1)
  }
}

async function deleteProduct() {
  const current = product.value
  if (!current || deleting.value) {
    return
  }
  const confirmed = await confirmAction(
    `确定删除产品「${current.name}」吗？此操作不可恢复。仅未生成二维码、无生产记录的产品可删除。`,
    { title: '删除产品', confirmText: '删除', danger: true },
  )
  if (!confirmed) {
    return
  }
  deleting.value = true
  try {
    await catalogApi.deleteProduct(Number(current.id))
    notifySuccess('已删除', `产品「${current.name}」已删除`)
    backToList()
  }
  catch (error) {
    notifyError(error, '产品删除失败')
  }
  finally {
    deleting.value = false
  }
}

onMounted(async () => {
  await load()
  const requestedBatch = Number(route.query.batch)
  if (product.value && Number.isInteger(requestedBatch) && requestedBatch > 0) {
    await openProductBatch(requestedBatch, 1)
  }
})
</script>

<template>
  <div class="pts-page">
    <!-- 二维码生成批次：本批全部产品主码与部件码 -->
    <template v-if="batchPage">
      <FaPageHeader :description="batchMeta">
        <template #title>
          <span class="pts-step-label block">二维码生成批次</span>
          <span id="batch-detail-code" class="pts-code text-2xl">{{ batchPage.batch.batchCode }}</span>
        </template>
        <div class="flex flex-wrap gap-2">
          <ElButton id="batch-back" @click="backToProduct">
            <template #icon>
              <FaIcon name="i-ri:arrow-left-line" />
            </template>
            返回产品详情
          </ElButton>
          <ElButton id="download-batch-codes" type="primary" tag="a" :href="batchPage.batch.downloadUrl" target="_blank" rel="noopener">
            <template #icon>
              <FaIcon name="i-ri:download-2-line" />
            </template>
            下载本批全部二维码
          </ElButton>
        </div>
      </FaPageHeader>
      <FaPageMain>
        <section id="product-batch-detail" ref="batchSection" v-loading="batchLoading" aria-label="二维码生成批次">
          <div class="pts-toolbar">
            <span id="batch-pagination-summary" class="pts-muted text-sm">{{ batchSummary }}</span>
            <ElPagination
              layout="prev, pager, next"
              :current-page="batchPage.pagination.page"
              :page-size="batchPage.pagination.pageSize"
              :total="batchPage.pagination.total"
              :disabled="batchLoading"
              hide-on-single-page
              background
              @current-change="changeBatchPage"
            />
          </div>
          <div v-if="!batchPage.sets.length" class="pts-empty">
            该批次暂无二维码
          </div>
          <div id="batch-code-sets" class="flex flex-col gap-4">
            <article v-for="set in batchPage.sets" :key="set.id" class="pts-code-set" :data-code-set="set.id" :aria-label="`产品套码 ${set.setCode}`">
              <header class="pts-code-set-header">
                <div>
                  <strong class="pts-code text-sm">{{ set.setCode }}</strong>
                  <span class="pts-cell-sub">1 个产品主码 · {{ set.parts.length }} 个专属部件码</span>
                </div>
                <div class="pts-no-print flex gap-2">
                  <ElButton size="small" @click="printCodeSet(set.id)">
                    <template #icon>
                      <FaIcon name="i-ri:printer-line" />
                    </template>
                    打印
                  </ElButton>
                  <ElButton size="small" tag="a" :href="set.downloadUrl" target="_blank" rel="noopener">
                    <template #icon>
                      <FaIcon name="i-ri:download-2-line" />
                    </template>
                    下载全部
                  </ElButton>
                </div>
              </header>
              <div class="pts-label-grid mt-3">
                <div class="pts-label pts-label-main">
                  <img :src="set.machine.qrUrl" alt="产品主码">
                  <strong class="text-sm">产品主码</strong>
                  <span class="pts-cell-sub">{{ set.productName }}</span>
                  <code class="pts-label-code">{{ set.machine.identificationCode }}</code>
                </div>
                <div v-for="part in set.parts" :key="part.position" class="pts-label">
                  <img :src="part.qrUrl" :alt="`${part.partName}二维码`">
                  <strong class="text-sm">{{ part.position }}. {{ part.partName }}</strong>
                  <span class="pts-cell-sub">{{ part.supplierName }}</span>
                  <code class="pts-label-code">{{ part.identificationCode }}</code>
                </div>
              </div>
            </article>
          </div>
        </section>
      </FaPageMain>
    </template>

    <!-- 产品详情 -->
    <template v-else>
      <FaPageHeader :description="product?.productCode || (loaded ? '' : '正在加载产品详情…')">
        <template #title>
          <span class="pts-step-label block">产品详情</span>
          <span id="product-detail-name">{{ product?.name || '产品详情' }}</span>
        </template>
        <div class="flex flex-wrap gap-2">
          <ElButton id="product-back" @click="backToList">
            <template #icon>
              <FaIcon name="i-ri:arrow-left-line" />
            </template>
            返回产品列表
          </ElButton>
          <template v-if="product">
            <ElButton id="edit-product" @click="openEdit">
              <template #icon>
                <FaIcon name="i-ri:edit-line" />
              </template>
              编辑产品
            </ElButton>
            <template v-if="isAdmin">
              <ElButton id="generate-product-codes" type="primary" :disabled="!canGenerate" @click="openCodeModal">
                <template #icon>
                  <FaIcon name="i-ri:qr-code-line" />
                </template>
                生成二维码
              </ElButton>
              <!-- Without any code set the zip endpoint answers 404, so the link stays inert. -->
              <ElButton id="download-product-codes" tag="a" :href="hasCodes ? zipUrl : undefined" :disabled="!hasCodes" target="_blank" rel="noopener">
                <template #icon>
                  <FaIcon name="i-ri:download-2-line" />
                </template>
                下载全部二维码
              </ElButton>
            </template>
            <ElButton id="delete-product" type="danger" plain :loading="deleting" @click="deleteProduct">
              <template #icon>
                <FaIcon name="i-ri:delete-bin-line" />
              </template>
              删除产品
            </ElButton>
          </template>
        </div>
      </FaPageHeader>

      <div id="product-detail" v-loading="loading">
        <FaPageMain v-if="loaded && !loading && !product">
          <div v-if="loadFailed" class="pts-empty">
            <FaIcon name="i-ri:error-warning-line" class="text-3xl mb-2" />
            <div class="text-base font-medium">
              产品详情加载失败
            </div>
            <p class="mt-1 mb-3">
              请检查网络后重试。
            </p>
            <ElButton @click="load">
              <template #icon>
                <FaIcon name="i-ri:refresh-line" />
              </template>
              重新加载
            </ElButton>
          </div>
          <div v-else class="pts-empty">
            <FaIcon name="i-ri:box-3-line" class="text-3xl mb-2" />
            <div class="text-base font-medium">
              未找到该产品
            </div>
            <p class="mt-1 mb-0">
              产品可能已被删除，或你没有查看权限。
            </p>
          </div>
        </FaPageMain>

        <template v-if="product">
          <FaPageMain v-if="isAdmin">
            <div id="product-summary-strip" class="pts-metrics">
              <div v-for="item in summary" :key="item.label" class="pts-metric">
                <span class="pts-metric-label">{{ item.label }}</span>
                <span class="pts-metric-value">{{ item.value }}</span>
                <span class="pts-metric-hint">{{ item.hint }}</span>
              </div>
            </div>
          </FaPageMain>

          <FaPageMain>
            <template #title>
              <span class="text-foreground font-medium">产品资料</span>
              <span class="text-xs ml-2">已填写的产品档案字段，可在“编辑”中维护</span>
            </template>
            <div id="product-attribute-view">
              <div v-if="attributes.length" class="pts-attr-grid">
                <div v-for="item in attributes" :key="item.column" class="pts-attr-item">
                  <span class="pts-attr-label">{{ item.column }}</span>
                  <a v-if="item.image" class="pts-attr-image" :href="item.image" target="_blank" rel="noopener" :aria-label="`查看${item.column}大图`">
                    <img :src="item.image" :alt="item.column">
                  </a>
                  <strong v-else class="pts-attr-value">{{ item.value }}</strong>
                </div>
              </div>
              <div v-else class="pts-empty pts-empty-compact">
                尚未填写产品资料，点击“编辑”补充
              </div>
            </div>
          </FaPageMain>

          <template v-if="isAdmin">
            <FaPageMain>
              <template #title>
                <span class="text-foreground font-medium">部件与库存</span>
                <span class="text-xs ml-2">每套产品的用量及当前绑定供应批次</span>
              </template>
              <ElTable id="product-component-table" :data="components" empty-text="未配置部件，不能生成产品二维码" stripe>
                <ElTableColumn label="部件" min-width="180">
                  <template #default="{ row }">
                    <span class="pts-cell-main">{{ row.partName || row.slotName }}</span>
                    <span class="pts-cell-sub">{{ row.specification || row.partCode || '' }}</span>
                  </template>
                </ElTableColumn>
                <ElTableColumn label="供应商" prop="supplierName" min-width="140" show-overflow-tooltip />
                <ElTableColumn label="供应批次" min-width="160">
                  <template #default="{ row }">
                    <span class="pts-cell-main pts-code">{{ row.inventoryBatchNo || '未绑定' }}</span>
                    <span class="pts-cell-sub">{{ row.inventoryProductionDate || '-' }}</span>
                  </template>
                </ElTableColumn>
                <ElTableColumn label="每套用量" prop="quantity" width="100" />
                <ElTableColumn label="批次可用" width="110">
                  <template #default="{ row }">
                    <ElTag v-if="row.inventoryBatchId" :type="Number(row.inventoryQuantityAvailable || 0) > 0 ? 'success' : 'danger'" disable-transitions>
                      {{ Number(row.inventoryQuantityAvailable || 0) }}
                    </ElTag>
                    <ElTag v-else type="warning" disable-transitions>
                      需编辑
                    </ElTag>
                  </template>
                </ElTableColumn>
              </ElTable>
            </FaPageMain>

            <FaPageMain>
              <template #title>
                <span class="text-foreground font-medium">二维码生成批次</span>
                <span class="text-xs ml-2">点击批次查看该次生成的全部产品主码与部件码</span>
              </template>
              <ElTable id="product-batch-table" :data="codeBatches" row-key="id" empty-text="尚未生成二维码批次" stripe>
                <ElTableColumn label="生成批次" min-width="220">
                  <template #default="{ row }">
                    <span class="pts-cell-main pts-code">{{ row.batchCode }}</span>
                    <span class="pts-cell-sub">{{ row.prefix }}</span>
                  </template>
                </ElTableColumn>
                <ElTableColumn label="范围" width="130">
                  <template #default="{ row }">
                    {{ padSequence(row.startSequence) }}–{{ padSequence(row.endSequence) }}
                  </template>
                </ElTableColumn>
                <ElTableColumn label="套数" prop="quantity" width="80" />
                <ElTableColumn label="生成人" width="110">
                  <template #default="{ row }">
                    {{ row.generatedBy || '-' }}
                  </template>
                </ElTableColumn>
                <ElTableColumn label="生成时间" width="150">
                  <template #default="{ row }">
                    {{ formatDate(row.generatedAt) }}
                  </template>
                </ElTableColumn>
                <ElTableColumn label="操作" width="180" align="right" fixed="right">
                  <template #default="{ row }">
                    <ElButton link type="primary" tag="a" :href="row.downloadUrl" target="_blank" rel="noopener">
                      下载
                    </ElButton>
                    <ElButton link type="primary" :loading="openingBatchId === row.id" @click="openProductBatch(row.id)">
                      查看二维码
                    </ElButton>
                  </template>
                </ElTableColumn>
              </ElTable>
            </FaPageMain>
          </template>
        </template>
      </div>
    </template>

    <ProductFormDialog v-model="formVisible" :product="product" @saved="onProductSaved" />
    <CodeGenerateDialog v-if="isAdmin" v-model="codeVisible" :product="product" @generated="onCodesGenerated" />
  </div>
</template>
