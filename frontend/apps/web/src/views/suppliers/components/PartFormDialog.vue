<script setup lang="ts">
/**
 * 添加 / 编辑供应部件 (ADMIN). The safety stock drives the low-stock alerts:
 * a part whose available stock is at or below it is flagged 低于安全库存.
 */
import type { FormInstance, FormRules } from 'element-plus'
import type { AnyRecord } from '@/api/types'
import catalogApi from '@/api/modules/catalog'
import { notifyError, notifySuccess } from '@/utils/feedback'
import { entityCodeRules } from '../utils'

defineOptions({
  name: 'PartFormDialog',
})

const props = defineProps<{
  supplier: AnyRecord | null
  /** The part to edit, or `null` to add one to `supplier`. */
  part: AnyRecord | null
}>()

const emit = defineEmits<{
  saved: [part: AnyRecord, created: boolean]
}>()

const visible = defineModel<boolean>({ default: false })

const MAX_MINIMUM_STOCK = 10_000_000

const formRef = ref<FormInstance>()
const nameInput = ref<{ focus: () => void }>()
const saving = ref(false)
const form = reactive({
  name: '',
  partCode: '',
  specification: '',
  minimumStock: 0 as number | undefined,
})

const isEdit = computed(() => Boolean(props.part?.id))

const rules: FormRules = {
  name: [
    { required: true, whitespace: true, message: '请输入部件名称', trigger: 'blur' },
    { max: 100, message: '部件名称不能超过 100 个字符', trigger: 'blur' },
  ],
  partCode: entityCodeRules('部件编码'),
  specification: [{ max: 150, message: '规格型号不能超过 150 个字符', trigger: 'blur' }],
  minimumStock: [{ required: true, message: `安全库存需在 0-${MAX_MINIMUM_STOCK} 之间`, trigger: 'change' }],
}

watch(visible, (value) => {
  if (!value) {
    return
  }
  const part = props.part
  form.name = part?.name ?? ''
  form.partCode = part?.partCode ?? ''
  form.specification = part?.specification ?? ''
  form.minimumStock = Number(part?.minimumStock ?? 0)
  nextTick(() => formRef.value?.clearValidate())
})

function focusName() {
  nameInput.value?.focus()
}

async function submit() {
  const supplierId = Number(props.supplier?.id || 0)
  if (!formRef.value || !supplierId || saving.value) {
    return
  }
  const valid = await formRef.value.validate().catch(() => false)
  if (!valid) {
    return
  }
  const partId = Number(props.part?.id || 0)
  const body = {
    partCode: form.partCode.trim(),
    name: form.name.trim(),
    specification: form.specification.trim(),
    minimumStock: Number(form.minimumStock),
    supplierId,
  }
  saving.value = true
  try {
    const saved = partId
      ? await catalogApi.updatePartType(partId, body)
      : await catalogApi.createPartType(body)
    visible.value = false
    notifySuccess(partId ? '供应部件已更新' : '供应部件已添加')
    emit('saved', saved, !partId)
  }
  catch (error) {
    notifyError(error, '部件保存失败')
  }
  finally {
    saving.value = false
  }
}
</script>

<template>
  <ElDialog id="part-modal" v-model="visible" :title="isEdit ? '编辑供应部件' : '添加供应部件'" width="560px" :close-on-click-modal="false" append-to-body @opened="focusName">
    <p id="part-modal-help" class="pts-muted text-sm mt-0 mb-4">
      {{ props.supplier?.name || '' }}
    </p>
    <ElForm id="part-form" ref="formRef" :model="form" :rules="rules" label-position="top" @submit.prevent="submit">
      <ElFormItem label="部件名称" prop="name">
        <ElInput ref="nameInput" v-model="form.name" name="name" maxlength="100" />
      </ElFormItem>
      <div class="gap-x-4 grid grid-cols-1 sm:grid-cols-2">
        <ElFormItem label="部件编码" prop="partCode">
          <ElInput v-model="form.partCode" name="partCode" maxlength="32" placeholder="例如：MOTOR-A01" />
        </ElFormItem>
        <ElFormItem label="规格型号" prop="specification">
          <ElInput v-model="form.specification" name="specification" maxlength="150" placeholder="例如：24V / 80W" />
        </ElFormItem>
        <ElFormItem label="安全库存" prop="minimumStock">
          <ElInputNumber v-model="form.minimumStock" name="minimumStock" :min="0" :max="MAX_MINIMUM_STOCK" :step="1" step-strictly controls-position="right" class="w-full!" />
          <span class="pts-muted text-xs mt-1 block">可用库存不高于此值时进入预警；填 0 仅提示缺货</span>
        </ElFormItem>
      </div>
    </ElForm>
    <template #footer>
      <ElButton @click="visible = false">
        取消
      </ElButton>
      <ElButton type="primary" :loading="saving" @click="submit">
        保存部件
      </ElButton>
    </template>
  </ElDialog>
</template>
