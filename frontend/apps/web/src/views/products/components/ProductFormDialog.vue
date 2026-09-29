<script setup lang="ts">
/**
 * 添加新产品 / 编辑产品.
 *
 * ADMIN builds the manufacturing BOM: each row picks supplier → part → the
 * supplier inventory batch the part is drawn from, plus the per-set quantity.
 * Every row may be removed: a product without rows is a complete,
 * indivisible item. OPERATIONS register simple products — name, optional SKU
 * (locked once created) and the 产品资料 profile — and never load the
 * admin-only supplier / part / inventory-batch catalogs.
 *
 * 产品资料: non-empty values are sent, plus `null` for a stored column the
 * user emptied; the server merges them over the stored profile (absent columns
 * are kept, `null` clears) and fills SKU / 品名 / 创建人 / 时间 / 状态 itself.
 */
import type { FormInstance, FormItemRule, FormRules } from 'element-plus'
import type { ProductAttributeMeta } from '@/api/modules/catalog'
import type { AnyRecord } from '@/api/types'
import catalogApi from '@/api/modules/catalog'
import { notifyError, notifySuccess } from '@/utils/feedback'
import { componentsByName, loadProductAttributeMeta, PRODUCT_ATTRIBUTE_PRIMARY_COLUMNS, PRODUCT_THUMB_COLUMN } from '../utils'
import ProductAttributeInput from './ProductAttributeInput.vue'

defineOptions({
  name: 'ProductFormDialog',
})

const props = defineProps<{
  /** The product to edit, or `null` to add a new one. */
  product: AnyRecord | null
}>()

const emit = defineEmits<{
  saved: [product: AnyRecord, created: boolean]
}>()

const visible = defineModel<boolean>({ default: false })

/** Expanded part count limit per product (MAX_REQUIRED_PARTS on the server). */
const MAX_PARTS = 20

interface ComponentRow {
  key: number
  supplierId?: number
  partTypeId?: number
  inventoryBatchId?: number
  quantity: number
}

const appAccountStore = useAppAccountStore()
const isOperations = computed(() => appAccountStore.isOperations)
const isAdmin = computed(() => appAccountStore.isAdmin)

let rowSequence = 0
const formRef = ref<FormInstance>()
const nameInput = ref<{ focus: () => void }>()
const saving = ref(false)
const form = reactive({
  name: '',
  modelCode: '',
  components: [] as ComponentRow[],
})
const attributes = reactive<Record<string, string>>({})
const meta = ref<ProductAttributeMeta>({ columns: [], derived: [], imageColumns: [PRODUCT_THUMB_COLUMN] })
const metaLoading = ref(false)
const moreOpen = ref<string[]>([])

const suppliers = ref<AnyRecord[]>([])
const parts = ref<AnyRecord[]>([])
const inventoryBatches = ref<AnyRecord[]>([])
const catalogLoading = ref(false)

const isEdit = computed(() => Boolean(props.product?.id))
const title = computed(() => (isEdit.value ? '编辑产品' : '添加新产品'))
const help = computed(() => (isOperations.value
  ? '填写产品名称、可选的产品编码与产品资料'
  : '填写产品名称，并连续添加需要扫码的部件'))

const derivedColumns = computed(() => new Set(meta.value.derived))
const editableColumns = computed(() => meta.value.columns.filter(column => !derivedColumns.value.has(column)))
const primaryColumns = computed(() => PRODUCT_ATTRIBUTE_PRIMARY_COLUMNS.filter(column => editableColumns.value.includes(column)))
const moreColumns = computed(() => editableColumns.value.filter(column => !primaryColumns.value.includes(column)))
/** System-maintained columns, shown read-only so they do not look missing. */
const autoFields = computed(() => meta.value.derived.map((column) => {
  const value = props.product?.attributes?.[column]
  return {
    column,
    value: value ? String(value) : (props.product ? '-' : '保存后自动填写'),
  }
}))
const totalParts = computed(() => form.components.reduce((sum, row) => sum + Number(row.quantity || 0), 0))

const rules: FormRules = {
  name: [
    { required: true, whitespace: true, message: '请输入产品名称', trigger: 'blur' },
    { max: 100, message: '产品名称不能超过 100 个字符', trigger: 'blur' },
  ],
  modelCode: [{ max: 60, message: '产品编码不能超过 60 个字符', trigger: 'blur' }],
}

const rowRules: Record<'supplierId' | 'partTypeId' | 'inventoryBatchId' | 'quantity', FormItemRule[]> = {
  supplierId: [{ required: true, message: '请选择供应商', trigger: 'change' }],
  partTypeId: [{ required: true, message: '请选择部件', trigger: 'change' }],
  inventoryBatchId: [{ required: true, message: '请选择供应批次', trigger: 'change' }],
  quantity: [{ required: true, message: `每套数量需为 1-${MAX_PARTS}`, trigger: 'change' }],
}

// ---- BOM rows ------------------------------------------------------------

function newRow(data: Partial<ComponentRow> = {}): ComponentRow {
  rowSequence += 1
  return {
    key: rowSequence,
    supplierId: data.supplierId ?? undefined,
    partTypeId: data.partTypeId ?? undefined,
    inventoryBatchId: data.inventoryBatchId ?? undefined,
    quantity: Number(data.quantity || 1),
  }
}

function addComponentRow() {
  form.components.push(newRow())
}

function removeComponentRow(index: number) {
  // A product may have no components at all (complete, indivisible item), so
  // every row can be removed.
  form.components.splice(index, 1)
}

/** Active suppliers, plus the one already bound to the row even if it was disabled since. */
function supplierOptions(row: ComponentRow) {
  return suppliers.value.filter(item => item.active || item.id === row.supplierId)
}

function partOptions(row: ComponentRow) {
  if (!row.supplierId) {
    return []
  }
  return parts.value.filter(item => item.supplierId === row.supplierId && (item.active || item.id === row.partTypeId))
}

function batchOptions(row: ComponentRow) {
  if (!row.partTypeId) {
    return []
  }
  return inventoryBatches.value.filter(item => item.partTypeId === row.partTypeId && (item.active || item.id === row.inventoryBatchId))
}

function partLabel(part: AnyRecord) {
  return `${part.name}${part.specification ? ` · ${part.specification}` : ''}`
}

function onSupplierChange(row: ComponentRow) {
  row.partTypeId = undefined
  row.inventoryBatchId = undefined
}

function onPartChange(row: ComponentRow) {
  row.inventoryBatchId = undefined
}

async function loadCatalogs() {
  catalogLoading.value = true
  try {
    const [supplierList, partList, batchList] = await Promise.all([
      catalogApi.suppliers(),
      catalogApi.partTypes(),
      catalogApi.inventoryBatches(),
    ])
    suppliers.value = supplierList
    parts.value = partList
    inventoryBatches.value = batchList
  }
  catch (error) {
    notifyError(error, '供应商与批次加载失败')
  }
  finally {
    catalogLoading.value = false
  }
}

async function loadMeta() {
  metaLoading.value = true
  try {
    meta.value = await loadProductAttributeMeta()
  }
  finally {
    metaLoading.value = false
  }
}

// ---- open / submit ---------------------------------------------------------

function resetForm() {
  const product = props.product
  form.name = product?.name ?? ''
  form.modelCode = product?.productCode ?? ''
  for (const key of Object.keys(attributes)) {
    delete attributes[key]
  }
  for (const [key, value] of Object.entries((product?.attributes ?? {}) as AnyRecord)) {
    attributes[key] = value === null || value === undefined ? '' : String(value)
  }
  // Operations never see the BOM editor; a new admin product starts with one row.
  form.components = isAdmin.value
    ? (product ? componentsByName(product).map(item => newRow(item)) : [newRow()])
    : []
  moreOpen.value = []
}

watch(visible, (value) => {
  if (!value) {
    return
  }
  resetForm()
  nextTick(() => formRef.value?.clearValidate())
  loadMeta()
  if (isAdmin.value) {
    loadCatalogs()
  }
})

function focusName() {
  nameInput.value?.focus()
}

async function submit() {
  if (!formRef.value || saving.value) {
    return
  }
  const valid = await formRef.value.validate().catch(() => false)
  if (!valid) {
    return
  }
  const productId = Number(props.product?.id || 0)
  const stored: AnyRecord = props.product?.attributes ?? {}
  const collected: Record<string, string | null> = {}
  for (const column of editableColumns.value) {
    const value = String(attributes[column] ?? '').trim()
    if (value !== '') {
      collected[column] = value
    }
    else if (productId && String(stored[column] ?? '').trim() !== '') {
      // Emptied on purpose: the server keeps absent columns, so a cleared
      // field has to be sent as null to be removed from the profile.
      collected[column] = null
    }
  }
  let body: AnyRecord
  if (isOperations.value) {
    body = { name: form.name.trim(), attributes: collected }
    const sku = form.modelCode.trim()
    if (!productId && sku) {
      body.modelCode = sku
    }
  }
  else {
    if (totalParts.value > MAX_PARTS) {
      notifyError(`一个产品的部件总数最多为 ${MAX_PARTS}`, '产品保存失败')
      return
    }
    body = {
      name: form.name.trim(),
      attributes: collected,
      components: form.components.map(row => ({
        inventoryBatchId: Number(row.inventoryBatchId),
        quantity: Number(row.quantity),
      })),
    }
  }
  saving.value = true
  try {
    const saved = productId
      ? await catalogApi.updateProduct(productId, body)
      : await catalogApi.createProduct(body)
    visible.value = false
    let detail = '已保存，可在采购订单中选择该产品'
    if (!isOperations.value) {
      detail = form.components.length
        ? '供应批次已绑定，可按库存生成产品二维码'
        : '未配置部件，已保存为完整成品'
    }
    notifySuccess(productId ? '产品已更新' : '产品已添加', detail)
    emit('saved', saved, !productId)
  }
  catch (error) {
    notifyError(error, '产品保存失败')
  }
  finally {
    saving.value = false
  }
}
</script>

<template>
  <ElDialog v-model="visible" :title="title" width="920px" top="5vh" :close-on-click-modal="false" append-to-body @opened="focusName">
    <p class="pts-muted text-sm mt-0 mb-4">
      {{ help }}
    </p>
    <ElForm id="product-form" ref="formRef" :model="form" :rules="rules" label-position="top" @submit.prevent="submit">
      <div class="gap-x-4 grid grid-cols-1" :class="{ 'sm:grid-cols-2': isOperations }">
        <ElFormItem label="产品名称" prop="name">
          <ElInput ref="nameInput" v-model="form.name" name="name" maxlength="100" placeholder="例如：升降桌控制器" />
        </ElFormItem>
        <ElFormItem v-if="isOperations" id="product-sku-field" label="产品编码 / SKU（可选）" prop="modelCode">
          <ElInput v-model="form.modelCode" name="modelCode" maxlength="60" placeholder="留空则由系统自动生成" :readonly="isEdit" :disabled="isEdit" />
        </ElFormItem>
      </div>

      <section v-if="isAdmin" id="product-components-section" v-loading="catalogLoading" class="mb-5">
        <div class="mb-3 flex gap-3 items-start justify-between">
          <div>
            <h3 class="text-sm font-semibold m-0">
              产品部件
            </h3>
            <p class="pts-muted text-xs mb-0 mt-1">
              依次选择供应商、部件与到货批次；数量表示每套产品用量。完整成品可删除所有部件行，不配置部件。
            </p>
          </div>
          <ElButton id="add-component" size="small" @click="addComponentRow">
            <template #icon>
              <FaIcon name="i-ri:add-line" />
            </template>
            添加部件
          </ElButton>
        </div>
        <div v-if="!form.components.length" class="pts-empty pts-empty-compact">
          未配置部件，保存后为完整成品
        </div>
        <div v-for="(row, index) in form.components" :key="row.key" class="pts-component-row">
          <ElFormItem label="供应商" :prop="`components.${index}.supplierId`" :rules="rowRules.supplierId">
            <ElSelect v-model="row.supplierId" name="supplierId" placeholder="请选择" filterable class="w-full" @change="onSupplierChange(row)">
              <ElOption v-for="supplier in supplierOptions(row)" :key="supplier.id" :label="supplier.name" :value="supplier.id" />
            </ElSelect>
          </ElFormItem>
          <ElFormItem label="部件" :prop="`components.${index}.partTypeId`" :rules="rowRules.partTypeId">
            <ElSelect v-model="row.partTypeId" name="partTypeId" placeholder="请选择部件" filterable :disabled="!row.supplierId" class="w-full" @change="onPartChange(row)">
              <ElOption v-for="part in partOptions(row)" :key="part.id" :label="partLabel(part)" :value="part.id" />
            </ElSelect>
          </ElFormItem>
          <ElFormItem label="供应批次" :prop="`components.${index}.inventoryBatchId`" :rules="rowRules.inventoryBatchId">
            <ElSelect v-model="row.inventoryBatchId" name="inventoryBatchId" placeholder="请选择批次" filterable :disabled="!row.partTypeId" class="w-full">
              <ElOption
                v-for="batch in batchOptions(row)"
                :key="batch.id"
                :label="`${batch.batchNo} · 可用 ${batch.quantityAvailable}`"
                :value="batch.id"
                :disabled="batch.quantityAvailable <= 0 && batch.id !== row.inventoryBatchId"
              />
            </ElSelect>
          </ElFormItem>
          <ElFormItem label="每套数量" :prop="`components.${index}.quantity`" :rules="rowRules.quantity">
            <ElInputNumber v-model="row.quantity" name="quantity" :min="1" :max="MAX_PARTS" :step="1" step-strictly controls-position="right" class="w-full!" />
          </ElFormItem>
          <div class="pts-component-row-actions">
            <ElButton link type="danger" :aria-label="`删除第 ${index + 1} 行部件`" @click="removeComponentRow(index)">
              <template #icon>
                <FaIcon name="i-ri:delete-bin-line" />
              </template>
              删除此行
            </ElButton>
          </div>
        </div>
      </section>

      <section v-loading="metaLoading">
        <h3 class="text-sm font-semibold m-0">
          产品资料
        </h3>
        <p class="pts-muted text-xs mb-3 mt-1">
          SKU、品名、创建人与时间由系统自动填写，其余字段选填
        </p>
        <div v-if="autoFields.length" id="product-attribute-auto" class="mb-4 flex flex-wrap gap-2">
          <span v-for="item in autoFields" :key="item.column" class="pts-auto-field">
            <small>{{ item.column }}</small>
            <b>{{ item.value }}</b>
          </span>
        </div>
        <div id="product-attribute-fields">
          <div class="pts-form-grid">
            <ProductAttributeInput
              v-for="column in primaryColumns"
              :key="column"
              v-model="attributes[column]"
              :column="column"
              :image-columns="meta.imageColumns"
            />
          </div>
          <ElCollapse v-if="moreColumns.length" v-model="moreOpen" class="mt-1">
            <ElCollapseItem name="more" :title="`更多产品资料（选填，共 ${moreColumns.length} 项）`">
              <div class="pts-form-grid pt-2">
                <ProductAttributeInput
                  v-for="column in moreColumns"
                  :key="column"
                  v-model="attributes[column]"
                  :column="column"
                  :image-columns="meta.imageColumns"
                />
              </div>
            </ElCollapseItem>
          </ElCollapse>
        </div>
      </section>
    </ElForm>
    <template #footer>
      <ElButton @click="visible = false">
        取消
      </ElButton>
      <ElButton type="primary" :loading="saving" @click="submit">
        保存产品
      </ElButton>
    </template>
  </ElDialog>
</template>
