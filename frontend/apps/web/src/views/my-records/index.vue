<script setup lang="ts">
/**
 * 批次登记记录（管理员、仓管）— the batch registration history
 * (batch_trace_records), with quality status. Replaces the retired per-unit
 * record list for treadmills.
 */
import type { AnyRecord } from '@/api/types'
import batchesApi from '@/api/modules/batches'
import { formatDate, qualityStatus } from '@/utils/format'
import { notifyError } from '@/utils/feedback'
import PtsListWindowNotice from '../../components/PtsListWindowNotice.vue'

defineOptions({
  name: 'MyRecordsPage',
})

const appAccountStore = useAppAccountStore()

const loading = ref(false)
const records = ref<AnyRecord[]>([])
const search = ref('')

const title = computed(() => appAccountStore.isAdmin ? '全部批次登记记录' : '批次登记记录')
const help = computed(() => appAccountStore.isAdmin
  ? '查看全部整批走步机的登记台数与质量状态'
  : '查看已授权产品的整批登记与质量状态')

const filtered = computed(() => {
  const query = search.value.trim().toLowerCase()
  if (!query) {
    return records.value
  }
  return records.value.filter(record => `${record.batchCode ?? ''} ${record.productName ?? ''}`.toLowerCase().includes(query))
})

async function load() {
  loading.value = true
  try {
    records.value = await batchesApi.batchTraceRecords()
  }
  catch (error) {
    notifyError(error, '批次登记记录加载失败')
  }
  finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="pts-page">
    <FaPageHeader :title="title" :description="help" />
    <FaPageMain>
      <PtsListWindowNotice url="/api/batch-trace-records" />
      <div class="pts-toolbar">
        <ElInput id="my-record-search" v-model="search" placeholder="搜索批次码或产品" clearable class="w-72" aria-label="搜索批次登记记录">
          <template #prefix>
            <FaIcon name="i-ri:search-line" />
          </template>
        </ElInput>
        <span class="pts-muted text-sm">显示 {{ filtered.length }} / {{ records.length }}</span>
      </div>
      <ElTable id="my-record-table" v-loading="loading" :data="filtered" row-key="id" empty-text="暂无批次登记记录" stripe>
        <ElTableColumn label="批次码" min-width="220">
          <template #default="{ row }">
            <span class="pts-code pts-cell-main">{{ row.batchCode || '-' }}</span>
          </template>
        </ElTableColumn>
        <ElTableColumn label="产品" min-width="180">
          <template #default="{ row }">
            <span class="pts-cell-main">{{ row.productName || '-' }}</span>
            <span class="pts-cell-sub pts-code">{{ row.productModelCode || '' }}</span>
          </template>
        </ElTableColumn>
        <ElTableColumn label="登记台数" prop="registeredQuantity" width="100" />
        <ElTableColumn label="质量状态" width="110">
          <template #default="{ row }">
            <ElTag :type="qualityStatus(row.qualityStatus || 'ASSEMBLED').type" disable-transitions>
              {{ qualityStatus(row.qualityStatus || 'ASSEMBLED').label }}
            </ElTag>
          </template>
        </ElTableColumn>
        <ElTableColumn label="操作人" prop="operatorName" width="110" />
        <ElTableColumn label="登记时间" width="150">
          <template #default="{ row }">
            {{ formatDate(row.registeredAt) }}
          </template>
        </ElTableColumn>
        <ElTableColumn label="生成时间" width="150">
          <template #default="{ row }">
            {{ formatDate(row.generatedAt) }}
          </template>
        </ElTableColumn>
      </ElTable>
    </FaPageMain>
  </div>
</template>
