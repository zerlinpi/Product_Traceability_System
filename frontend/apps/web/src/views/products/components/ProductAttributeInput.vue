<script setup lang="ts">
/**
 * One 产品资料 column in the product form: a picture upload, a fixed option
 * list, a date, a number, a long text or a plain text input, depending on the
 * column (see ../utils.ts).
 */
import { attributeFieldKind, PRODUCT_ATTRIBUTE_OPTIONS } from '../utils'
import ProductImageField from './ProductImageField.vue'

defineOptions({
  name: 'ProductAttributeInput',
})

const props = defineProps<{
  column: string
  imageColumns: string[]
}>()

const model = defineModel<string>({ default: '' })

const kind = computed(() => attributeFieldKind(props.column, props.imageColumns))
const options = computed(() => PRODUCT_ATTRIBUTE_OPTIONS[props.column] ?? [])
const wide = computed(() => kind.value === 'image' || kind.value === 'textarea')
</script>

<template>
  <ElFormItem :label="props.column" :class="{ 'pts-form-wide': wide }">
    <ProductImageField v-if="kind === 'image'" v-model="model" :column="props.column" />
    <ElSelect v-else-if="kind === 'select'" v-model="model" clearable placeholder="（不选）" class="w-full">
      <ElOption v-for="option in options" :key="option" :label="option" :value="option" />
    </ElSelect>
    <ElInput v-else-if="kind === 'textarea'" v-model="model" type="textarea" :rows="2" maxlength="500" />
    <ElDatePicker v-else-if="kind === 'date'" v-model="model" type="date" value-format="YYYY-MM-DD" placeholder="选择日期" class="w-full!" />
    <ElInput v-else-if="kind === 'number'" v-model="model" type="number" step="any" />
    <ElInput v-else v-model="model" maxlength="500" />
  </ElFormItem>
</template>
