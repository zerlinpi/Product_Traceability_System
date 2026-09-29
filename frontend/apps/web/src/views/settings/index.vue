<script setup lang="ts">
/**
 * 系统设置（管理员）— the general workflow switch and the Lingxing integration.
 *
 * - Credentials are write-only: the server only ever echoes masked values,
 *   the inputs always start blank, and leaving both blank keeps what is
 *   stored. appId and appSecret must be changed together.
 * - The three write endpoints enable 采购单下单 / 入库推送 / 库存同步
 *   individually (each push is gated on its own endpoint).
 * - The quality-release flag is only sent when it actually changed: the
 *   server refuses that change while a workstation is mid-scan, which must
 *   not block saving unrelated Lingxing settings.
 */
import type { AnyRecord } from '@/api/types'
import operationsApi from '@/api/modules/operations'
import { notifyError, notifySuccess } from '@/utils/feedback'

defineOptions({
  name: 'SettingsPage',
})

const loading = ref(false)
const saving = ref(false)
/** Settings as last read from the server; null until the first successful load. */
const loaded = ref<AnyRecord | null>(null)
const form = reactive({
  requireQualityRelease: false,
  appId: '',
  appSecret: '',
  endpointPurchaseOrder: '',
  endpointInboundReceipt: '',
  endpointInventorySync: '',
})

const lingxing = computed<AnyRecord | null>(() => loaded.value?.lingxing ?? null)
const credentialsConfigured = computed(() => Boolean(lingxing.value?.configured))

const lingxingState = computed(() => {
  const data = lingxing.value
  if (!data) {
    return '尚未配置'
  }
  const credentialState = data.configured ? '凭据已配置' : '尚未配置'
  const ready: AnyRecord = data.endpointsConfigured ?? {}
  const endpointState = data.writeEndpointsConfigured
    ? '写入接口已配置'
    : (ready.purchaseOrder ? '采购下单接口已配置，其余待配置' : '写入接口待配置')
  return `${credentialState}；${endpointState}`
})

const maskedHint = computed(() => {
  const data = lingxing.value
  if (!data) {
    return '保存后只显示脱敏值；留空则不修改已有凭据。'
  }
  return data.configured
    ? `当前值：${data.appId} / ${data.appSecret}`
    : '保存后只显示脱敏值；两个凭据需同时填写。'
})

const credentialPlaceholder = computed(() => (credentialsConfigured.value ? '输入新值（留空则不修改）' : '输入新值'))

function applySettings(data: AnyRecord) {
  loaded.value = data
  form.requireQualityRelease = Boolean(data.requireQualityRelease)
  // Secrets are never echoed back into the inputs.
  form.appId = ''
  form.appSecret = ''
  const endpoints: AnyRecord = data.lingxing?.endpoints ?? {}
  form.endpointPurchaseOrder = endpoints.purchaseOrder || ''
  form.endpointInboundReceipt = endpoints.inboundReceipt || ''
  form.endpointInventorySync = endpoints.inventorySync || ''
}

async function loadSettings() {
  loading.value = true
  try {
    applySettings(await operationsApi.settings())
  }
  catch (error) {
    notifyError(error, '系统设置加载失败')
  }
  finally {
    loading.value = false
  }
}

async function submitSettings() {
  // Never save a form that was not filled from the server: blank endpoint
  // fields would silently switch the configured pushes off.
  if (saving.value || !loaded.value) {
    return
  }
  const body: AnyRecord = {}
  if (form.requireQualityRelease !== Boolean(loaded.value.requireQualityRelease)) {
    body.requireQualityRelease = form.requireQualityRelease
  }
  const values = [form.appId.trim(), form.appSecret.trim()]
  if (values.some(Boolean)) {
    if (!values.every(Boolean)) {
      notifyError('appId、appSecret 需同时填写', '设置保存失败')
      return
    }
    body.appId = values[0]
    body.appSecret = values[1]
  }
  body.lingxingEndpoints = {
    purchaseOrder: form.endpointPurchaseOrder.trim(),
    inboundReceipt: form.endpointInboundReceipt.trim(),
    inventorySync: form.endpointInventorySync.trim(),
  }
  saving.value = true
  try {
    // The response carries the same shape as GET /api/settings.
    applySettings(await operationsApi.updateSettings(body))
    notifySuccess('系统设置已保存')
  }
  catch (error) {
    notifyError(error, '设置保存失败')
  }
  finally {
    saving.value = false
  }
}

onMounted(loadSettings)
</script>

<template>
  <div class="pts-page">
    <FaPageHeader title="系统设置" description="通用流程参数与领星凭据统一在此维护，敏感值仅脱敏回显" />
    <ElForm id="settings-form" v-loading="loading" :model="form" label-position="top" @submit.prevent="submitSettings">
      <FaPageMain title="通用设置">
        <p class="pts-muted text-sm mt-0 mb-3">
          影响历史逐台流程，不影响批次扫码
        </p>
        <ElFormItem label="逐台记录需要质量放行" class="mb-0">
          <ElSwitch
            v-model="form.requireQualityRelease"
            name="requireQualityRelease"
            aria-label="逐台记录需要质量放行"
            active-text="开启"
            inactive-text="关闭"
          />
        </ElFormItem>
      </FaPageMain>

      <FaPageMain title="领星凭据">
        <p id="settings-lingxing-state" class="pts-muted text-sm mt-0 mb-3" aria-live="polite">
          {{ lingxingState }}
        </p>
        <div class="gap-x-5 grid grid-cols-1 md:grid-cols-2">
          <ElFormItem label="appId">
            <ElInput v-model="form.appId" name="appId" maxlength="256" autocomplete="off" :placeholder="credentialPlaceholder" />
          </ElFormItem>
          <ElFormItem label="appSecret">
            <ElInput v-model="form.appSecret" name="appSecret" type="password" maxlength="256" autocomplete="new-password" :placeholder="credentialPlaceholder" />
          </ElFormItem>
        </div>
        <p id="settings-lingxing-masked" class="pts-muted text-xs m-0">
          {{ maskedHint }}
        </p>
      </FaPageMain>

      <FaPageMain title="领星写入接口">
        <p class="pts-muted text-sm mt-0 mb-3">
          采购下单、入库推送、库存同步的接口地址（路径或完整链接）；各推送分别按自己的接口是否配置启用
        </p>
        <div class="gap-x-5 grid grid-cols-1 md:grid-cols-2">
          <ElFormItem label="采购订单接口">
            <ElInput
              v-model="form.endpointPurchaseOrder"
              name="endpointPurchaseOrder"
              maxlength="300"
              autocomplete="off"
              placeholder="默认 /erp/sc/routing/purchase/purchase/setOrders"
              aria-describedby="settings-endpoint-po-help"
            />
            <p id="settings-endpoint-po-help" class="pts-muted text-xs mt-1 mb-0 w-full leading-normal">
              采购单下单：把“待下单”采购单改为“待到货”，按领星采购单号提交
            </p>
          </ElFormItem>
          <ElFormItem label="入库接口">
            <ElInput v-model="form.endpointInboundReceipt" name="endpointInboundReceipt" maxlength="300" autocomplete="off" placeholder="如 /erp/sc/...（留空停用）" />
          </ElFormItem>
          <ElFormItem label="库存同步接口">
            <ElInput v-model="form.endpointInventorySync" name="endpointInventorySync" maxlength="300" autocomplete="off" placeholder="如 /erp/sc/...（留空停用）" />
          </ElFormItem>
        </div>
      </FaPageMain>

      <div class="mx-4 flex justify-end">
        <ElButton type="primary" native-type="submit" :loading="saving" :disabled="!loaded">
          <template #icon>
            <FaIcon name="i-ri:save-line" />
          </template>
          保存设置
        </ElButton>
      </div>
    </ElForm>
  </div>
</template>
