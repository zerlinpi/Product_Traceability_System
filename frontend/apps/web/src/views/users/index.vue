<script setup lang="ts">
/**
 * 用户管理（管理员）— accounts, their role and status.
 *
 * Roles are exclusive (管理员 / 仓管 / 运营). Warehouse accounts operate every
 * product (per-product scoping is disabled on the server), so the scope column
 * is fixed per role and the form has no product picker. New accounts and
 * password resets must change the password at next login. An administrator
 * cannot deactivate or demote the account they are signed in with.
 */
import type { FormInstance, FormRules } from 'element-plus'
import type { UserUpdatePayload } from '@/api/modules/auth'
import type { AnyRecord, Role, SessionUser } from '@/api/types'
import authApi from '@/api/modules/auth'
import { ROLE_LABELS } from '@/api/types'
import { confirmAction, notifyError, notifySuccess } from '@/utils/feedback'

defineOptions({
  name: 'UserListPage',
})

const appAccountStore = useAppAccountStore()

const ROLE_OPTIONS: { value: Role, label: string }[] = [
  { value: 'WAREHOUSE', label: ROLE_LABELS.WAREHOUSE },
  { value: 'OPERATIONS', label: ROLE_LABELS.OPERATIONS },
  { value: 'ADMIN', label: ROLE_LABELS.ADMIN },
]

/** 权限范围 per role (fixed: warehouse operates every product). */
const ROLE_SCOPES: Record<Role, { title: string, detail: string }> = {
  ADMIN: { title: '全系统权限', detail: '系统配置与全业务访问' },
  OPERATIONS: { title: '运营业务', detail: '采购、领星同步与工厂进度' },
  WAREHOUSE: { title: '仓储作业', detail: '全部产品入库、批次与生产订单' },
}

const loading = ref(false)
const users = ref<SessionUser[]>([])
const togglingId = ref<number | null>(null)

/** Table rows are the SessionUser payloads of GET /api/users. */
function isCurrent(user: AnyRecord) {
  return user.id !== null && user.id !== undefined && user.id === appAccountStore.user?.id
}

function roleScope(role: unknown) {
  return ROLE_SCOPES[role as Role] ?? ROLE_SCOPES.WAREHOUSE
}

function roleLabel(role: unknown) {
  return ROLE_LABELS[role as Role] ?? String(role ?? '')
}

async function load() {
  loading.value = true
  try {
    users.value = await authApi.users()
  }
  catch (error) {
    notifyError(error, '账号列表加载失败')
  }
  finally {
    loading.value = false
  }
}

async function toggleUser(user: AnyRecord) {
  if (!user.id || isCurrent(user) || togglingId.value !== null) {
    return
  }
  if (user.active) {
    const confirmed = await confirmAction(
      `确定停用账号「${user.displayName || user.username}」吗？停用后该账号会立即退出且无法登录，可随时重新启用。`,
      { title: '停用账号', confirmText: '停用', danger: true },
    )
    if (!confirmed) {
      return
    }
  }
  togglingId.value = Number(user.id)
  try {
    await authApi.setUserActive(Number(user.id), !user.active)
    notifySuccess('账号状态已更新')
    await load()
  }
  catch (error) {
    notifyError(error, '账号状态更新失败')
  }
  finally {
    togglingId.value = null
  }
}

// ---- user form ---------------------------------------------------------------

const dialogVisible = ref(false)
const editing = ref<AnyRecord | null>(null)
const formRef = ref<FormInstance>()
const nameInput = ref<{ focus: () => void }>()
const saving = ref(false)
const form = reactive({
  displayName: '',
  username: '',
  role: 'WAREHOUSE' as Role,
  password: '',
})

const isEdit = computed(() => editing.value !== null)
const editingSelf = computed(() => editing.value !== null && isCurrent(editing.value))

const rules: FormRules = {
  displayName: [
    { required: true, whitespace: true, message: '请输入姓名', trigger: 'blur' },
    { max: 60, message: '姓名不能超过 60 个字符', trigger: 'blur' },
  ],
  username: [
    { required: true, whitespace: true, message: '请输入账号', trigger: 'blur' },
    { pattern: /^\s*[A-Z0-9][\w.-]{0,49}\s*$/i, message: '账号需为 1-50 位字母、数字、点、下划线或短横线', trigger: 'blur' },
  ],
  role: [{ required: true, message: '请选择账号角色', trigger: 'change' }],
  password: [
    {
      validator: (_rule, value, callback) => {
        const text = String(value ?? '')
        if (!text && !isEdit.value) {
          callback(new Error('请输入初始密码'))
        }
        else if (text && (text.length < 8 || text.length > 128)) {
          callback(new Error('密码长度需为 8-128 个字符'))
        }
        else if (text && text.toLowerCase() === (isEdit.value ? editing.value?.username : form.username.trim())?.toLowerCase()) {
          callback(new Error('密码不能与账号相同'))
        }
        else {
          callback()
        }
      },
      trigger: 'blur',
    },
  ],
}

function openUserModal(user: AnyRecord | null = null) {
  editing.value = user
  form.displayName = user?.displayName ?? ''
  form.username = user?.username ?? ''
  form.role = (user?.role as Role | undefined) ?? 'WAREHOUSE'
  form.password = ''
  dialogVisible.value = true
  nextTick(() => formRef.value?.clearValidate())
}

function focusName() {
  nameInput.value?.focus()
}

async function submitUser() {
  if (!formRef.value || saving.value) {
    return
  }
  const valid = await formRef.value.validate().catch(() => false)
  if (!valid) {
    return
  }
  const user = editing.value
  saving.value = true
  try {
    if (user?.id) {
      const body: UserUpdatePayload = {
        displayName: form.displayName.trim(),
        role: form.role,
      }
      if (form.password) {
        body.password = form.password
      }
      const saved = await authApi.updateUser(Number(user.id), body)
      // Keep the account button in sync when administrators rename themselves.
      if (isCurrent(user) && appAccountStore.user) {
        appAccountStore.setUser({ ...appAccountStore.user, displayName: saved.displayName })
      }
    }
    else {
      await authApi.createUser({
        displayName: form.displayName.trim(),
        username: form.username.trim(),
        password: form.password,
        role: form.role,
      })
    }
    dialogVisible.value = false
    notifySuccess(user ? '账号已更新' : '账号已添加')
    await load()
  }
  catch (error) {
    notifyError(error, '账号保存失败')
  }
  finally {
    saving.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="pts-page">
    <FaPageHeader title="用户管理" description="创建管理员、仓管或运营账号；每个账号只属于一个角色，仓管可操作全部产品">
      <ElButton id="open-user-modal" type="primary" @click="openUserModal()">
        <template #icon>
          <FaIcon name="i-ri:user-add-line" />
        </template>
        添加账号
      </ElButton>
    </FaPageHeader>
    <FaPageMain>
      <template #title>
        <span class="text-foreground font-medium">账号列表</span>
        <span class="text-xs ml-2">管理员统一设置账号角色与启用状态</span>
      </template>
      <ElTable id="user-table" v-loading="loading" :data="users" row-key="id" empty-text="尚未添加账号" stripe>
        <ElTableColumn label="姓名" min-width="150">
          <template #default="{ row }">
            <span class="pts-cell-main">{{ row.displayName }}</span>
            <span v-if="isCurrent(row)" class="pts-cell-sub">当前登录账号</span>
          </template>
        </ElTableColumn>
        <ElTableColumn label="登录账号" min-width="130">
          <template #default="{ row }">
            <span class="pts-code">{{ row.username }}</span>
          </template>
        </ElTableColumn>
        <ElTableColumn label="角色" width="100">
          <template #default="{ row }">
            <ElTag :type="row.role === 'ADMIN' ? 'primary' : 'info'" disable-transitions>
              {{ roleLabel(row.role) }}
            </ElTag>
          </template>
        </ElTableColumn>
        <ElTableColumn label="权限范围" min-width="200">
          <template #default="{ row }">
            <span class="pts-cell-main">{{ roleScope(row.role).title }}</span>
            <span class="pts-cell-sub">{{ roleScope(row.role).detail }}</span>
          </template>
        </ElTableColumn>
        <ElTableColumn label="状态" width="90">
          <template #default="{ row }">
            <ElTag :type="row.active ? 'success' : 'info'" disable-transitions>
              {{ row.active ? '启用' : '停用' }}
            </ElTag>
          </template>
        </ElTableColumn>
        <ElTableColumn label="操作" width="150" align="right" fixed="right">
          <template #default="{ row }">
            <ElButton link type="primary" @click="openUserModal(row)">
              编辑
            </ElButton>
            <ElButton v-if="isCurrent(row)" link disabled>
              当前账号
            </ElButton>
            <ElButton v-else link :type="row.active ? 'danger' : 'primary'" :loading="togglingId === row.id" @click="toggleUser(row)">
              {{ row.active ? '停用' : '启用' }}
            </ElButton>
          </template>
        </ElTableColumn>
      </ElTable>
    </FaPageMain>

    <ElDialog id="user-modal" v-model="dialogVisible" :title="isEdit ? '编辑账号' : '添加账号'" width="480px" :close-on-click-modal="false" append-to-body @opened="focusName">
      <p class="pts-muted text-sm mt-0 mb-4">
        每个账号只属于一个角色；仓管可操作全部产品，无需逐个授权
      </p>
      <ElForm id="user-form" ref="formRef" :model="form" :rules="rules" label-position="top" @submit.prevent="submitUser">
        <ElFormItem label="姓名" prop="displayName">
          <ElInput ref="nameInput" v-model="form.displayName" name="displayName" maxlength="60" />
        </ElFormItem>
        <ElFormItem label="登录账号" prop="username">
          <ElInput v-model="form.username" name="username" maxlength="50" autocomplete="off" :disabled="isEdit" />
        </ElFormItem>
        <ElFormItem label="账号角色" prop="role">
          <ElSelect v-model="form.role" name="role" :disabled="editingSelf" class="w-full">
            <ElOption v-for="option in ROLE_OPTIONS" :key="option.value" :label="option.label" :value="option.value" />
          </ElSelect>
          <span class="pts-muted text-xs mt-1 block">管理员维护系统；仓管负责扫码、收货与入库；运营负责采购与领星同步。</span>
        </ElFormItem>
        <ElFormItem :label="isEdit ? '重置密码（可不填）' : '初始密码'" prop="password" :required="!isEdit">
          <ElInput v-model="form.password" name="password" type="password" show-password maxlength="128" autocomplete="new-password" />
          <span class="pts-muted text-xs mt-1 block">至少 8 个字符；设置或重置后，该账号下次登录时需要修改密码</span>
        </ElFormItem>
      </ElForm>
      <template #footer>
        <ElButton @click="dialogVisible = false">
          取消
        </ElButton>
        <ElButton type="primary" :loading="saving" @click="submitUser">
          保存账号
        </ElButton>
      </template>
    </ElDialog>
  </div>
</template>
