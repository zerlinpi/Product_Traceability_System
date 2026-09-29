<script setup lang="ts">
/**
 * 修改录入记录 — correct the part codes and remarks of a legacy per-unit
 * record (PUT /api/records/:id; the record returns to 待检).
 *
 * Scan-gun behaviour, kept from the previous UI:
 * - clicking or focusing a part-code box makes it the scan target
 *   (highlighted) and selects its text;
 * - characters typed while focus is elsewhere in the dialog (not another
 *   editable field or a button) go into the current target, the first key
 *   replacing the old value;
 * - Enter trims the value and moves focus to the next part-code box (its text
 *   selected) instead of submitting the form.
 * `handleRecordScannerKeydown` is attached to `document` in the capture phase
 * only while the dialog is open.
 *
 * The part-code boxes are native inputs (with Element Plus input styling) so
 * the scan logic reads and writes exactly the element the operator sees.
 */
import type { AnyRecord } from '@/api/types'
import recordsApi from '@/api/modules/records'
import { isEditableElement } from '@/composables/scanner'
import { notifyError, notifySuccess } from '@/utils/feedback'
import { recordMachineLabel } from '../helpers'

defineOptions({
  name: 'TraceRecordEditDialog',
})

const props = defineProps<{
  record: AnyRecord | null
}>()

const emit = defineEmits<{
  done: []
}>()

const visible = defineModel<boolean>({ default: false })

interface PartField {
  key: string
  label: string
  value: string
}

const PART_CODE_MAX_LENGTH = 120
const REMARKS_MAX_LENGTH = 200

const editorRef = ref<HTMLElement>()
const parts = ref<PartField[]>([])
const remarks = ref('')
const submitting = ref(false)
/** Index of the part-code box that receives scanned characters. */
const scanTargetIndex = ref(-1)
/** Index of the part-code box that failed validation. */
const errorIndex = ref(-1)
/** The next redirected key replaces the target's value instead of appending. */
let scanReplace = true

const machineLabel = computed(() => (props.record ? recordMachineLabel(props.record) : ''))

function scanInputs(): HTMLInputElement[] {
  return Array.from(editorRef.value?.querySelectorAll<HTMLInputElement>('input.record-scan-input') ?? [])
}

function setRecordScanTarget(input: HTMLInputElement, select = false) {
  scanTargetIndex.value = scanInputs().indexOf(input)
  scanReplace = true
  if (select) {
    input.select()
  }
}

function onScanInputFocus(event: Event) {
  if (event.currentTarget instanceof HTMLInputElement) {
    setRecordScanTarget(event.currentTarget, true)
  }
}

function clearPartError(index: number) {
  if (errorIndex.value === index && parts.value[index]?.value.trim()) {
    errorIndex.value = -1
  }
}

/** Write through the DOM and let `v-model` pick the value up. */
function writeScanValue(input: HTMLInputElement, value: string) {
  input.value = value
  input.dispatchEvent(new Event('input', { bubbles: true }))
}

function handleRecordScannerKeydown(event: KeyboardEvent): boolean {
  if (!visible.value) {
    return false
  }
  if (event.ctrlKey || event.altKey || event.metaKey || event.isComposing) {
    return false
  }
  const inputs = scanInputs()
  const element = event.target instanceof HTMLElement ? event.target : null
  const eventTarget = element instanceof HTMLInputElement && element.classList.contains('record-scan-input') ? element : null
  const target = eventTarget ?? inputs[scanTargetIndex.value] ?? null
  if (!target || !inputs.includes(target)) {
    return false
  }
  // Never steal keys from another field (校对备注) or from a focused button.
  const interactive = isEditableElement(element) || Boolean(element?.closest('button, a[href], [role="button"]'))
  if (interactive && element !== target) {
    return false
  }
  if (event.key === 'Enter') {
    event.preventDefault()
    writeScanValue(target, target.value.trim())
    const next = inputs[inputs.indexOf(target) + 1] ?? target
    next.focus({ preventScroll: true })
    setRecordScanTarget(next, true)
    return true
  }
  if (event.key.length === 1 && element !== target) {
    event.preventDefault()
    writeScanValue(target, scanReplace ? event.key : `${target.value}${event.key}`)
    target.focus({ preventScroll: true })
    target.setSelectionRange(target.value.length, target.value.length)
    scanReplace = false
  }
  else if (event.key.length === 1) {
    scanReplace = false
  }
  return true
}

let listening = false

function installScanner() {
  if (!listening) {
    document.addEventListener('keydown', handleRecordScannerKeydown, true)
    listening = true
  }
}

function uninstallScanner() {
  if (listening) {
    document.removeEventListener('keydown', handleRecordScannerKeydown, true)
    listening = false
  }
}

watch(visible, async (value) => {
  if (!value) {
    uninstallScanner()
    scanTargetIndex.value = -1
    return
  }
  const record = props.record
  const recordParts: AnyRecord[] = Array.isArray(record?.parts) ? record.parts : []
  parts.value = recordParts.map((part, index) => ({
    key: `${record?.id ?? 'record'}-${part.position ?? index + 1}-${index}`,
    label: `${part.position ?? index + 1}. ${part.partName || ''}`,
    value: String(part.identificationCode || ''),
  }))
  remarks.value = String(record?.remarks || '')
  errorIndex.value = -1
  scanTargetIndex.value = -1
  installScanner()
  await nextTick()
  // Route keys to the first box straight away, even before the dialog has
  // finished opening and taken focus.
  const first = scanInputs()[0]
  if (first) {
    setRecordScanTarget(first)
  }
})

function onOpened() {
  const inputs = scanInputs()
  const active = document.activeElement
  // The operator may already be scanning into a box; keep their progress.
  if (active instanceof HTMLInputElement && inputs.includes(active)) {
    return
  }
  const target = inputs[scanTargetIndex.value] ?? inputs[0]
  if (target) {
    target.focus({ preventScroll: true })
    setRecordScanTarget(target, true)
  }
}

onBeforeUnmount(uninstallScanner)

async function submitRecordEdit() {
  const record = props.record
  if (!record || submitting.value) {
    return
  }
  const partCodes = parts.value.map(part => part.value.trim())
  const missing = partCodes.findIndex(code => !code)
  if (missing >= 0) {
    errorIndex.value = missing
    const input = scanInputs()[missing]
    if (input) {
      input.focus({ preventScroll: true })
      setRecordScanTarget(input, true)
    }
    return
  }
  errorIndex.value = -1
  submitting.value = true
  try {
    await recordsApi.updateRecord(Number(record.id), { partCodes, remarks: remarks.value.trim() })
    visible.value = false
    notifySuccess('录入记录已修改')
    emit('done')
  }
  catch (error) {
    notifyError(error, '记录修改失败')
  }
  finally {
    submitting.value = false
  }
}
</script>

<template>
  <ElDialog
    id="record-edit-modal"
    v-model="visible"
    title="修改录入记录"
    width="560px"
    class="pts-dialog-fluid"
    :close-on-click-modal="false"
    append-to-body
    @opened="onOpened"
  >
    <p id="record-edit-machine" class="pts-muted text-sm mt-0 mb-3">
      {{ machineLabel }}
    </p>
    <div class="pts-scan-edit-hint">
      <strong>扫码修改</strong>
      <span>先点击要修改的部件输入框，再扫码；回车后自动移到下一个部件。</span>
    </div>
    <ElForm id="record-edit-form" label-position="top" @submit.prevent="submitRecordEdit">
      <div id="record-part-editor" ref="editorRef" class="pts-record-part-editor">
        <div v-for="(part, index) in parts" :key="part.key" class="pts-record-part-field">
          <label class="pts-record-part-label" :for="`record-part-code-${index}`">{{ part.label }}</label>
          <div class="el-input w-full" :class="{ 'pts-scan-target': scanTargetIndex === index, 'pts-input-error': errorIndex === index }">
            <div class="el-input__wrapper">
              <input
                :id="`record-part-code-${index}`"
                v-model="part.value"
                class="el-input__inner record-scan-input"
                :class="{ 'scan-target-active': scanTargetIndex === index }"
                name="partCode"
                type="text"
                required
                :maxlength="PART_CODE_MAX_LENGTH"
                autocomplete="off"
                spellcheck="false"
                :aria-invalid="errorIndex === index"
                :aria-describedby="errorIndex === index ? `record-part-code-error-${index}` : undefined"
                @focus="onScanInputFocus"
                @click="onScanInputFocus"
                @input="clearPartError(index)"
              >
            </div>
          </div>
          <span v-if="errorIndex === index" :id="`record-part-code-error-${index}`" class="pts-field-error" role="alert">请输入第 {{ index + 1 }} 个部件码</span>
        </div>
        <div v-if="!parts.length" class="pts-empty">
          该记录没有部件码
        </div>
      </div>
      <ElFormItem label="校对备注" class="mt-4 mb-0">
        <ElInput
          v-model="remarks"
          name="remarks"
          type="textarea"
          :rows="3"
          :maxlength="REMARKS_MAX_LENGTH"
          show-word-limit
          placeholder="可填写本次修改说明"
        />
      </ElFormItem>
    </ElForm>
    <template #footer>
      <ElButton @click="visible = false">
        取消
      </ElButton>
      <ElButton type="primary" :loading="submitting" @click="submitRecordEdit">
        保存修改
      </ElButton>
    </template>
  </ElDialog>
</template>
