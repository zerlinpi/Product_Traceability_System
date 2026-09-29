<script setup lang="ts">
/**
 * 删除录入记录 — delete a legacy per-unit record with a required reason
 * (DELETE /api/records/:id). The main code and part codes can be scanned
 * again afterwards; the deletion is written to the audit log.
 */
import type { AnyRecord } from '@/api/types'
import recordsApi from '@/api/modules/records'
import { notifyError, notifySuccess } from '@/utils/feedback'
import { recordMachineLabel } from '../helpers'

defineOptions({
  name: 'TraceRecordDeleteDialog',
})

const props = defineProps<{
  record: AnyRecord | null
}>()

const emit = defineEmits<{
  done: []
}>()

const visible = defineModel<boolean>({ default: false })

const REASON_MAX_LENGTH = 200

const reason = ref('')
const reasonError = ref('')
const submitting = ref(false)
const reasonInput = ref<{ focus: () => void }>()

const machineLabel = computed(() => (props.record ? recordMachineLabel(props.record) : ''))

watch(visible, (value) => {
  if (value) {
    reason.value = ''
    reasonError.value = ''
  }
})

function onOpened() {
  reasonInput.value?.focus()
}

async function submitRecordDelete() {
  const record = props.record
  if (!record || submitting.value) {
    return
  }
  const text = reason.value.trim()
  if (!text) {
    reasonError.value = '请输入删除原因'
    reasonInput.value?.focus()
    return
  }
  if (text.length > REASON_MAX_LENGTH) {
    reasonError.value = `删除原因不能超过 ${REASON_MAX_LENGTH} 个字符`
    return
  }
  submitting.value = true
  try {
    await recordsApi.deleteRecord(Number(record.id), text)
    visible.value = false
    notifySuccess('录入记录已删除', '该套二维码可以重新录入')
    emit('done')
  }
  catch (error) {
    notifyError(error, '记录删除失败')
  }
  finally {
    submitting.value = false
  }
}
</script>

<template>
  <ElDialog
    id="record-delete-modal"
    v-model="visible"
    title="删除录入记录"
    width="480px"
    class="pts-dialog-fluid"
    :close-on-click-modal="false"
    append-to-body
    @opened="onOpened"
  >
    <p id="record-delete-machine" class="pts-muted text-sm mt-0 mb-4">
      {{ machineLabel }}
    </p>
    <ElForm id="record-delete-form" label-position="top" @submit.prevent="submitRecordDelete">
      <ElFormItem label="删除原因" :error="reasonError" required>
        <ElInput
          ref="reasonInput"
          v-model="reason"
          name="reason"
          type="textarea"
          :rows="3"
          :maxlength="REASON_MAX_LENGTH"
          show-word-limit
          placeholder="例如：扫错产品，需要重新录入"
          @input="reasonError = ''"
        />
      </ElFormItem>
      <ElAlert type="warning" :closable="false" show-icon title="删除后主码和部件码可以重新扫码，操作会写入审计记录。" />
    </ElForm>
    <template #footer>
      <ElButton @click="visible = false">
        取消
      </ElButton>
      <ElButton type="danger" :loading="submitting" @click="submitRecordDelete">
        确认删除
      </ElButton>
    </template>
  </ElDialog>
</template>
