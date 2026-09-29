# 前端（Vue 3 · Fantastic-admin 基础版）

聚星同创仓库管理系统的界面基于 [Fantastic-admin 基础版](https://github.com/fantastic-admin/basic) v6.4.0（MIT）重构：
Vue 3 + TypeScript + Vite + Pinia + Vue Router，布局、导航、标签栏、工具栏来自框架，业务表单与表格使用 Element Plus。

构建产物直接写入仓库根目录的 `static/dist/`，由 Flask 在 `GET /` 提供。**部署服务器不需要 Node.js**：
`static/dist` 随程序一起提交与发布，修改前端后必须重新构建并提交构建产物。

## 目录

```text
frontend/
├── apps/web/                 # 应用（原 core-element-plus）
│   ├── src/api/              # 接口客户端与按领域划分的接口模块
│   ├── src/composables/      # scanner.ts：扫码枪焦点与按键捕获
│   ├── src/router/routes.ts  # 页面清单、角色权限、各角色导航分组
│   ├── src/store/            # 会话（account.ts）与框架状态
│   ├── src/views/            # 业务页面（路径与旧版视图标识一致）
│   └── vite/plugins.ts       # 含 pts-csp-guard：构建时拒绝违反服务端 CSP 的入口页
├── packages/                 # 框架内建组件、主题、设置（themes 已改为品牌绿）
└── e2e/                      # 无头浏览器冒烟脚本（playwright-core + 本机 Edge）
```

## 常用命令

```bash
pnpm install              # 安装依赖（国内网络可加 --registry=https://registry.npmmirror.com）
pnpm dev                  # 开发服务器 :9000，/api 代理到 127.0.0.1:5080
pnpm typecheck            # vue-tsc 类型检查
pnpm build                # 类型检查 + 构建到 ../static/dist
pnpm e2e                  # 冒烟测试（需先启动 python tools/dev_server.py）
```

本地联调可用 `python tools/dev_server.py --port 5080`：它使用临时数据库并写入演示数据，
不会触碰 `data/traceability.db`。

## 约定

- **角色与导航**：`src/router/routes.ts` 的 `PAGES` 声明每个页面允许的角色（`meta.auth`），`MENU_GROUPS` 声明各角色的导航分组。
  它与后端 `traceability/capabilities.py` 保持一致；后端仍是真正的鉴权方，前端只是不展示无权页面。
- **接口**：只通过 `src/api/modules/*` 调用。客户端统一处理信封 `{ok, data, message}`、`X-CSRF-Token`、
  `Idempotency-Key`（网络中断或 5xx 保留同一键重放，4xx 丢弃）、401 回登录页、428 强制改密。
- **现场写操作幂等**：批次生成、批次登记、采购单创建、生产订单批量生成、扫码枪入库都声明了幂等作用域，
  与服务端 `run_idempotent` 一一对应。
- **扫码页面**：使用 `composables/scanner.ts` 的 `useScanCapture` + `focusWithRetry`，
  保证进入页面、提交成功或失败后焦点都回到扫码框，主键盘与小键盘 Enter 都能提交。
- **CSP**：服务端策略为 `script-src 'self'; style-src 'self'`（外加 reka-ui 滚动区固定样式的哈希）。
  不要写 `v-html`、内联 `<script>`、模板里的静态 `style="..."`；需要样式时用 UnoCSS 类或 `assets/styles/pts.css`。
  图标只用 `i-集合:名称` 形式（构建时打包进 CSS），不要使用会在线拉取的 Iconify 名称。
- **视觉**：灰白中性底色；绿色只用于主操作、启用、合格与成功；琥珀色表示待检、低库存；红色表示暂扣、缺货与危险操作；不使用渐变和重阴影。
- **打印**：用 `utils/print.ts` 的 `printElement` 打印二维码标签，不要写入新窗口。

## 升级框架

框架代码保持在 `apps/web/src/layouts`、`packages/*` 中，业务代码集中在 `views/`、`api/`、`composables/`、`router/routes.ts`。
升级 Fantastic-admin 时对比上游的 `apps/core-element-plus` 与 `packages`，保留本仓库对
`vite.config.ts`、`vite/plugins.ts`、`router/guards.ts`、`store/modules/app/account.ts`、`packages/themes` 的修改。
