<script setup lang="ts">
/**
 * 生成产品二维码 (legacy per-unit code sets, ADMIN only).
 *
 * Each set is one product main code plus one dedicated code per BOM slot; the
 * bound supplier batches are debited for the whole run in one transaction.
 * Treadmill products are batch-managed and the server rejects them (409).
 */
import type { FormInstance, FormRules } from 'element-plus'
import type { AnyRecord } from '@/api/types'
import catalogApi from '@/api/modules/catalog'
import { notifyError, notifySuccess } from '@/utils/feedback'
import { todayCode } from '../utils'

defineOptions({
  name: 'CodeGenerateDialog',
})

const props = defineProps<{
  product: AnyRecord | null
}>()

const emit = defineEmits<{
  generated: [sets: AnyRecord[]]
}>()

const visible = defineModel<boolean>({ default: false })

const formRef = ref<FormInstance>()
const prefixInput = ref<{ focus: () => void }>()
const submitting = ref(false)
const form = reactive({
  prefix: '',
  quantity: 1,
})

const rules: FormRules = {
  prefix: [
    { required: true, whitespace: true, message: '请输入自定义前缀', trigger: 'blur' },
    { pattern: /^\s*[A-Z0-9][\w-]{1,23}\s*$/i, message: '前缀需为 2-24 位字母、数字、下划线或短横线', trigger: 'blur' },
  ],
  quantity: [{ required: true, message: '单次可生成 1-1000 套', trigger: 'change' }],
}

const subtitle = computed(() => (props.product
  ? `${props.product.name} · ${props.product.componentCount ?? 0} 个部件`
  : ''))
const preview = computed(() => `${form.prefix.trim().toUpperCase() || '前缀'}-${todayCode()}-0001`)

watch(visible, (value) => {
  if (value) {
    form.prefix = ''
    form.quantity = 1
    nextTick(() => formRef.value?.clearValidate())
  }
})

function focusPrefix() {
  prefixInput.value?.focus()
}

async function submit() {
  if (!formRef.value || !props.product || submitting.value) {
    return
  }
  const valid = await formRef.value.validate().catch(() => false)
  if (!valid) {
    return
  }
  submitting.value = true
  try {
    const sets = await catalogApi.createCodeSets(Number(props.product.id), {
      prefix: form.prefix.trim(),
      quantity: Number(form.quantity),
    })
    visible.value = false
    notifySuccess('二维码已生成', `共生成 ${sets.length} 套，每套主码和部件码一一对应`)
    emit('generated', sets)
  }
  catch (error) {
    notifyError(error, '二维码生成失败')
  }
  finally {
    submitting.value = false
  }
}
</script>

<template>
  <ElDialog id="code-modal" v-model="visible" title="生成产品二维码" width="520px" :close-on-click-modal="false" append-to-body @opened="focusPrefix">
    <p id="code-product-name" class="pts-muted text-sm mt-0 mb-4">
      {{ subtitle }}
    </p>
    <ElForm id="code-form" ref="formRef" :model="form" :rules="rules" label-position="top" @submit.prevent="submit">
      <ElFormItem label="自定义前缀" prop="prefix">
        <ElInput ref="prefixInput" v-model="form.prefix" name="prefix" maxlength="24" placeholder="例如：TABLE-A" />
      </ElFormItem>
      <ElFormItem label="生成套数" prop="quantity">
        <ElInputNumber v-model="form.quantity" name="quantity" :min="1" :max="1000" :step="1" step-strictly controls-position="right" class="w-full!" />
        <span class="pts-muted text-xs mt-1 block">单次可生成 1-1000 套，每套含主码与部件码</span>
      </ElFormItem>
      <div class="pts-code-rule">
        <span>系统自动补全</span>
        <strong id="code-preview">{{ preview }}</strong>
      </div>
    </ElForm>
    <template #footer>
      <ElButton @click="visible = false">
        取消
      </ElButton>
      <ElButton type="primary" :loading="submitting" @click="submit">
        <template #icon>
          <FaIcon name="i-ri:qr-code-line" />
        </template>
        生成二维码
      </ElButton>
    </template>
  </ElDialog>
</template>
