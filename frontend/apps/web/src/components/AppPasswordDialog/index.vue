<script setup lang="ts">
/**
 * Change-password dialog.
 *
 * Opened voluntarily from the account menu, or forced: right after the first
 * login of an account whose password was set by an administrator, and
 * whenever the API answers 428. A forced dialog cannot be dismissed — the
 * backend rejects every other request until the password is changed.
 */
import type { FormInstance, FormRules } from 'element-plus'
import { errorMessage } from '@/api'
import { notifySuccess } from '@/utils/feedback'

defineOptions({
  name: 'AppPasswordDialog',
})

const appAccountStore = useAppAccountStore()

const formRef = ref<FormInstance>()
const submitting = ref(false)
const serverError = ref('')
const model = reactive({
  currentPassword: '',
  newPassword: '',
  confirmPassword: '',
})

const forced = computed(() => appAccountStore.passwordChangeRequired)
const visible = computed({
  get: () => appAccountStore.isLogin && appAccountStore.passwordDialogVisible,
  set: (value: boolean) => {
    if (!value) {
      appAccountStore.closePasswordDialog()
    }
  },
})

const rules: FormRules = {
  currentPassword: [{ required: true, message: '请输入当前密码', trigger: 'blur' }],
  newPassword: [
    { required: true, message: '请输入新密码', trigger: 'blur' },
    { min: 8, max: 128, message: '密码长度需为 8-128 个字符', trigger: 'blur' },
    {
      validator: (_rule, value, callback) => {
        if (value && value === model.currentPassword) {
          callback(new Error('新密码不能与当前密码相同'))
        }
        else if (value && appAccountStore.username && value.toLowerCase() === appAccountStore.username.toLowerCase()) {
          callback(new Error('密码不能与账号相同'))
        }
        else {
          callback()
        }
      },
      trigger: 'blur',
    },
  ],
  confirmPassword: [
    { required: true, message: '请再次输入新密码', trigger: 'blur' },
    {
      validator: (_rule, value, callback) => {
        if (value !== model.newPassword) {
          callback(new Error('两次输入的新密码不一致'))
        }
        else {
          callback()
        }
      },
      trigger: 'blur',
    },
  ],
}

watch(visible, (value) => {
  if (value) {
    serverError.value = ''
    model.currentPassword = ''
    model.newPassword = ''
    model.confirmPassword = ''
    nextTick(() => formRef.value?.clearValidate())
  }
})

async function submit() {
  if (!formRef.value || submitting.value) {
    return
  }
  const valid = await formRef.value.validate().catch(() => false)
  if (!valid) {
    return
  }
  submitting.value = true
  serverError.value = ''
  try {
    await appAccountStore.changePassword({
      currentPassword: model.currentPassword,
      newPassword: model.newPassword,
    })
    notifySuccess('密码已更新', '请牢记新密码，下次登录时使用')
  }
  catch (error) {
    serverError.value = errorMessage(error, '密码修改失败')
  }
  finally {
    submitting.value = false
  }
}
</script>

<template>
  <ElDialog
    v-model="visible"
    title="修改密码"
    width="440px"
    :close-on-click-modal="false"
    :close-on-press-escape="!forced"
    :show-close="!forced"
    align-center
    append-to-body
  >
    <p class="text-sm text-muted-foreground mb-4">
      {{ forced ? '首次登录必须修改初始密码，修改后才能继续使用系统' : '请输入当前密码和新密码' }}
    </p>
    <ElForm ref="formRef" :model="model" :rules="rules" label-position="top" @submit.prevent="submit">
      <ElFormItem label="当前密码" prop="currentPassword">
        <ElInput v-model="model.currentPassword" type="password" show-password autocomplete="current-password" name="currentPassword" />
      </ElFormItem>
      <ElFormItem label="新密码" prop="newPassword">
        <ElInput v-model="model.newPassword" type="password" show-password autocomplete="new-password" name="newPassword" />
      </ElFormItem>
      <ElFormItem label="确认新密码" prop="confirmPassword">
        <ElInput v-model="model.confirmPassword" type="password" show-password autocomplete="new-password" name="confirmPassword" @keyup.enter="submit" />
      </ElFormItem>
      <p class="text-xs text-muted-foreground -mt-2">
        至少 8 个字符；不能使用系统默认密码、常见密码、账号本身或重复/连续字符
      </p>
      <ElAlert v-if="serverError" :title="serverError" type="error" :closable="false" show-icon class="mt-3" />
    </ElForm>
    <template #footer>
      <ElButton v-if="!forced" @click="visible = false">
        取消
      </ElButton>
      <ElButton v-else @click="appAccountStore.logout()">
        退出登录
      </ElButton>
      <ElButton type="primary" :loading="submitting" @click="submit">
        保存新密码
      </ElButton>
    </template>
  </ElDialog>
</template>
