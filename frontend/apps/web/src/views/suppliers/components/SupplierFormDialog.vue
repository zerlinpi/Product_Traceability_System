<script setup lang="ts">
/**
 * 添加供应商 / 编辑供应商 (ADMIN). Base data only: parts, batches and
 * quantities are maintained on the supplier detail page.
 */
import type { FormInstance, FormRules } from 'element-plus'
import type { AnyRecord } from '@/api/types'
import catalogApi from '@/api/modules/catalog'
import { notifyError, notifySuccess } from '@/utils/feedback'
import { entityCodeRules } from '../utils'

defineOptions({
  name: 'SupplierFormDialog',
})

const props = defineProps<{
  /** The supplier to edit, or `null` to add one. */
  supplier: AnyRecord | null
}>()

const emit = defineEmits<{
  saved: [supplier: AnyRecord, created: boolean]
}>()

const visible = defineModel<boolean>({ default: false })

const formRef = ref<FormInstance>()
const codeInput = ref<{ focus: () => void }>()
const saving = ref(false)
const form = reactive({
  supplierCode: '',
  name: '',
  contact: '',
  phone: '',
})

const isEdit = computed(() => Boolean(props.supplier?.id))

const rules: FormRules = {
  supplierCode: entityCodeRules('供应商编码'),
  name: [
    { required: true, whitespace: true, message: '请输入供应商名称', trigger: 'blur' },
    { max: 100, message: '供应商名称不能超过 100 个字符', trigger: 'blur' },
  ],
  contact: [{ max: 50, message: '联系人不能超过 50 个字符', trigger: 'blur' }],
  phone: [{ max: 50, message: '联系电话不能超过 50 个字符', trigger: 'blur' }],
}

watch(visible, (value) => {
  if (!value) {
    return
  }
  const supplier = props.supplier
  form.supplierCode = supplier?.supplierCode ?? ''
  form.name = supplier?.name ?? ''
  form.contact = supplier?.contact ?? ''
  form.phone = supplier?.phone ?? ''
  nextTick(() => formRef.value?.clearValidate())
})

function focusCode() {
  codeInput.value?.focus()
}

async function submit() {
  if (!formRef.value || saving.value) {
    return
  }
  const valid = await formRef.value.validate().catch(() => false)
  if (!valid) {
    return
  }
  const supplierId = Number(props.supplier?.id || 0)
  const body = {
    supplierCode: form.supplierCode.trim(),
    name: form.name.trim(),
    contact: form.contact.trim(),
    phone: form.phone.trim(),
  }
  saving.value = true
  try {
    const saved = supplierId
      ? await catalogApi.updateSupplier(supplierId, body)
      : await catalogApi.createSupplier(body)
    visible.value = false
    notifySuccess(supplierId ? '供应商已更新' : '供应商已添加')
    emit('saved', saved, !supplierId)
  }
  catch (error) {
    notifyError(error, '供应商保存失败')
  }
  finally {
    saving.value = false
  }
}
</script>

<template>
  <ElDialog id="supplier-modal" v-model="visible" :title="isEdit ? '编辑供应商' : '添加供应商'" width="560px" :close-on-click-modal="false" append-to-body @opened="focusCode">
    <p class="pts-muted text-sm mt-0 mb-4">
      供应商基础信息不包含数量，部件和批次在详情页维护
    </p>
    <ElForm id="supplier-form" ref="formRef" :model="form" :rules="rules" label-position="top" @submit.prevent="submit">
      <div class="gap-x-4 grid grid-cols-1 sm:grid-cols-2">
        <ElFormItem label="供应商编码" prop="supplierCode">
          <ElInput ref="codeInput" v-model="form.supplierCode" name="supplierCode" maxlength="32" placeholder="例如：SUP-A01" />
        </ElFormItem>
        <ElFormItem label="供应商名称" prop="name">
          <ElInput v-model="form.name" name="name" maxlength="100" />
        </ElFormItem>
        <ElFormItem label="联系人" prop="contact">
          <ElInput v-model="form.contact" name="contact" maxlength="50" />
        </ElFormItem>
        <ElFormItem label="联系电话" prop="phone">
          <ElInput v-model="form.phone" name="phone" maxlength="50" />
        </ElFormItem>
      </div>
    </ElForm>
    <template #footer>
      <ElButton @click="visible = false">
        取消
      </ElButton>
      <ElButton type="primary" :loading="saving" @click="submit">
        保存供应商
      </ElButton>
    </template>
  </ElDialog>
</template>
