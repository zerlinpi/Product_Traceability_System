<script setup lang="ts">
/**
 * A grid of purchase-order template columns (采购单模板字段).
 *
 * Option columns pick from fixed lists (（不选） = blank), 预计到货时间 is a
 * date picker, numeric columns are number inputs (any decimal, as before) and
 * everything else is free text capped at 500 characters like the server.
 * Values stay strings so what the operator typed is exactly what is sent.
 */
import type { FieldSpec } from '../template'

defineOptions({
  name: 'PurchaseOrderFieldGrid',
})

const props = defineProps<{
  fields: FieldSpec[]
  /** Columns shown but not editable (采购单号 while editing: the number is immutable). */
  lockedColumns?: string[]
}>()

const values = defineModel<Record<string, string>>({ required: true })

function setValue(column: string, value: unknown) {
  values.value = {
    ...values.value,
    [column]: value === null || value === undefined ? '' : String(value),
  }
}

function isLocked(column: string) {
  return props.lockedColumns?.includes(column) ?? false
}
</script>

<template>
  <div class="gap-x-5 grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-3">
    <ElFormItem v-for="field in fields" :key="field.column" :label="field.column" :data-column="field.column">
      <ElSelect
        v-if="field.kind === 'select'"
        :model-value="values[field.column]"
        placeholder="（不选）"
        clearable
        class="w-full"
        @update:model-value="setValue(field.column, $event)"
      >
        <ElOption v-for="option in field.options" :key="option" :label="option" :value="option" />
      </ElSelect>
      <ElDatePicker
        v-else-if="field.kind === 'date'"
        :model-value="values[field.column]"
        type="date"
        value-format="YYYY-MM-DD"
        placeholder="选择日期"
        class="w-full!"
        @update:model-value="setValue(field.column, $event)"
      />
      <ElInput
        v-else-if="field.kind === 'number'"
        :model-value="values[field.column]"
        type="number"
        step="any"
        @update:model-value="setValue(field.column, $event)"
      />
      <ElInput
        v-else
        :model-value="values[field.column]"
        maxlength="500"
        :placeholder="isLocked(field.column) ? '' : field.placeholder"
        :disabled="isLocked(field.column)"
        :title="isLocked(field.column) ? '采购单号创建后不可修改' : undefined"
        @update:model-value="setValue(field.column, $event)"
      />
    </ElFormItem>
  </div>
</template>
