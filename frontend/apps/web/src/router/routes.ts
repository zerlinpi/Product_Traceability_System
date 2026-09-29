import type { RouteRecordMainRaw } from '@fantastic-admin/types'
import type { RouteComponent, RouteRecordRaw } from 'vue-router'
import type { Role } from '@/api/types'

function Layout() {
  return import('@/layouts/index.vue')
}

// 固定路由（默认路由）
const constantRoutes: RouteRecordRaw[] = [
  {
    path: '/login',
    name: 'login',
    component: () => import('@/views/login.vue'),
    meta: {
      title: '登录',
    },
  },
  {
    path: '/:all(.*)*',
    name: 'notFound',
    component: () => import('@/views/[...all].vue'),
    meta: {
      title: '找不到页面',
    },
  },
]

// 系统路由。主页已关闭（settings.app.home.enable = false），访问 / 时守卫会跳转到
// 当前角色导航中的第一个页面：管理员 → 数据概览，仓管 → 批次生成，运营 → 我的产品。
const systemRoutes: RouteRecordRaw[] = [
  {
    path: '/',
    component: Layout,
    meta: {
      breadcrumb: false,
    },
    children: [
      {
        path: '',
        component: () => import('@/views/index.vue'),
        meta: {
          title: '首页',
          breadcrumb: false,
        },
      },
      {
        path: 'reload',
        name: 'reload',
        component: () => import('@/views/reload.vue'),
        meta: {
          title: '重新加载中...',
          breadcrumb: false,
        },
      },
    ],
  },
]

// ---------------------------------------------------------------------------
// 业务页面
//
// 路径与旧版界面的视图标识一致（#batch-entry → #/batch-entry），现场收藏的
// 书签和工位快捷方式在升级后仍然有效。每个页面都声明允许访问的角色（meta.auth），
// 与后端能力矩阵（traceability/capabilities.py）保持一致；后端仍是真正的鉴权方。
// ---------------------------------------------------------------------------

type PageKey
  = | 'dashboard'
    | 'products'
    | 'suppliers'
    | 'users'
    | 'batch-gen'
    | 'batch-entry'
    | 'batch-trace'
    | 'batch-quality'
    | 'trace'
    | 'my-records'
    | 'purchase-orders'
    | 'inbound-receipts'
    | 'production-orders'
    | 'scan-gun'
    | 'inventory-sync'
    | 'settings'

interface PageDefinition {
  title: string
  icon: string
  roles: Role[]
  component: () => Promise<RouteComponent>
  /** Extra child routes (detail pages) that highlight this page's menu entry. */
  details?: { path: string, name: string, title: string, component: () => Promise<RouteComponent> }[]
  /** Per-role menu title, e.g. 运营看到「我的产品」. */
  titleFor?: Partial<Record<Role, string>>
}

/** The single place that says which role may open which page. */
export const PAGES: Record<PageKey, PageDefinition> = {
  'dashboard': {
    title: '数据概览',
    icon: 'i-ri:dashboard-3-line',
    roles: ['ADMIN'],
    component: () => import('@/views/dashboard/index.vue'),
  },
  'products': {
    title: '产品管理',
    titleFor: { OPERATIONS: '我的产品' },
    icon: 'i-ri:box-3-line',
    roles: ['ADMIN', 'OPERATIONS'],
    component: () => import('@/views/products/index.vue'),
    details: [
      { path: ':id(\\d+)', name: 'product-detail', title: '产品详情', component: () => import('@/views/products/detail.vue') },
    ],
  },
  'suppliers': {
    title: '供应商',
    icon: 'i-ri:building-2-line',
    roles: ['ADMIN'],
    component: () => import('@/views/suppliers/index.vue'),
    details: [
      { path: ':id(\\d+)', name: 'supplier-detail', title: '供应商详情', component: () => import('@/views/suppliers/detail.vue') },
    ],
  },
  'users': {
    title: '用户管理',
    icon: 'i-ri:team-line',
    roles: ['ADMIN'],
    component: () => import('@/views/users/index.vue'),
  },
  'batch-gen': {
    title: '批次生成',
    icon: 'i-ri:qr-code-line',
    roles: ['ADMIN', 'WAREHOUSE'],
    component: () => import('@/views/batch-gen/index.vue'),
    details: [
      { path: ':id(\\d+)', name: 'batch-gen-detail', title: '批次详情', component: () => import('@/views/batch-gen/detail.vue') },
    ],
  },
  'batch-entry': {
    title: '现场批次登记',
    icon: 'i-ri:scan-2-line',
    roles: ['ADMIN', 'WAREHOUSE'],
    component: () => import('@/views/batch-entry/index.vue'),
  },
  'batch-trace': {
    title: '扫码查询',
    icon: 'i-ri:search-eye-line',
    roles: ['ADMIN', 'WAREHOUSE', 'OPERATIONS'],
    component: () => import('@/views/batch-trace/index.vue'),
  },
  'batch-quality': {
    title: '批次质量处理',
    icon: 'i-ri:shield-check-line',
    roles: ['ADMIN'],
    component: () => import('@/views/batch-quality/index.vue'),
  },
  'trace': {
    title: '产品查询',
    icon: 'i-ri:git-merge-line',
    roles: ['ADMIN'],
    component: () => import('@/views/trace/index.vue'),
  },
  'my-records': {
    title: '批次登记记录',
    icon: 'i-ri:file-list-3-line',
    roles: ['ADMIN', 'WAREHOUSE'],
    component: () => import('@/views/my-records/index.vue'),
  },
  'purchase-orders': {
    title: '采购订单',
    icon: 'i-ri:shopping-cart-2-line',
    roles: ['ADMIN', 'OPERATIONS'],
    component: () => import('@/views/purchase-orders/index.vue'),
  },
  'inbound-receipts': {
    title: '供应收货',
    icon: 'i-ri:truck-line',
    roles: ['ADMIN', 'WAREHOUSE'],
    component: () => import('@/views/inbound-receipts/index.vue'),
  },
  'production-orders': {
    title: '生产订单',
    icon: 'i-ri:building-4-line',
    roles: ['ADMIN', 'WAREHOUSE'],
    component: () => import('@/views/production-orders/index.vue'),
  },
  'scan-gun': {
    title: '扫码枪入库',
    icon: 'i-ri:barcode-box-line',
    roles: ['ADMIN', 'WAREHOUSE'],
    component: () => import('@/views/scan-gun/index.vue'),
  },
  'inventory-sync': {
    title: '库存同步',
    icon: 'i-ri:refresh-line',
    roles: ['ADMIN', 'OPERATIONS'],
    component: () => import('@/views/inventory-sync/index.vue'),
  },
  'settings': {
    title: '系统设置',
    icon: 'i-ri:settings-3-line',
    roles: ['ADMIN'],
    component: () => import('@/views/settings/index.vue'),
  },
}

function pageTitle(key: PageKey, role: Role) {
  const page = PAGES[key]
  return page.titleFor?.[role] ?? page.title
}

function pageRoute(key: PageKey, role: Role): RouteRecordRaw {
  const page = PAGES[key]
  const title = pageTitle(key, role)
  const path = `/${key}`
  return {
    path,
    component: Layout,
    meta: {
      title,
      icon: page.icon,
      auth: page.roles,
    },
    children: [
      {
        path: '',
        name: key,
        component: page.component,
        meta: {
          title,
          menu: false,
          breadcrumb: false,
          auth: page.roles,
        },
      },
      ...(page.details ?? []).map(detail => ({
        path: detail.path,
        name: detail.name,
        component: detail.component,
        meta: {
          title: detail.title,
          menu: false,
          activeMenu: path,
          auth: page.roles,
        },
      })),
    ],
  }
}

interface MenuGroup {
  title: string
  icon: string
  pages: PageKey[]
}

/** Navigation per role, in the order the old sidebar used. */
const MENU_GROUPS: Record<Role, MenuGroup[]> = {
  ADMIN: [
    { title: '运营中心', icon: 'i-ri:dashboard-3-line', pages: ['dashboard'] },
    { title: '基础资料', icon: 'i-ri:database-2-line', pages: ['products', 'suppliers', 'users'] },
    { title: '批次管理', icon: 'i-ri:qr-code-line', pages: ['batch-gen', 'batch-entry', 'batch-trace', 'batch-quality'] },
    { title: '历史记录', icon: 'i-ri:history-line', pages: ['trace', 'my-records'] },
    { title: '采购生产', icon: 'i-ri:shopping-cart-2-line', pages: ['purchase-orders', 'inbound-receipts', 'production-orders', 'scan-gun', 'inventory-sync'] },
    { title: '系统', icon: 'i-ri:settings-3-line', pages: ['settings'] },
  ],
  WAREHOUSE: [
    { title: '仓库作业', icon: 'i-ri:store-2-line', pages: ['batch-gen', 'batch-entry', 'inbound-receipts', 'production-orders', 'scan-gun', 'batch-trace', 'my-records'] },
  ],
  OPERATIONS: [
    { title: '运营协同', icon: 'i-ri:line-chart-line', pages: ['products', 'purchase-orders', 'batch-trace', 'inventory-sync'] },
  ],
}

/** Pages a role may open, in navigation order (the first one is the landing page). */
export function allowedPages(role: Role | ''): PageKey[] {
  if (!role) {
    return []
  }
  return MENU_GROUPS[role].flatMap(group => group.pages)
}

/** Route tree (navigation groups) registered after login for the given role. */
export function asyncRoutesFor(role: Role | ''): RouteRecordMainRaw[] {
  if (!role) {
    return []
  }
  return MENU_GROUPS[role].map(group => ({
    meta: {
      title: group.title,
      icon: group.icon,
    },
    children: group.pages.map(key => pageRoute(key, role)),
  }))
}

export {
  constantRoutes,
  systemRoutes,
}
