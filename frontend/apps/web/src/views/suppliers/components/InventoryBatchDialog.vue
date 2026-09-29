<script setup lang="ts">
/**
 * 登记到货批次 / 编辑到货批次 (ADMIN).
 *
 * A new batch writes a RECEIPT movement. Editing keeps what was already
 * consumed: the received quantity may not drop below it, and a change of the
 * available stock is recorded as an ADJUSTMENT movement by the server. The
 * part of an existing batch cannot change.
 */
import type { FormInstance, FormRules } from 'element-plus'
import type { AnyRecord } from '@/api/types'
import catalogApi from '@/api/modules/catalog'
import { notifyError, notifySuccess } from '@/utils/feedback'
import { entityCodeRules, todayInputDate } from '../utils'

defineOptions({
  name: 'InventoryBatchDialog',
})

const props = defineProps<{
  /** The supplier's parts (supplier detail `parts`). */
  parts: AnyRecord[]
  /** The batch to edit, or `null` to register a new one. */
  batch: AnyRecord | null
  /** Part pre-selected when registering from a part row. */
  preferredPartId?: number | null
}>()

const emit = defineEmits<{
  saved: [batch: AnyRecord, created: boolean]
}>()

const visible = defineModel<boolean>({ default: false })

const MAX_QUANTITY = 10_000_000

const formRef = ref<FormInstance>()
const batchNoInput = ref<{ focus: () => void }>()
const saving = ref(false)
const form = reactive({
  partTypeId: undefined as number | undefined,
  batchNo: '',
  quantity: undefined as number | undefined,
  productionDate: '',
  receivedDate: '',
  remarks: '',
})

const isEdit = computed(() => Boolean(props.batch?.id))
const consumed = computed(() => Number(props.batch?.quantityConsumed || 0))
const help = computed(() => (isEdit.value
  ? `已领用 ${consumed.value}，到货数量不可低于该值`
  : '记录供应商部件的批次、数量和日期'))
/** Active parts, plus the batch's own part even if it was disabled since. */
const partOptions = computed(() => props.parts.filter(part => part.active || part.id === props.batch?.partTypeId))

function partLabel(part: AnyRecord) {
  return `${part.name}${part.specification ? ` · ${part.specification}` : ''}`
}

const rules: FormRules = {
  partTypeId: [{ required: true, message: '请选择供应部件', trigger: 'change' }],
  batchNo: entityCodeRules('供应批次号'),
  quantity: [
    { required: true, message: `到货数量需为 1-${MAX_QUANTITY}`, trigger: 'change' },
    {
      validator: (_rule, value, callback) => {
        if (isEdit.value && Number(value) < consumed.value) {
          callback(new Error(`到货数量不能小于已领用数量 ${consumed.value}`))
        }
        else {
          callback()
        }
      },
      trigger: 'change',
    },
  ],
  receivedDate: [{ required: true, message: '请输入到货日期', trigger: 'change' }],
  remarks: [{ max: 200, message: '备注不能超过 200 个字符', trigger: 'blur' }],
}

watch(visible, (value) => {
  if (!value) {
    return
  }
  const batch = props.batch
  const preferred = partOptions.value.find(part => part.id === props.preferredPartId)
  form.partTypeId = batch?.partTypeId ?? preferred?.id ?? partOptions.value[0]?.id
  form.batchNo = batch?.batchNo ?? ''
  form.quantity = batch ? Number(batch.quantityReceived) : undefined
  form.productionDate = batch?.productionDate ?? ''
  form.receivedDate = batch?.receivedDate || todayInputDate()
  form.remarks = batch?.remarks ?? ''
  nextTick(() => formRef.value?.clearValidate())
})

function focusBatchNo() {
  batchNoInput.value?.focus()
}

async function submit() {
  if (!formRef.value || saving.value) {
    return
  }
  const valid = await formRef.value.validate().catch(() => false)
  if (!valid) {
    return
  }
  const batchId = Number(props.batch?.id || 0)
  const body = {
    partTypeId: props.batch?.partTypeId || Number(form.partTypeId),
    batchNo: form.batchNo.trim(),
    quantity: Number(form.quantity),
    productionDate: form.productionDate || '',
    receivedDate: form.receivedDate || '',
    remarks: form.remarks.trim(),
  }
  saving.value = true
  try {
    const saved = batchId
      ? await catalogApi.updateInventoryBatch(batchId, body)
      : await catalogApi.createInventoryBatch(body)
    visible.value = false
    notifySuccess(batchId ? '供应批次已更新' : '到货批次已登记')
    emit('saved', saved, !batchId)
  }
  catch (error) {
    notifyError(error, '批次保存失败')
  }
  finally {
    saving.value = false
  }
}
</script>

<template>
  <ElDialog id="inventory-batch-modal" v-model="visible" :title="isEdit ? '编辑到货批次' : '登记到货批次'" width="560px" :close-on-click-modal="false" append-to-body @opened="focusBatchNo">
    <p id="inventory-batch-help" class="pts-muted text-sm mt-0 mb-4">
      {{ help }}
    </p>
    <ElForm id="inventory-batch-form" ref="formRef" :model="form" :rules="rules" label-position="top" @submit.prevent="submit">
      <ElFormItem label="供应部件" prop="partTypeId">
        <ElSelect v-model="form.partTypeId" name="partTypeId" placeholder="请选择供应部件" :disabled="isEdit" class="w-full">
          <ElOption v-for="part in partOptions" :key="part.id" :label="partLabel(part)" :value="part.id" />
        </ElSelect>
      </ElFormItem>
      <div class="gap-x-4 grid grid-cols-1 sm:grid-cols-2">
        <ElFormItem label="供应批次号" prop="batchNo">
          <ElInput ref="batchNoInput" v-model="form.batchNo" name="batchNo" maxlength="32" />
        </ElFormItem>
        <ElFormItem label="到货数量" prop="quantity">
          <ElInputNumber v-model="form.quantity" name="quantity" :min="1" :max="MAX_QUANTITY" :step="1" step-strictly controls-position="right" class="w-full!" />
        </ElFormItem>
        <ElFormItem label="生产日期" prop="productionDate">
          <ElDatePicker v-model="form.productionDate" name="productionDate" type="date" value-format="YYYY-MM-DD" placeholder="选择日期" class="w-full!" />
        </ElFormItem>
        <ElFormItem label="到货日期" prop="receivedDate">
          <ElDatePicker v-model="form.receivedDate" name="receivedDate" type="date" value-format="YYYY-MM-DD" placeholder="选择日期" :clearable="false" class="w-full!" />
        </ElFormItem>
      </div>
      <ElFormItem label="备注" prop="remarks">
        <ElInput v-model="form.remarks" name="remarks" type="textarea" :rows="3" maxlength="200" show-word-limit />
      </ElFormItem>
    </ElForm>
    <template #footer>
      <ElButton @click="visible = false">
        取消
      </ElButton>
      <ElButton type="primary" :loading="saving" @click="submit">
        保存批次
      </ElButton>
    </template>
  </ElDialog>
</template>
