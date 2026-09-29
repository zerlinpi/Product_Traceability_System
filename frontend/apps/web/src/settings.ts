import { setSettings } from '@fantastic-admin/settings'

/**
 * Framework settings (see packages/settings/src/types.ts for every option).
 *
 * - Permissions are on: a route is reachable only when the logged-in role is
 *   listed in its `meta.auth`.
 * - Hash routing, so Flask serves one entry page and bookmarks such as
 *   `/#/batch-entry` keep working without server-side rewrites.
 * - No home page: `/` lands on the first page of the role's navigation.
 * - The menu mode is switched per role by the router guard (ADMIN: side,
 *   others: single).
 */
export default setSettings({
  app: {
    account: {
      auth: true,
    },
    routeMode: 'hash',
    routeBaseOn: 'frontend',
    dynamicTitle: true,
    home: {
      enable: false,
      title: '首页',
      fullPath: '/',
    },
    copyright: {
      enable: false,
    },
  },
  theme: {
    colorScheme: 'light',
    radius: 0.5,
  },
  menu: {
    mode: 'side',
    mainMenuClickMode: 'smart',
    subMenuUniqueExpand: true,
    subMenuCollapseButton: true,
  },
  topbar: {
    tabbar: true,
    toolbar: true,
    mode: 'fixed',
  },
  tabbar: {
    icon: true,
  },
  toolbar: {
    breadcrumb: true,
    menuSearch: {
      enable: true,
      hotkeys: true,
    },
    fullscreen: true,
    pageReload: true,
    colorScheme: true,
  },
  page: {
    progress: true,
  },
})
