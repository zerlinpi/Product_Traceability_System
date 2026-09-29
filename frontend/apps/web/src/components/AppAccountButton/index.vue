<script setup lang="ts">
import type { HTMLAttributes } from 'vue'
import { cn } from '@/utils'
import eventBus from '@/utils/eventBus'

defineOptions({
  name: 'AppAccountButton',
})

const props = withDefaults(defineProps<{
  onlyAvatar?: boolean
  dropdownAlign?: 'start' | 'center' | 'end'
  dropdownSide?: 'left' | 'right' | 'top' | 'bottom'
  buttonVariant?: 'secondary' | 'ghost'
  class?: HTMLAttributes['class']
}>(), {
  dropdownAlign: 'end',
  dropdownSide: 'right',
  buttonVariant: 'ghost',
})

const appSettingsStore = useAppSettingsStore()
const appAccountStore = useAppAccountStore()

const initial = computed(() => Array.from(appAccountStore.account.trim())[0] || '用')
</script>

<template>
  <FaDropdown
    :align="dropdownAlign" :side="dropdownSide" :items="[
      [
        { label: '修改密码', icon: 'i-ri:lock-password-line', handle: () => appAccountStore.openPasswordDialog() },
      ],
      [
        ...(appSettingsStore.mode === 'pc'
          ? [{ label: '快捷键', icon: 'i-ri:keyboard-line', handle: () => eventBus.emit('global-hotkeys-intro-toggle') }]
          : []),
      ],
      [
        {
          label: '退出登录',
          icon: 'i-ri:logout-box-r-line',
          handle: () => appAccountStore.logout(),
        },
      ],
    ]" class="flex-center"
  >
    <template #header>
      <div class="flex-center-start gap-2">
        <FaAvatar src="" :fallback="initial" shape="square" />
        <div class="min-w-0 space-y-1">
          <div class="text-base lh-none truncate">
            {{ appAccountStore.account }}
          </div>
          <div class="text-xs text-secondary-foreground/60 font-normal">
            {{ appAccountStore.roleLabel }} · {{ appAccountStore.username }}
          </div>
        </div>
      </div>
    </template>
    <FaButton
      :variant="buttonVariant" size="icon-sm" :class="cn('flex-center gap-1 p-2', {
        'p-1': onlyAvatar,
      }, props.class)"
      aria-label="账户菜单"
    >
      <FaAvatar src="" :fallback="initial" :class="cn('size-6 text-xs', { 'size-full': onlyAvatar })" />
      <div v-if="!onlyAvatar" class="flex-center-between flex-1 gap-2 min-w-0">
        <div class="text-start flex-1 truncate">
          {{ appAccountStore.account }}
          <span class="pts-role-badge" :data-role="appAccountStore.role">{{ appAccountStore.roleLabel }}</span>
        </div>
        <FaIcon name="i-ri:expand-up-down-line" />
      </div>
    </FaButton>
  </FaDropdown>
</template>
