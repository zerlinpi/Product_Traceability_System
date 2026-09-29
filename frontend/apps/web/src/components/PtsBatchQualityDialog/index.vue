<script setup lang="ts">
/**
 * 批次质量处理弹窗 — pass (合格放行) or hold (暂扣, reason required) a batch
 * registration that is still 待检. Used by 批次质量处理 and the dashboard queue.
 */
import type { AnyRecord } from '@/api/types'
import batchesApi from '@/api/modules/batches'
import { notifyError, notifySuccess } from '@/utils/feedback'

defineOptions({
  name: 'PtsBatchQualityDialog',
})

const props = defineProps<{
  record: AnyRecord | null
}>()

const emit = defineEmits<{
  done: []
}>()

const visible = defineModel<boolean>({ default: false })

const status = ref<'PASSED' | 'HOLD'>('PASSED')
const reason = ref('')
const submitting = ref(false)
const reasonError = ref('')

const description = computed(() => {
  const record = props.record
  if (!record) {
    return ''
  }
  return `${record.batchCode || ''} · ${record.productName || ''} · ${record.registeredQuantity ?? ''} 台`
})

watch(visible, (value) => {
  if (value) {
    status.value = 'PASSED'
    reason.value = ''
    reasonError.value = ''
  }
})

async function submit() {
  const record = props.record
  if (!record || submitting.value) {
    return
  }
  if (status.value === 'HOLD') {
    const text = reason.value.trim()
    if (!text || text.length > 500) {
      reasonError.value = '暂扣时必须填写 1 至 500 个字符的原因'
      return
    }
  }
  submitting.value = true
  try {
    if (status.value === 'HOLD') {
      await batchesApi.holdBatchRecord(Number(record.id), reason.value.trim())
    }
    else {
      await batchesApi.passBatchRecord(Number(record.id))
    }
    visible.value = false
    notifySuccess('批次质量状态已更新')
    emit('done')
  }
  catch (error) {
    notifyError(error, '质量状态更新失败')
  }
  finally {
    submitting.value = false
  }
}
</script>

<template>
  <ElDialog v-model="visible" title="处理批次质量状态" width="480px" :close-on-click-modal="false" append-to-body>
    <p class="pts-muted text-sm mt-0 mb-4">
      {{ description }}
    </p>
    <ElForm id="batch-quality-form" label-position="top" @submit.prevent="submit">
      <ElFormItem label="处理结果">
        <ElRadioGroup v-model="status">
          <ElRadioButton value="PASSED">
            合格放行
          </ElRadioButton>
          <ElRadioButton value="HOLD">
            暂扣处理
          </ElRadioButton>
        </ElRadioGroup>
      </ElFormItem>
      <ElFormItem v-if="status === 'HOLD'" label="暂扣原因" :error="reasonError" required>
        <ElInput v-model="reason" type="textarea" :rows="3" maxlength="500" show-word-limit placeholder="暂扣时必须填写原因（1 至 500 个字符）" @input="reasonError = ''" />
      </ElFormItem>
      <ElAlert
        :type="status === 'HOLD' ? 'warning' : 'success'"
        :closable="false"
        show-icon
        :title="status === 'HOLD' ? '暂扣的批次不能入库，需复核后闭环' : '合格放行将整批走步机标记为合格'"
      />
    </ElForm>
    <template #footer>
      <ElButton @click="visible = false">
        取消
      </ElButton>
      <ElButton :type="status === 'HOLD' ? 'danger' : 'primary'" :loading="submitting" @click="submit">
        保存处理结果
      </ElButton>
    </template>
  </ElDialog>
</template>
