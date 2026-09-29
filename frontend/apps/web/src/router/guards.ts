import type { Router } from 'vue-router'
import { useNProgress } from '@vueuse/integrations/useNProgress'
import { asyncRoutesFor } from './routes'
import '@/assets/styles/nprogress.css'

function setupRoutes(router: Router) {
  router.beforeEach(async (to) => {
    const appSettingsStore = useAppSettingsStore()
    const appAccountStore = useAppAccountStore()
    const appRouteStore = useAppRouteStore()
    const appMenuStore = useAppMenuStore()
    // 每次页面加载先用会话 Cookie 探测一次登录状态（GET /api/auth/me）
    await appAccountStore.restoreSession()
    if (appAccountStore.isLogin && appAccountStore.passwordChangeRequired) {
      // 首次登录必须先改密：后端对其他接口一律返回 428，停留在登录页并由强制弹窗完成改密
      if (to.name !== 'login') {
        return {
          name: 'login',
          query: {
            redirect: to.fullPath !== appSettingsStore.settings.app.home.fullPath ? to.fullPath : undefined,
          },
        }
      }
      return
    }
    if (appAccountStore.isLogin) {
      // 是否已根据角色生成并注册路由
      if (appRouteStore.isGenerate) {
        // 导航菜单如果不是 single 模式，则需要根据 path 定位主导航菜单的选中状态
        appSettingsStore.settings.menu.mode !== 'single' && appMenuStore.setActived(to.path)
        // 如果已登录状态下，进入登录页会强制跳转到主页
        if (to.name === 'login') {
          return {
            path: appSettingsStore.settings.app.home.fullPath,
            replace: true,
          }
        }
        // 主页未开启：进入 / 时跳转到当前角色导航中的第一个页面
        else if (!appSettingsStore.settings.app.home.enable && to.fullPath === appSettingsStore.settings.app.home.fullPath && appMenuStore.sidebarMenus.length > 0) {
          return {
            path: appMenuStore.sidebarMenusFirstDeepestPath,
            replace: true,
          }
        }
      }
      else {
        // 管理员有多个导航分组，使用「主导航 + 次导航」；仓管与运营只有一个分组，使用单栏导航
        appSettingsStore.updateSettings({
          menu: {
            mode: appAccountStore.isAdmin ? 'side' : 'single',
          },
        })
        appRouteStore.generateRoutesAtFront(asyncRoutesFor(appAccountStore.role))
        // 注册并记录路由数据，登出时通过 router.addRoute() 返回的回调删除
        const removeRoutes: (() => void)[] = []
        appRouteStore.routes.forEach((route) => {
          if (!/^(?:https?:|mailto:|tel:)/.test(route.path)) {
            removeRoutes.push(router.addRoute(route))
          }
        })
        appRouteStore.systemRoutes.forEach((route) => {
          removeRoutes.push(router.addRoute(route))
        })
        appRouteStore.setCurrentRemoveRoutes(removeRoutes)
        // 动态路由生成并注册后，重新进入当前路由
        return {
          path: to.path,
          query: to.query,
          replace: true,
        }
      }
    }
    else {
      if (to.name !== 'login') {
        return {
          name: 'login',
          query: {
            redirect: to.fullPath !== appSettingsStore.settings.app.home.fullPath ? to.fullPath : undefined,
          },
        }
      }
    }
  })
}

// 当父级路由未配置重定向时，自动重定向到有访问权限的子路由
function setupRedirectAuthChildrenRoute(router: Router) {
  router.beforeEach((to) => {
    const { auth } = useAppAuth()
    const currentRoute = router.getRoutes().find(route => route.path === (to.matched.at(-1)?.path ?? ''))
    if (!currentRoute?.redirect) {
      const findAuthRoute = currentRoute?.children?.find(route => route.meta?.menu !== false && auth(route.meta?.auth ?? ''))
      if (findAuthRoute) {
        return findAuthRoute
      }
    }
  })
}

// 进度条
function setupProgress(router: Router) {
  const { isLoading } = useNProgress()
  router.beforeEach(() => {
    const appSettingsStore = useAppSettingsStore()
    if (appSettingsStore.settings.page.progress) {
      isLoading.value = true
    }
  })
  router.afterEach(() => {
    const appSettingsStore = useAppSettingsStore()
    if (appSettingsStore.settings.page.progress) {
      isLoading.value = false
    }
  })
}
// 标题
function setupTitle(router: Router) {
  router.afterEach((to) => {
    const appSettingsStore = useAppSettingsStore()
    appSettingsStore.setTitle(to.matched?.at(-1)?.meta?.title ?? to.meta.title)
  })
}

// 页面保活
function setupKeepAlive(router: Router) {
  router.afterEach(async (to, from) => {
    const appKeepAliveStore = useAppKeepAliveStore()
    if (to.meta.keepAlive) {
      const componentName = to.matched.at(-1)?.components?.default.name
      if (componentName) {
        // 保活当前页面前，先判断是否需要清除保活，判断依据：
        // 1. 如果 to.meta.keepAlive 为 boolean 类型，并且不为 true，则需要清除保活
        // 2. 如果 to.meta.keepAlive 为 string 类型，并且与 from.name 不一致，则需要清除保活
        // 3. 如果 to.meta.keepAlive 为 array 类型，并且不包含 from.name，则需要清除保活
        // 4. 如果 to.meta.noKeepAlive 为 string 类型，并且与 from.name 一致，则需要清除保活
        // 5. 如果 to.meta.noKeepAlive 为 array 类型，并且包含 from.name，则需要清除保活
        // 6. 如果是刷新页面，则需要清除保活
        let shouldClear = false
        if (typeof to.meta.keepAlive === 'boolean') {
          shouldClear = !to.meta.keepAlive
        }
        else if (typeof to.meta.keepAlive === 'string') {
          shouldClear = to.meta.keepAlive !== from.name
        }
        else if (Array.isArray(to.meta.keepAlive)) {
          shouldClear = !to.meta.keepAlive.includes(from.name as string)
        }
        if (to.meta.noKeepAlive) {
          if (typeof to.meta.noKeepAlive === 'string') {
            shouldClear = to.meta.noKeepAlive === from.name
          }
          else if (Array.isArray(to.meta.noKeepAlive)) {
            shouldClear = to.meta.noKeepAlive.includes(from.name as string)
          }
        }
        if (from.name === 'reload') {
          shouldClear = true
        }
        if (shouldClear) {
          appKeepAliveStore.remove(componentName)
          await nextTick()
        }
        appKeepAliveStore.add(componentName)
      }
    }
  })
}

// 其他
function setupOther(router: Router) {
  router.afterEach(() => {
    document.documentElement.scrollTop = 0
  })
}

export default function setupGuards(router: Router) {
  setupRoutes(router)
  setupRedirectAuthChildrenRoute(router)
  setupProgress(router)
  setupTitle(router)
  setupKeepAlive(router)
  setupOther(router)
}
