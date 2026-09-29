// 自定义指令
import directive from '@/utils/directive'

import App from './App.vue'
import router from './router'
import pinia from './store'
import uiProvider from './ui/provider'
import '@/utils/storage'

// UnoCSS
import 'virtual:uno.css'
// 全局样式
import '@/assets/styles/globals.css'
import '@/assets/styles/pts.css'

// 旧版界面使用 `#batch-entry` 形式的地址；新版哈希路由为 `#/batch-entry`。
// 在路由初始化之前改写，工位上收藏的旧书签可以直接打开对应页面。
const legacyHash = window.location.hash.match(/^#([a-z][a-z0-9-]*)$/)
if (legacyHash) {
  window.history.replaceState(window.history.state, '', `${window.location.pathname}${window.location.search}#/${legacyHash[1]}`)
}

const app = createApp(App)
app.use(pinia)
app.use(router)
app.use(uiProvider)
directive(app)

app.mount('#app')
