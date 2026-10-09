<script setup lang="ts">
/**
 * Tells the operator when a list is showing a window rather than everything.
 *
 * The list endpoints return at most a few hundred rows and report the real total
 * in headers. Without this the page looks identical whether it is showing all 40
 * records or the newest 500 of 30,000 — and the second case is the one where
 * someone concludes a record is missing.
 *
 * Renders nothing unless the response actually was truncated, so pages that fit
 * in one window are unchanged.
 */
import { computed } from 'vue'

import { listWindowFor } from '@/api'

const props = defineProps<{
  /** The list endpoint this page loads, e.g. `/api/purchase-orders`. */
  url: string
}>()

const info = computed(() => listWindowFor(props.url))

const truncated = computed(() => {
  const current = info.value
  return current !== undefined && current.returned < current.total
})
</script>

<template>
  <ElAlert
    v-if="truncated && info"
    id="list-window-notice"
    type="warning"
    :closable="false"
    show-icon
    class="mb-3"
  >
    <template #title>
      仅显示最新 {{ info.returned }} 条，共 {{ info.total }} 条
    </template>
    <span>更早的记录未在此页显示。请用筛选条件缩小范围，或使用导出功能获取完整数据。</span>
  </ElAlert>
</template>
