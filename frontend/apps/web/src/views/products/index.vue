<script setup lang="ts">
/**
 * 产品管理（管理员）/ 我的产品（运营）.
 *
 * ADMIN sees every product with its creator, BOM, stock state and how many
 * code sets can still be generated; a row opens the product detail.
 * OPERATIONS only see — and maintain — the simple products they registered
 * themselves (name, optional SKU, 产品资料); a row opens the edit form, as in
 * the previous UI. Operations never call the admin-only catalog endpoints.
 */
import type { AnyRecord } from '@/api/types'
import catalogApi from '@/api/modules/catalog'
import { confirmAction, notifyError, notifySuccess } from '@/utils/feedback'
import CodeGenerateDialog from './components/CodeGenerateDialog.vue'
import ProductFormDialog from './components/ProductFormDialog.vue'
import { componentLabel, componentsByName, productThumbUrl, STOCK_STATE, stockState } from './utils'

defineOptions({
  name: 'ProductListPage',
})

type StockFilter = 'ALL' | 'ACTIVE' | 'LOW' | 'OUT'

const router = useRouter()
const appAccountStore = useAppAccountStore()
const isAdmin = computed(() => appAccountStore.isAdmin)

const pageTitle = computed(() => (appAccountStore.isOperations ? '我的产品' : '产品管理'))
const pageDescription = computed(() => (appAccountStore.isOperations
  ? '添加并管理你自己的采购产品，仅你自己可见'
  : '添加产品、配置部件并生成成套二维码'))
const listHint = computed(() => (appAccountStore.isOperations
  ? '点击产品行编辑名称与产品资料'
  : '配置供应批次、查看可生成数量和二维码批次；点击产品行查看详情与二维码'))

const loading = ref(false)
const products = ref<AnyRecord[]>([])
const search = ref('')
const stockFilter = ref<StockFilter>('ALL')

const filtered = computed(() => {
  const query = search.value.trim().toLowerCase()
  // The stock filter is an admin tool (operations products have no BOM).
  const filter = isAdmin.value ? stockFilter.value : 'ALL'
  return products.value.filter((product) => {
    if (query && !`${product.name ?? ''} ${product.productCode ?? ''}`.toLowerCase().includes(query)) {
      return false
    }
    if (filter === 'ACTIVE') {
      return Boolean(product.active)
    }
    if (filter === 'LOW' || filter === 'OUT') {
      return stockState(product) === filter
    }
    return true
  })
})

const emptyHint = computed(() => (appAccountStore.isOperations
  ? '还没有产品，登记第一个产品后即可在采购订单中选择。'
  : '尚未添加产品，添加后可配置部件、批次与二维码。'))

function stockTag(product: AnyRecord) {
  return STOCK_STATE[stockState(product)]
}

function componentSummary(product: AnyRecord) {
  return componentsByName(product).slice(0, 3)
}

function extraComponentCount(product: AnyRecord) {
  return Math.max(0, componentsByName(product).length - 3)
}

async function load() {
  loading.value = true
  try {
    products.value = await catalogApi.products()
  }
  catch (error) {
    notifyError(error, '产品列表加载失败')
  }
  finally {
    loading.value = false
  }
}

// ---- navigation ------------------------------------------------------------

function openProductDetail(product: AnyRecord) {
  router.push({ name: 'product-detail', params: { id: product.id } })
}

/** Admin rows open the detail; operations have no manufacturing detail, so their rows open the edit form. */
function onRowClick(product: AnyRecord, _column: unknown, event: Event) {
  const target = event.target as HTMLElement | null
  if (target?.closest('button, a, input, label')) {
    return
  }
  if (isAdmin.value) {
    openProductDetail(product)
  }
  else {
    openProductForm(product)
  }
}

// ---- product form ------------------------------------------------------------

const formVisible = ref(false)
const editing = ref<AnyRecord | null>(null)

function openProductForm(product: AnyRecord | null = null) {
  editing.value = product
  formVisible.value = true
}

function onProductSaved() {
  load()
}

// ---- delete (operations maintain their own products from the list) ----------

const deletingId = ref<number | null>(null)

async function deleteProduct(product: AnyRecord) {
  const confirmed = await confirmAction(
    `确定删除产品「${product.name}」吗？此操作不可恢复。仅未生成二维码、无生产记录的产品可删除。`,
    { title: '删除产品', confirmText: '删除', danger: true },
  )
  if (!confirmed) {
    return
  }
  deletingId.value = Number(product.id)
  try {
    await catalogApi.deleteProduct(Number(product.id))
    notifySuccess('已删除', `产品「${product.name}」已删除`)
    await load()
  }
  catch (error) {
    notifyError(error, '产品删除失败')
  }
  finally {
    deletingId.value = null
  }
}

// ---- code generation (admin, products with a BOM) ---------------------------

const codeVisible = ref(false)
const codeProduct = ref<AnyRecord | null>(null)

function openCodeModal(product: AnyRecord) {
  codeProduct.value = product
  codeVisible.value = true
}

function onCodesGenerated(sets: AnyRecord[]) {
  const product = codeProduct.value
  if (!product) {
    return
  }
  const batchId = sets[0]?.generationBatchId
  // Open the new generation batch's QR codes on the product detail page.
  router.push({
    name: 'product-detail',
    params: { id: product.id },
    query: batchId ? { batch: String(batchId) } : {},
  })
}

onMounted(load)
</script>

<template>
  <div class="pts-page">
    <FaPageHeader :title="pageTitle" :description="pageDescription">
      <ElButton id="open-product-modal" type="primary" @click="openProductForm()">
        <template #icon>
          <FaIcon name="i-ri:add-line" />
        </template>
        添加新产品
      </ElButton>
    </FaPageHeader>
    <FaPageMain>
      <template #title>
        <span class="text-foreground font-medium">产品列表</span>
        <span id="product-list-hint" class="text-xs ml-2">{{ listHint }}</span>
      </template>
      <div class="pts-toolbar">
        <div class="pts-toolbar-filters">
          <ElInput id="product-search" v-model="search" placeholder="搜索产品名称或编码" clearable class="w-72" aria-label="搜索产品">
            <template #prefix>
              <FaIcon name="i-ri:search-line" />
            </template>
          </ElInput>
          <ElSelect v-if="isAdmin" id="product-stock-filter" v-model="stockFilter" class="w-36" aria-label="筛选产品状态">
            <ElOption label="全部状态" value="ALL" />
            <ElOption label="仅看启用" value="ACTIVE" />
            <ElOption label="库存紧张" value="LOW" />
            <ElOption label="无法生成" value="OUT" />
          </ElSelect>
        </div>
        <span id="product-result-count" class="pts-muted text-sm">显示 {{ filtered.length }} / {{ products.length }}</span>
      </div>
      <div id="product-grid">
        <ElTable v-loading="loading" :data="filtered" row-key="id" row-class-name="cursor-pointer" stripe @row-click="onRowClick">
          <template #empty>
            <div v-if="!loading" class="pts-empty">
              <FaIcon name="i-ri:box-3-line" class="text-3xl mb-2" />
              <template v-if="!products.length">
                <div class="text-base font-medium">
                  暂无产品
                </div>
                <p class="mt-1 mb-3">
                  {{ emptyHint }}
                </p>
                <ElButton type="primary" @click="openProductForm()">
                  添加新产品
                </ElButton>
              </template>
              <template v-else>
                <div class="text-base font-medium">
                  没有符合条件的产品
                </div>
                <p class="mt-1 mb-0">
                  试试调整搜索关键字或筛选条件。
                </p>
              </template>
            </div>
          </template>
          <ElTableColumn label="主图" width="76">
            <template #default="{ row }">
              <img v-if="productThumbUrl(row)" :src="productThumbUrl(row)" alt="" loading="lazy" class="pts-thumb">
              <span v-else class="pts-thumb pts-thumb-empty" aria-hidden="true">
                <FaIcon name="i-ri:image-line" />
              </span>
            </template>
          </ElTableColumn>
          <ElTableColumn label="产品" min-width="220">
            <template #default="{ row }">
              <ElButton link type="primary" class="pts-cell-link" :aria-label="`查看产品 ${row.name}`" @click="isAdmin ? openProductDetail(row) : openProductForm(row)">
                {{ row.name }}
              </ElButton>
              <span class="pts-cell-sub pts-code">{{ row.productCode }}</span>
              <span v-if="isAdmin && row.createdBy" class="pts-cell-sub">添加人：{{ row.createdBy }}</span>
            </template>
          </ElTableColumn>
          <ElTableColumn label="状态" width="170">
            <template #default="{ row }">
              <div class="flex flex-wrap gap-1">
                <ElTag :type="row.active ? 'success' : 'info'" disable-transitions>
                  {{ row.active ? '启用' : '停用' }}
                </ElTag>
                <ElTag v-if="isAdmin && row.componentCount" :type="stockTag(row).type" disable-transitions>
                  {{ stockTag(row).label }}
                </ElTag>
              </div>
            </template>
          </ElTableColumn>
          <ElTableColumn v-if="isAdmin" label="部件与供应批次" min-width="240">
            <template #default="{ row }">
              <template v-if="row.componentCount">
                <div v-for="item in componentSummary(row)" :key="`${item.partTypeId}-${item.inventoryBatchId}-${item.position}`" class="mb-1 last:mb-0">
                  <span class="pts-cell-main text-sm">{{ componentLabel(item) }}<template v-if="item.quantity > 1"> × {{ item.quantity }}</template></span>
                  <span class="pts-cell-sub">{{ item.supplierName }} · {{ item.inventoryBatchNo || '未绑定批次' }}</span>
                </div>
                <span v-if="extraComponentCount(row)" class="pts-cell-sub">另有 {{ extraComponentCount(row) }} 项部件</span>
              </template>
              <span v-else class="pts-cell-sub">采购产品（未配置部件）</span>
            </template>
          </ElTableColumn>
          <ElTableColumn v-if="isAdmin" label="生成批次" width="150">
            <template #default="{ row }">
              <span v-if="row.componentCount">{{ row.generationBatchCount }} 个生成批次 · {{ row.generatedCount }} 套</span>
              <span v-else class="pts-cell-sub">-</span>
            </template>
          </ElTableColumn>
          <ElTableColumn v-if="isAdmin" label="可生成" width="110" align="right">
            <template #default="{ row }">
              <template v-if="row.componentCount">
                <span class="text-base font-semibold">{{ row.availableUnits == null ? '未绑定' : row.availableUnits }}</span>
                <span v-if="row.availableUnits != null" class="pts-cell-sub">套可生成</span>
              </template>
              <span v-else class="pts-cell-sub">-</span>
            </template>
          </ElTableColumn>
          <ElTableColumn label="操作" :width="isAdmin ? 230 : 150" align="right" fixed="right">
            <template #default="{ row }">
              <template v-if="isAdmin">
                <ElButton link type="primary" @click="openProductDetail(row)">
                  查看详情
                </ElButton>
                <ElButton link type="primary" @click="openProductForm(row)">
                  编辑
                </ElButton>
                <ElButton v-if="row.componentCount" link type="primary" @click="openCodeModal(row)">
                  生成二维码
                </ElButton>
              </template>
              <template v-else>
                <ElButton link type="primary" @click="openProductForm(row)">
                  编辑
                </ElButton>
                <ElButton link type="danger" :loading="deletingId === row.id" @click="deleteProduct(row)">
                  删除
                </ElButton>
              </template>
            </template>
          </ElTableColumn>
        </ElTable>
      </div>
    </FaPageMain>

    <ProductFormDialog v-model="formVisible" :product="editing" @saved="onProductSaved" />
    <CodeGenerateDialog v-model="codeVisible" :product="codeProduct" @generated="onCodesGenerated" />
  </div>
</template>
