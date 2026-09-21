# API 契约（真实基线）

> 基线：共 **107 个路由**（`app.py` 100 个 + `traceability/auth.py` 7 个）。
> 路由级权限见 `PERMISSION_MATRIX.md`。
> 本文档描述**当前实际返回格式**，不是目标设计。

---

## 1. 响应信封

### 成功

```json
{ "ok": true, "data": <任意> }
```

由 `app.py:1719` 的 `success(data, status=200)` 产生。

### 失败

```json
{ "ok": false, "message": "中文错误描述" }
```

**只有这两个字段。** 没有 `code`、`details`、`requestId`（属第三十五目标）。
前端目前**通过解析中文字符串**判断错误类型。

---

## 2. 异常 → HTTP 状态映射

| 异常 | 状态码 | 处理器位置 |
| --- | --- | --- |
| `AuthError` | 由异常自带（401 / 403 / 404 / 409 / 428） | `auth.py:357` |
| `ApiError` | 由异常自带（默认 400） | `app.py:1686` |
| `ValueError` | 400 | `app.py:1690` |
| `BluetoothCollectionError` | 见实现 | `app.py:1694` |
| `LingxingError` | 见实现 | `app.py:1698` |
| `sqlite3.IntegrityError` | 见实现 | `app.py:1702` |
| `HTTPException` | 原状态码 | `app.py:1707` |
| 兜底 `Exception` | 500 | `app.py:1714` |

### 状态码语义（由测试实际断言统计）

| 码 | 出现次数 | 含义 |
| --- | --- | --- |
| 200 | 129 | 成功 |
| 201 | 86 | 创建成功（`POST` 建资源） |
| 409 | 31 | 冲突：重复码、状态机非法转换、账号重名、并发守卫占用 |
| 403 | 31 | 权限不足 |
| 400 | 25 | 参数校验失败 |
| 404 | 17 | 对象不存在 |
| 401 | 11 | 未登录 / 登录失效 |
| 502 | 3 | 领星上游返回错误 |
| 500 | 3 | 服务端异常 |
| 503 | 2 | 依赖不可用 |
| 428 | 1 | 首次登录必须先改密码 |

---

## 3. 认证与 CSRF（所有 `/api/` 通用）

闸门**只对 `/api/` 前缀生效**。非 `/api/` 路由（`GET /`、`GET /favicon.ico`）不受约束。

| 项 | 规则 |
| --- | --- |
| 豁免路径 | 仅 `/api/health`、`/api/auth/login`（共 4 个无需登录的路由） |
| 会话 | Flask session cookie，`PERMANENT_SESSION_LIFETIME = 12h` |
| 会话失效 | `users.session_version` 变化即失效（改密、改角色、改范围） |
| 首次改密 | `must_change_password = 1` 时除 `/api/auth/me`、`/logout`、`/change-password` 外一律 428 |
| CSRF | `POST/PUT/PATCH/DELETE` 必须带 `X-CSRF-Token`，与 session 内 token 恒定时间比对，否则 403 |
| CSRF 获取 | 登录响应或 `GET /api/auth/me` 的 `data.csrfToken` |

> 不存在匿名访问（除上述 4 个）。

### 认证与用户管理端点（注册在 `traceability/auth.py`，**不在** `app.py`）

| Method | Path | 有效访问 | 说明 |
| --- | --- | --- | --- |
| `POST` | `/api/auth/login` | public | 登录，返回 `csrfToken` |
| `GET` | `/api/auth/me` | 已登录 | 当前用户 + `csrfToken` |
| `POST` | `/api/auth/logout` | 已登录 | 清 session |
| `POST` | `/api/auth/change-password` | 已登录 | 改密后 `session_version + 1`，旧会话失效 |
| `GET` | `/api/users` | ADMIN | 用户列表 |
| `POST` | `/api/users` | ADMIN | 建用户，`must_change_password = 1` |
| `PUT` | `/api/users/<int:user_id>` | ADMIN | 改用户；改密/改角色/改范围时 `session_version + 1` |

> 这 7 个路由是本次审计发现的**遗漏点**：只扫描 `app.py` 的工具会完全看不到它们。

---

## 4. 分页现状

**绝大多数列表接口不分页，返回全量数据。**

目前仅 **1 个**接口支持分页：

```
GET /api/production-batches/<int:batch_id>/qr?page=N
→ { "ok": true, "data": { "items": [...], "page": N, ... } }
```

其余列表接口（`/api/products`、`/api/suppliers`、`/api/records`、`/api/purchase-orders`、
`/api/audit-events`、`/api/inbound-receipts` 等）**无 `limit` / `offset` / cursor 参数**。

> 这是第十六目标的主要风险点：数据量增长后这些接口会返回无限增长的结果集。

---

## 5. 导出接口

| Method | Path | 格式 |
| --- | --- | --- |
| `GET` | `/api/product-code-batches/<int:generation_batch_id>/qrcodes.zip` | ZIP |
| `GET` | `/api/products/<int:product_model_id>/qrcodes.zip` | ZIP |
| `GET` | `/api/product-code-sets/<int:code_set_id>/qrcodes.zip` | ZIP |
| `GET` | `/api/part-label-batches/<int:batch_id>/qrcodes.zip` | ZIP |
| `GET` | `/api/records/export.xlsx` | Excel |
| `GET` | `/api/purchase-orders/export` | Excel（领星 48 列模板） |
| `GET` | `/api/purchase-orders/<int:purchase_order_id>/export` | Excel |

> 这些接口**当前均在内存中构建完整文件**，未使用流式响应或临时文件策略（属第十六目标）。
> **`/api/records/export.xlsx` 无任何角色守卫**（仅需登录）。

---

## 6. 幂等性现状

**没有通用的 `idempotency_key` / request fingerprint 机制。**

现有的等价手段是**领域唯一键 + 状态守卫**：

| 场景 | 机制 |
| --- | --- |
| 领星采购推送 | `purchase_orders.sync_status = 'PUSHED'` 时直接返回成功并提示「已推送」 |
| 领星入库推送 | `inbound_receipts` 同上 |
| 并发推送 | `push_in_progress` 标记 + `push_started_at` 超时接管（v19） |
| 进程崩溃残留 | `_clear_abandoned_guards()` 启动时清除 |
| 库存同步 | `app_settings['inventory_sync.in_progress']` 守卫 |
| 批次码 / 产品主码 / 部件码 | 数据库唯一约束 |
| 批次登记 | 一个生产批次至多一条登记（唯一约束） |
| 一个采购订单至多一个生产订单 | 唯一约束 |

**缺口**：批次生成、批次登记、采购单创建、生产订单创建、扫码枪入库
**没有请求级幂等键**。客户端超时重试的确定性依赖上述领域唯一键，
若领域键不覆盖（例如扫码枪入库的连续数量提交），重试可能产生重复写入。
（属第五目标）

---

## 7. 领星集成契约

| 项 | 值 |
| --- | --- |
| 凭据 | `AppID` + `AppSecret`（无 `id` 字段） |
| 认证基址 | `https://openapi.lingxing.com` |
| 签名 | MD5 + AES-ECB |
| 采购下单 | `POST /erp/sc/routing/purchase/purchase/setOrders`，请求体 `{"order_sn": [...]}` |
| 成功判定 | `code: 0` 且 `data` 为空数组 |
| 失败判定 | `code: 1`（真实原因在 `data[].detail`，`message` 仍为 `success`）；或 `code: 0` 但 `data` 含拒绝明细 |
| 默认重试 | 最多 3 次，间隔 2s，超时 30s |
| 幂等 | 已 `PUSHED` 直接返回，不重复写远端 |
| 写接口未配置时 | 在外部请求前**安全中止**，不会把本地单据误标为已同步 |

> **⚠️ 安全问题（属第八目标）**：`clean_lingxing_endpoint()` 与
> `LingxingIntegrationService._endpoint_url()` 当前**允许管理员输入任意 http(s) URL**，
> 未限制 host、未拒绝私网地址、未禁止 HTTP。存在 SSRF 风险。
> 生产环境**必须**通过 `PTS_LINGXING_*_URL` 显式配置且只填领星官方路径。

---

## 8. 兼容性规则（已发布 API 不得破坏）

1. **不得无版本直接删除字段**；只允许新增兼容字段。
2. 破坏性修改未来使用 `/api/v2` 前缀，**不复制整个代码库**。
3. 范围授权字段 `productModelIds` / `supplierIds` 虽已无鉴权效果，
   但**必须继续返回**（前端与既有调用方依赖）。
4. 错误信封 `{ok, message}` 必须保持；新增 `code` / `details` / `requestId` 只能作为**附加字段**。
5. 历史 `OPERATOR` 角色在 v14 已映射为 `WAREHOUSE`；数据库 CHECK 约束会拒绝新的 `OPERATOR`。

### ⚠️ 已发生的有意授权变更（2026-09-21）

以下 4 个路由的**授权结果发生变化**：`OPERATIONS` 由「可访问」变为 `403`。
不是 bug 修复的副作用，而是把后端对齐到前端 `allowedViews()` 既有策略。

| Method | Path | 变更前 | 变更后 |
| --- | --- | --- | --- |
| `GET` | `/api/records` | 任何已登录 | ADMIN + WAREHOUSE |
| `PUT` | `/api/records/<int:record_id>` | 任何已登录 | ADMIN + WAREHOUSE |
| `DELETE` | `/api/records/<int:record_id>` | 任何已登录 | ADMIN + WAREHOUSE |
| `POST` | `/api/scan` | 任何已登录 | ADMIN + WAREHOUSE |

影响评估：`OPERATIONS` 前端从未渲染 `my-records` / `scan-gun` 视图，
因此**正常 UI 流程不受影响**；只有直接调用 API 的运营账号会从「静默成功」变为 403。

其余 8 个原本只挂空操作范围守卫的路由（追溯 / 族谱 / 二维码读取）
**行为不变**，只是补挂了显式的 `TRACE_VIEW` 能力，使意图可见。

---

## 9. 重新生成路由清单

```bash
python tools/extract_routes.py --json > routes.json
```
