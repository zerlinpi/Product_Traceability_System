<script setup lang="ts">
/**
 * 产品族谱弹窗 — either a production batch with the supplier batches it
 * consumed (reverse trace), or a legacy per-unit main code with each bound
 * part (QR, part code, supplier and part batch). Presentational only: the
 * trace page loads the data and passes a prepared view (see ../helpers.ts).
 */
import type { GenealogyView } from '../helpers'
import { qualityStatus } from '@/utils/format'

defineOptions({
  name: 'TraceGenealogyDialog',
})

defineProps<{
  view: GenealogyView | null
}>()

const visible = defineModel<boolean>({ default: false })
</script>

<template>
  <ElDialog id="genealogy-modal" v-model="visible" title="产品族谱" width="880px" class="pts-dialog-fluid" append-to-body>
    <template v-if="view">
      <p id="genealogy-subtitle" class="pts-muted text-sm mt-0 mb-4">
        {{ view.subtitle }}
      </p>
      <div id="genealogy-content">
        <div class="pts-genealogy-overview">
          <div class="pts-genealogy-main">
            <img v-if="view.qrUrl" :src="view.qrUrl" :alt="view.qrAlt" class="genealogy-main-qr">
            <div class="pts-genealogy-main-copy">
              <span>{{ view.codeLabel }}</span>
              <strong>{{ view.code }}</strong>
              <small>{{ view.codeHint }}</small>
            </div>
          </div>
          <div class="pts-genealogy-summary-grid">
            <div v-for="item in view.summary" :key="item.label" class="pts-genealogy-summary">
              <span>{{ item.label }}</span>
              <div class="pts-genealogy-summary-value">
                <ElTag v-if="item.status !== undefined" :type="qualityStatus(item.status).type" disable-transitions>
                  {{ qualityStatus(item.status).label }}
                </ElTag>
                <template v-else>
                  {{ item.value }}
                </template>
              </div>
            </div>
          </div>
        </div>
        <div class="pts-genealogy-section">
          <div>
            <span>{{ view.sectionLabel }}</span>
            <strong>{{ view.sectionTitle }}</strong>
          </div>
          <small>{{ view.sectionHint }}</small>
        </div>
        <ul v-if="view.items.length" class="pts-genealogy-parts" :aria-label="view.sectionLabel">
          <li v-for="item in view.items" :key="item.key" class="pts-genealogy-part">
            <span class="pts-genealogy-part-index" aria-hidden="true">{{ item.index }}</span>
            <img v-if="item.qrUrl" :src="item.qrUrl" :alt="item.qrAlt" loading="lazy">
            <div class="pts-genealogy-part-copy">
              <strong>{{ item.title }}</strong>
              <span v-if="item.detail">{{ item.detail }}</span>
              <code>{{ item.code }}</code>
              <small v-if="item.note">{{ item.note }}</small>
            </div>
          </li>
        </ul>
        <div v-else class="pts-empty">
          {{ view.emptyText }}
        </div>
      </div>
    </template>
  </ElDialog>
</template>
