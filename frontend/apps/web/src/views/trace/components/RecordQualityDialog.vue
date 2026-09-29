<script setup lang="ts">
/**
 * 处理质量状态（历史逐台记录）— single or bulk quality handling of legacy
 * per-unit records: 合格放行 / 暂扣处理（必须填写原因）/ 退回待检.
 *
 * One record goes through PUT /api/records/:id/status; a selection goes
 * through PUT /api/records/status/bulk (at most 200 records, one transaction
 * on the server). Used by 产品查询 and the 数据概览 queue.
 */
import type { RecordQualityStatus } from '@/api/modules/records'
import type { AnyRecord } from '@/api/types'
import recordsApi, { RECORD_STATUS_BULK_LIMIT } from '@/api/modules/records'
import { notifyError, notifySuccess } from '@/utils/feedback'
import { recordMachineLabel } from '../helpers'

defineOptions({
  name: 'TraceRecordQualityDialog',
})

const props = defineProps<{
  /** The records to handle; more than one means a bulk update. */
  records: AnyRecord[]
}>()

const emit = defineEmits<{
  done: []
}>()

const visible = defineModel<boolean>({ default: false })

const REASON_MAX_LENGTH = 200

const STATUS_NOTES: Record<RecordQualityStatus, { type: 'success' | 'warning' | 'error', text: string }> = {
  HOLD: { type: 'error', text: '暂扣会进入管理看板的待处理队列，必须填写原因。' },
  PASSED: { type: 'success', text: '合格后记录进入已放行状态，完整保留录入和处理审计。' },
  ASSEMBLED: { type: 'warning', text: '退回待检后，记录会重新进入待处理队列。' },
}

const status = ref<RecordQualityStatus>('PASSED')
const reason = ref('')
const reasonError = ref('')
const submitting = ref(false)

const bulk = computed(() => props.records.length > 1)
const title = computed(() => (bulk.value ? '批量处理质量状态' : '处理质量状态'))
const description = computed(() => {
  if (bulk.value) {
    return `已选择 ${props.records.length} 条记录；处理结果将同时应用`
  }
  return props.records[0] ? recordMachineLabel(props.records[0]) : ''
})
const note = computed(() => STATUS_NOTES[status.value])

// Same defaults as the previous UI: keep a shared 合格 / 暂扣 status, otherwise
// start from 合格放行; a single record keeps its current explanation.
watch(visible, (value) => {
  if (!value) {
    return
  }
  const records = props.records
  const first = records[0]
  const shared = records.every(item => item.status === first?.status)
  status.value = shared && (first?.status === 'PASSED' || first?.status === 'HOLD') ? first.status : 'PASSED'
  reason.value = records.length === 1 ? String(first?.statusReason || '') : ''
  reasonError.value = ''
})

async function submitRecordQuality() {
  if (submitting.value) {
    return
  }
  const recordIds = props.records
    .map(item => Number(item.id))
    .filter(id => Number.isInteger(id) && id > 0)
  if (!recordIds.length) {
    return
  }
  if (recordIds.length > RECORD_STATUS_BULK_LIMIT) {
    notifyError(`单次最多批量处理 ${RECORD_STATUS_BULK_LIMIT} 条质量记录`, '质量状态更新失败')
    return
  }
  const text = reason.value.trim()
  if (status.value === 'HOLD' && !text) {
    reasonError.value = '暂扣记录必须填写原因'
    return
  }
  if (text.length > REASON_MAX_LENGTH) {
    reasonError.value = `处理说明不能超过 ${REASON_MAX_LENGTH} 个字符`
    return
  }
  submitting.value = true
  try {
    const body = { status: status.value, reason: text }
    if (recordIds.length > 1) {
      await recordsApi.updateRecordStatusBulk({ ...body, recordIds })
    }
    else {
      await recordsApi.updateRecordStatus(recordIds[0], body)
    }
    visible.value = false
    notifySuccess(recordIds.length > 1 ? `已批量更新 ${recordIds.length} 条质量记录` : '质量状态已更新')
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
  <ElDialog id="record-quality-modal" v-model="visible" :title="title" width="500px" class="pts-dialog-fluid" :close-on-click-modal="false" append-to-body>
    <p id="record-quality-machine" class="pts-muted text-sm mt-0 mb-4">
      {{ description }}
    </p>
    <ElForm id="record-quality-form" label-position="top" @submit.prevent="submitRecordQuality">
      <ElFormItem label="处理结果">
        <ElRadioGroup v-model="status" name="status" @change="reasonError = ''">
          <ElRadioButton value="PASSED">
            合格放行
          </ElRadioButton>
          <ElRadioButton value="HOLD">
            暂扣处理
          </ElRadioButton>
          <ElRadioButton value="ASSEMBLED">
            退回待检
          </ElRadioButton>
        </ElRadioGroup>
      </ElFormItem>
      <ElFormItem label="处理说明" :error="reasonError" :required="status === 'HOLD'">
        <ElInput
          v-model="reason"
          name="reason"
          type="textarea"
          :rows="3"
          :maxlength="REASON_MAX_LENGTH"
          show-word-limit
          placeholder="暂扣时必须填写原因；合格时可填写检验依据"
          @input="reasonError = ''"
        />
      </ElFormItem>
      <ElAlert id="record-quality-note" :type="note.type" :closable="false" show-icon :title="note.text" />
    </ElForm>
    <template #footer>
      <ElButton @click="visible = false">
        取消
      </ElButton>
      <ElButton :type="status === 'HOLD' ? 'danger' : 'primary'" :loading="submitting" @click="submitRecordQuality">
        保存处理结果
      </ElButton>
    </template>
  </ElDialog>
</template>
