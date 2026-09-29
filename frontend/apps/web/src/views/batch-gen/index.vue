<script setup lang="ts">
/**
 * 批次生成（管理员、仓管）— one unique QR code per production batch; the
 * whole batch of treadmills shares it. Generation deducts supplier stock for
 * the whole batch in one transaction (idempotent scope
 * `production-batches.create`).
 */
import type { FormInstance, FormRules } from 'element-plus'
import type { AnyRecord } from '@/api/types'
import batchesApi from '@/api/modules/batches'
import catalogApi from '@/api/modules/catalog'
import { formatDate, qualityStatus } from '@/utils/format'
import { notifyError, notifySuccess } from '@/utils/feedback'

defineOptions({
  name: 'BatchGenPage',
})

const router = useRouter()

const loading = ref(false)
const batches = ref<AnyRecord[]>([])
const search = ref('')

const filtered = computed(() => {
  const query = search.value.trim().toLowerCase()
  if (!query) {
    return batches.value
  }
  return batches.value.filter(batch => `${batch.batchCode ?? ''} ${batch.productName ?? ''} ${batch.prefix ?? ''}`.toLowerCase().includes(query))
})

function isRegistered(batch: AnyRecord) {
  return Boolean(batch.registered ?? batch.registeredQuantity ?? batch.entry)
}

function qrUrl(batch: AnyRecord) {
  return batch.downloadUrl || batchesApi.productionBatchQrUrl(batch.id)
}

async function load() {
  loading.value = true
  try {
    batches.value = await batchesApi.productionBatches()
  }
  catch (error) {
    notifyError(error, '生产批次加载失败')
  }
  finally {
    loading.value = false
  }
}

// ---- generate dialog --------------------------------------------------------

const dialogVisible = ref(false)
const submitting = ref(false)
const products = ref<AnyRecord[]>([])
const formRef = ref<FormInstance>()
const form = reactive({
  productModelId: undefined as number | undefined,
  prefix: '',
  quantity: 1,
})
const rules: FormRules = {
  productModelId: [{ required: true, message: '请选择产品', trigger: 'change' }],
  prefix: [
    { required: true, message: '请填写批次前缀', trigger: 'blur' },
    { max: 24, message: '前缀最多 24 个字符', trigger: 'blur' },
  ],
  quantity: [{ required: true, message: '请填写计划台数', trigger: 'blur' }],
}

async function openGenerateDialog() {
  form.productModelId = undefined
  form.prefix = ''
  form.quantity = 1
  dialogVisible.value = true
  nextTick(() => formRef.value?.clearValidate())
  try {
    products.value = (await catalogApi.products()).filter(item => item.active)
  }
  catch (error) {
    notifyError(error, '产品列表加载失败')
  }
}

async function submitGenerate() {
  if (!formRef.value || submitting.value) {
    return
  }
  const valid = await formRef.value.validate().catch(() => false)
  if (!valid || form.productModelId === undefined) {
    return
  }
  submitting.value = true
  try {
    await batchesApi.createProductionBatch({
      productModelId: form.productModelId,
      prefix: form.prefix.trim(),
      quantity: Number(form.quantity),
    })
    dialogVisible.value = false
    notifySuccess('生产批次已生成', '已生成唯一批次二维码，并按整批用量扣减库存')
    await load()
  }
  catch (error) {
    notifyError(error, '批次生成失败')
  }
  finally {
    submitting.value = false
  }
}

function openDetail(batch: AnyRecord) {
  router.push({ name: 'batch-gen-detail', params: { id: batch.id } })
}

onMounted(load)
</script>

<template>
  <div class="pts-page">
    <FaPageHeader title="批次生成" description="为一个生产批次生成唯一批次二维码；一码代表整批走步机">
      <ElButton id="open-batch-generate-modal" type="primary" @click="openGenerateDialog">
        <template #icon>
          <FaIcon name="i-ri:add-line" />
        </template>
        生成生产批次
      </ElButton>
    </FaPageHeader>
    <FaPageMain>
      <div class="pts-toolbar">
        <div class="pts-toolbar-filters">
          <ElInput id="batch-gen-search" v-model="search" placeholder="搜索批次码或产品" clearable class="w-72" aria-label="搜索生产批次">
            <template #prefix>
              <FaIcon name="i-ri:search-line" />
            </template>
          </ElInput>
        </div>
        <span class="pts-muted text-sm">显示 {{ filtered.length }} / {{ batches.length }}</span>
      </div>
      <ElTable v-loading="loading" :data="filtered" row-key="id" empty-text="尚未生成生产批次" stripe>
        <ElTableColumn label="批次码" min-width="220">
          <template #default="{ row }">
            <span class="pts-code pts-cell-main">{{ row.batchCode }}</span>
          </template>
        </ElTableColumn>
        <ElTableColumn label="产品型号" prop="productName" min-width="160" show-overflow-tooltip />
        <ElTableColumn label="计划台数" prop="plannedQuantity" width="100" />
        <ElTableColumn label="前缀" prop="prefix" width="110" />
        <ElTableColumn label="登记状态" width="110">
          <template #default="{ row }">
            <ElTag v-if="isRegistered(row)" :type="qualityStatus(row.qualityStatus || 'ASSEMBLED').type" disable-transitions>
              {{ qualityStatus(row.qualityStatus || 'ASSEMBLED').label }}
            </ElTag>
            <ElTag v-else type="info" disable-transitions>
              未登记
            </ElTag>
          </template>
        </ElTableColumn>
        <ElTableColumn label="生成人" prop="generatedBy" width="110" />
        <ElTableColumn label="生成时间" width="150">
          <template #default="{ row }">
            {{ formatDate(row.generatedAt) }}
          </template>
        </ElTableColumn>
        <ElTableColumn label="操作" width="190" align="right" fixed="right">
          <template #default="{ row }">
            <ElButton link type="primary" tag="a" :href="qrUrl(row)" target="_blank" rel="noopener">
              下载二维码
            </ElButton>
            <ElButton link type="primary" @click="openDetail(row)">
              查看详情
            </ElButton>
          </template>
        </ElTableColumn>
      </ElTable>
    </FaPageMain>

    <ElDialog v-model="dialogVisible" title="生成生产批次" width="520px" :close-on-click-modal="false" append-to-body>
      <p class="pts-muted text-sm mb-4">
        选择产品与计划台数，系统将生成唯一批次二维码并按整批用量扣减库存
      </p>
      <ElForm id="batch-generate-form" ref="formRef" :model="form" :rules="rules" label-position="top" @submit.prevent="submitGenerate">
        <ElFormItem label="产品型号" prop="productModelId">
          <ElSelect v-model="form.productModelId" placeholder="请选择产品" filterable class="w-full">
            <ElOption v-for="product in products" :key="product.id" :label="product.name" :value="product.id" />
          </ElSelect>
        </ElFormItem>
        <ElRow :gutter="16">
          <ElCol :span="12">
            <ElFormItem label="自定义前缀" prop="prefix">
              <ElInput v-model="form.prefix" maxlength="24" placeholder="例如：TW04-A" />
            </ElFormItem>
          </ElCol>
          <ElCol :span="12">
            <ElFormItem label="计划台数" prop="quantity">
              <ElInputNumber v-model="form.quantity" :min="1" :max="999999" :step="1" step-strictly class="w-full!" />
            </ElFormItem>
          </ElCol>
        </ElRow>
        <ElAlert type="info" :closable="false" show-icon title="一个批次仅一个二维码，代表整批全部走步机" />
      </ElForm>
      <template #footer>
        <ElButton @click="dialogVisible = false">
          取消
        </ElButton>
        <ElButton type="primary" :loading="submitting" @click="submitGenerate">
          生成批次
        </ElButton>
      </template>
    </ElDialog>
  </div>
</template>
