<script setup lang="ts">
/**
 * `/` is never shown: the router guard sends every role to the first page of
 * its navigation (settings.app.home.enable = false). This view only renders
 * for the instant before that redirect, or when a role has no page at all.
 */
defineOptions({
  name: 'Home',
})

const appAccountStore = useAppAccountStore()
</script>

<template>
  <div class="p-10 text-center text-muted-foreground">
    <p v-if="appAccountStore.isLogin">
      正在打开工作台…
    </p>
    <p v-else>
      请先登录
    </p>
  </div>
</template>
