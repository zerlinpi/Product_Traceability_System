<script setup lang="ts">
import imgLogo from '@/assets/images/logo.svg'

defineOptions({
  name: 'Logo',
})

withDefaults(
  defineProps<{
    showLogo?: boolean
    showTitle?: boolean
  }>(),
  {
    showLogo: true,
    showTitle: true,
  },
)

const appSettingsStore = useAppSettingsStore()

// The full product name is too long for the sidebar; the short brand is shown
// and the full name stays available as the tooltip and the page title.
const title = ref(import.meta.env.VITE_APP_TITLE)
const shortTitle = '聚星同创'
const subtitle = '仓库管理系统'
const logo = ref(imgLogo)

const to = computed(() => appSettingsStore.settings.app.home.enable ? appSettingsStore.settings.app.home.fullPath : '')
</script>

<template>
  <RouterLink :to class="text-primary px-3 no-underline flex-center gap-2 h-[var(--g-sidebar-logo-height)] w-inherit" :class="{ 'cursor-default': !appSettingsStore.settings.app.home.enable }" :title="title">
    <img v-if="showLogo" :src="logo" alt="" class="logo h-[30px] w-[30px] object-contain">
    <span v-if="showTitle" class="leading-tight min-w-0 flex flex-col">
      <span class="font-bold block truncate">{{ shortTitle }}</span>
      <span class="text-[11px] text-muted-foreground font-normal block truncate">{{ subtitle }}</span>
    </span>
  </RouterLink>
</template>
