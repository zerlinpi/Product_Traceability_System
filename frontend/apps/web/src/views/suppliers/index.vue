<script setup lang="ts">
/**
 * 供应商（管理员）— suppliers with their part / batch counts and the stock
 * still available across all their batches. A row opens the supplier detail,
 * where parts, inventory batches and product usage are maintained.
 */
import type { AnyRecord } from '@/api/types'
import catalogApi from '@/api/modules/catalog'
import { notifyError } from '@/utils/feedback'
import { formatNumber } from '@/utils/format'
import SupplierFormDialog from './components/SupplierFormDialog.vue'

defineOptions({
  name: 'SupplierListPage',
})

type StockFilter = 'ALL' | 'AVAILABLE' | 'OUT'

const router = useRouter()

const loading = ref(false)
const suppliers = ref<AnyRecord[]>([])
const search = ref('')
const stockFilter = ref<StockFilter>('ALL')

const filtered = computed(() => {
  const query = search.value.trim().toLowerCase()
  return suppliers.value.filter((supplier) => {
    if (query && !`${supplier.name ?? ''} ${supplier.supplierCode ?? ''} ${supplier.contact || ''}`.toLowerCase().includes(query)) {
      return false
    }
    if (stockFilter.value === 'AVAILABLE') {
      return Number(supplier.quantityAvailable) > 0
    }
    if (stockFilter.value === 'OUT') {
      return Number(supplier.quantityAvailable) <= 0
    }
    return true
  })
})

async function load() {
  loading.value = true
  try {
    suppliers.value = await catalogApi.suppliers()
  }
  catch (error) {
    notifyError(error, '供应商列表加载失败')
  }
  finally {
    loading.value = false
  }
}

function openSupplierDetail(supplier: AnyRecord) {
  router.push({ name: 'supplier-detail', params: { id: supplier.id } })
}

function onRowClick(supplier: AnyRecord, _column: unknown, event: Event) {
  const target = event.target as HTMLElement | null
  if (!target?.closest('button, a')) {
    openSupplierDetail(supplier)
  }
}

const formVisible = ref(false)

function openSupplierModal() {
  formVisible.value = true
}

onMounted(load)
</script>

<template>
  <div class="pts-page">
    <FaPageHeader title="供应商" description="管理供应部件、到货批次与可用库存">
      <ElButton id="open-supplier-modal" type="primary" @click="openSupplierModal">
        <template #icon>
          <FaIcon name="i-ri:add-line" />
        </template>
        添加供应商
      </ElButton>
    </FaPageHeader>
    <FaPageMain>
      <template #title>
        <span class="text-foreground font-medium">供应商列表</span>
        <span class="text-xs ml-2">按供应商管理部件、批次、到货数量和可用库存</span>
      </template>
      <div class="pts-toolbar">
        <div class="pts-toolbar-filters">
          <ElInput id="supplier-search" v-model="search" placeholder="搜索供应商名称、编码或联系人" clearable class="w-72" aria-label="搜索供应商">
            <template #prefix>
              <FaIcon name="i-ri:search-line" />
            </template>
          </ElInput>
          <ElSelect id="supplier-stock-filter" v-model="stockFilter" class="w-36" aria-label="筛选供应商库存">
            <ElOption label="全部库存" value="ALL" />
            <ElOption label="有可用库存" value="AVAILABLE" />
            <ElOption label="库存为零" value="OUT" />
          </ElSelect>
        </div>
        <span id="supplier-result-count" class="pts-muted text-sm">显示 {{ filtered.length }} / {{ suppliers.length }}</span>
      </div>
      <div id="supplier-grid">
        <ElTable v-loading="loading" :data="filtered" row-key="id" row-class-name="cursor-pointer" stripe @row-click="onRowClick">
          <template #empty>
            <div v-if="!loading" class="pts-empty">
              <FaIcon name="i-ri:building-2-line" class="text-3xl mb-2" />
              <template v-if="!suppliers.length">
                <div class="text-base font-medium">
                  暂无供应商
                </div>
                <p class="mt-1 mb-3">
                  添加供应商后即可登记部件、批次与到货数量。
                </p>
                <ElButton type="primary" @click="openSupplierModal">
                  添加供应商
                </ElButton>
              </template>
              <template v-else>
                <div class="text-base font-medium">
                  没有符合条件的供应商
                </div>
                <p class="mt-1 mb-0">
                  试试调整搜索关键字或筛选条件。
                </p>
              </template>
            </div>
          </template>
          <ElTableColumn label="供应商" min-width="220">
            <template #default="{ row }">
              <ElButton link type="primary" class="pts-cell-link" :aria-label="`查看供应商 ${row.name} 详情`" @click="openSupplierDetail(row)">
                {{ row.name }}
              </ElButton>
              <span class="pts-cell-sub pts-code">{{ row.supplierCode }}</span>
            </template>
          </ElTableColumn>
          <ElTableColumn label="状态" width="90">
            <template #default="{ row }">
              <ElTag :type="row.active ? 'success' : 'info'" disable-transitions>
                {{ row.active ? '启用' : '停用' }}
              </ElTag>
            </template>
          </ElTableColumn>
          <ElTableColumn label="供应部件" prop="partCount" width="100" align="right" />
          <ElTableColumn label="到货批次" prop="batchCount" width="100" align="right" />
          <ElTableColumn label="可用库存" width="110" align="right">
            <template #default="{ row }">
              <span class="font-semibold" :class="{ 'pts-text-danger': Number(row.quantityAvailable) <= 0 }">{{ formatNumber(row.quantityAvailable) || 0 }}</span>
            </template>
          </ElTableColumn>
          <ElTableColumn label="联系人" min-width="180">
            <template #default="{ row }">
              <span :class="row.contact ? 'pts-cell-main' : 'pts-cell-sub'">{{ row.contact || '未填写联系人' }}</span>
              <span v-if="row.phone" class="pts-cell-sub">{{ row.phone }}</span>
            </template>
          </ElTableColumn>
          <ElTableColumn label="操作" width="110" align="right" fixed="right">
            <template #default="{ row }">
              <ElButton link type="primary" @click="openSupplierDetail(row)">
                查看详情
              </ElButton>
            </template>
          </ElTableColumn>
        </ElTable>
      </div>
    </FaPageMain>

    <SupplierFormDialog v-model="formVisible" :supplier="null" @saved="load" />
  </div>
</template>
