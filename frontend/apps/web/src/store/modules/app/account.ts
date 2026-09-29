/**
 * Session state.
 *
 * The backend authenticates with an HttpOnly session cookie, so nothing
 * secret is kept in localStorage: on every page load the session is probed
 * once with GET /api/auth/me. The CSRF token handed out by login / me /
 * change-password is held in memory only and attached to every write by the
 * API client.
 *
 * Roles are exclusive (ADMIN / WAREHOUSE / OPERATIONS). The framework's
 * permission list is simply `[role]`, and every route declares the roles it
 * accepts in `meta.auth` — the same policy as the backend capability matrix
 * (traceability/capabilities.py), which remains the real enforcement.
 */
import type { Role, SessionUser } from '@/api/types'
import { ApiError, registerSessionHooks } from '@/api'
import authApi from '@/api/modules/auth'
import { ROLE_LABELS } from '@/api/types'
import router from '@/router'

export const useAppAccountStore = defineStore('appAccount', () => {
  const appSettingsStore = useAppSettingsStore()
  const appTabbarStore = useAppTabbarStore()
  const appRouteStore = useAppRouteStore()
  const appMenuStore = useAppMenuStore()

  const user = ref<SessionUser | null>(null)
  const csrfToken = ref('')
  /** Whether the session has been probed since the page loaded. */
  const sessionChecked = ref(false)
  /** The account must change its initial password before anything else. */
  const passwordChangeRequired = ref(false)
  /** The password dialog is open (forced or voluntary). */
  const passwordDialogVisible = ref(false)
  /** Message shown on the login page after an unexpected logout. */
  const loginNotice = ref('')

  const isLogin = computed(() => user.value !== null)
  const role = computed<Role | ''>(() => user.value?.role ?? '')
  const roleLabel = computed(() => (role.value ? ROLE_LABELS[role.value] : '未登录'))
  /** Display name, as used by the framework's account button. */
  const account = computed(() => user.value?.displayName || user.value?.username || '')
  const username = computed(() => user.value?.username ?? '')
  const avatar = ref('')
  const permissions = computed<string[]>(() => (role.value ? [role.value] : []))
  const isAdmin = computed(() => role.value === 'ADMIN')
  const isWarehouse = computed(() => role.value === 'WAREHOUSE')
  const isOperations = computed(() => role.value === 'OPERATIONS')

  function setUser(next: SessionUser | null) {
    user.value = next
    csrfToken.value = next?.csrfToken || (next ? csrfToken.value : '')
    passwordChangeRequired.value = Boolean(next?.mustChangePassword)
    if (passwordChangeRequired.value) {
      passwordDialogVisible.value = true
    }
  }

  /** Probe the session once per page load. Resolves to whether a user is logged in. */
  async function restoreSession() {
    if (sessionChecked.value) {
      return isLogin.value
    }
    try {
      setUser(await authApi.me())
    }
    catch (error) {
      setUser(null)
      if (!(error instanceof ApiError && error.status === 401)) {
        loginNotice.value = '系统服务暂时不可用，请稍后重试'
      }
    }
    finally {
      sessionChecked.value = true
    }
    return isLogin.value
  }

  async function login(data: { username: string, password: string }) {
    const next = await authApi.login(data)
    loginNotice.value = ''
    // A new account may have different roles: rebuild menus and routes.
    resetNavigation()
    setUser(next)
    sessionChecked.value = true
    return next
  }

  async function changePassword(data: { currentPassword: string, newPassword: string }) {
    const next = await authApi.changePassword(data)
    setUser(next)
    passwordDialogVisible.value = false
    return next
  }

  function openPasswordDialog() {
    passwordDialogVisible.value = true
  }

  function closePasswordDialog() {
    if (!passwordChangeRequired.value) {
      passwordDialogVisible.value = false
    }
  }

  function resetNavigation() {
    appSettingsStore.updateSettings({}, true)
    appTabbarStore.clean()
    appRouteStore.removeRoutes()
    appMenuStore.setActived(0)
  }

  function clearSession() {
    user.value = null
    csrfToken.value = ''
    passwordChangeRequired.value = false
    passwordDialogVisible.value = false
    resetNavigation()
  }

  /** Voluntary logout from the account menu. */
  async function logout() {
    try {
      await authApi.logout()
    }
    catch {
      // Local cleanup still happens; the server session expires on its own.
    }
    clearSession()
    await router.push({ name: 'login' })
  }

  /** The server answered 401: the session is gone (expired, revoked, or password changed elsewhere). */
  function handleUnauthorized(message = '') {
    if (!isLogin.value && router.currentRoute.value.name === 'login') {
      return
    }
    const redirect = router.currentRoute.value.name !== 'login' ? router.currentRoute.value.fullPath : undefined
    clearSession()
    loginNotice.value = message || '登录状态已失效，请重新登录'
    router.push({ name: 'login', query: redirect && redirect !== '/' ? { redirect } : {} })
  }

  registerSessionHooks({
    csrfToken: () => csrfToken.value,
    onUnauthorized: handleUnauthorized,
    onPasswordChangeRequired: () => {
      passwordChangeRequired.value = true
      passwordDialogVisible.value = true
    },
  })

  return {
    user,
    csrfToken,
    sessionChecked,
    passwordChangeRequired,
    passwordDialogVisible,
    loginNotice,
    isLogin,
    role,
    roleLabel,
    account,
    username,
    avatar,
    permissions,
    isAdmin,
    isWarehouse,
    isOperations,
    setUser,
    restoreSession,
    login,
    changePassword,
    openPasswordDialog,
    closePasswordDialog,
    logout,
    handleUnauthorized,
  }
})
