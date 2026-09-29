<script setup lang="ts">
import type { FormInstance, FormRules } from 'element-plus'
import { errorMessage } from '@/api'

defineOptions({
  name: 'Login',
})

const route = useRoute()
const router = useRouter()
const appAccountStore = useAppAccountStore()

const title = import.meta.env.VITE_APP_TITLE
const formRef = ref<FormInstance>()
const passwordInput = ref<{ select: () => void, focus: () => void }>()
const submitting = ref(false)
const loginError = ref(appAccountStore.loginNotice)
const model = reactive({
  username: localStorage.getItem('login_account') ?? '',
  password: '',
})

const rules: FormRules = {
  username: [{ required: true, message: '请输入账号', trigger: 'blur' }],
  password: [{ required: true, message: '请输入密码', trigger: 'blur' }],
}

const redirect = computed(() => {
  const value = route.query.redirect?.toString() ?? '/'
  // Only same-app paths; never an absolute URL.
  return value.startsWith('/') && !value.startsWith('//') ? value : '/'
})

function enterApplication() {
  router.replace(redirect.value)
}

async function submit() {
  if (!formRef.value || submitting.value) {
    return
  }
  const valid = await formRef.value.validate().catch(() => false)
  if (!valid) {
    return
  }
  submitting.value = true
  loginError.value = ''
  try {
    await appAccountStore.login({ username: model.username.trim(), password: model.password })
    localStorage.setItem('login_account', model.username.trim())
    model.password = ''
    // 首次登录必须先改密：强制弹窗会在当前页面打开，改密成功后再进入系统
    if (!appAccountStore.passwordChangeRequired) {
      enterApplication()
    }
  }
  catch (error) {
    loginError.value = errorMessage(error, '登录失败')
    passwordInput.value?.select()
  }
  finally {
    submitting.value = false
  }
}

watch(() => appAccountStore.passwordChangeRequired, (required, wasRequired) => {
  if (wasRequired && !required && appAccountStore.isLogin) {
    enterApplication()
  }
})

onMounted(() => {
  appAccountStore.loginNotice = ''
})
</script>

<template>
  <div class="login-page">
    <div class="login-card">
      <div class="login-brand">
        <div class="login-brand-mark" aria-hidden="true">
          聚星
        </div>
        <h1 class="login-title">
          {{ title }}
        </h1>
        <p class="login-subtitle">
          使用管理员、仓管或运营账号登录
        </p>
      </div>
      <ElForm id="login-form" ref="formRef" :model="model" :rules="rules" label-position="top" size="large" @submit.prevent="submit">
        <ElFormItem label="账号" prop="username">
          <ElInput v-model="model.username" name="username" autocomplete="username" autofocus placeholder="请输入账号">
            <template #prefix>
              <FaIcon name="i-ri:user-3-line" />
            </template>
          </ElInput>
        </ElFormItem>
        <ElFormItem label="密码" prop="password">
          <ElInput ref="passwordInput" v-model="model.password" name="password" type="password" show-password autocomplete="current-password" placeholder="请输入密码" @keyup.enter="submit">
            <template #prefix>
              <FaIcon name="i-ri:lock-2-line" />
            </template>
          </ElInput>
        </ElFormItem>
        <ElAlert v-if="loginError" :title="loginError" type="error" :closable="false" show-icon class="mb-4" />
        <ElButton type="primary" native-type="submit" class="w-full" :loading="submitting">
          登录
        </ElButton>
      </ElForm>
      <p class="login-footnote">
        首次登录需修改初始密码；忘记密码请联系系统管理员重置
      </p>
    </div>
  </div>
</template>

<style scoped>
.login-page {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 100vh;
  padding: 24px;
  background: var(--g-main-area-bg);
}

.login-card {
  width: 100%;
  max-width: 400px;
  padding: 36px 32px 28px;
  background: oklch(var(--card));
  border: 1px solid oklch(var(--border));
  border-radius: 12px;
}

.login-brand {
  margin-bottom: 24px;
  text-align: center;
}

.login-brand-mark {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 56px;
  height: 56px;
  margin-bottom: 14px;
  font-size: 18px;
  font-weight: 700;
  color: oklch(var(--primary-foreground));
  letter-spacing: 1px;
  background: oklch(var(--primary));
  border-radius: 14px;
}

.login-title {
  margin: 0;
  font-size: 20px;
  font-weight: 600;
}

.login-subtitle {
  margin: 8px 0 0;
  font-size: 13px;
  color: oklch(var(--muted-foreground));
}

.login-footnote {
  margin: 18px 0 0;
  font-size: 12px;
  color: oklch(var(--muted-foreground));
  text-align: center;
}
</style>
