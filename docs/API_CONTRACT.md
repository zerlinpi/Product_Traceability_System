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

## 6. 幂等性

### 6.1 请求级幂等键（现场写操作）

现场设备（扫码枪）或抖动局域网会把同一次提交投递两次：客户端超时、操作员重试，
而服务端已经提交。**UI 层的防双击无法阻止这一点**——重复请求是独立的 HTTP 请求。

**机制**：客户端携带幂等键，服务端首次执行并存储响应，后续同键请求**原样重放**。

| 项 | 值 |
| --- | --- |
| 键的来源 | `Idempotency-Key` 请求头，或 JSON body 的 `idempotencyKey` 字段 |
| 键长度 | 8–128 个可打印字符，**大小写敏感** |
| 作用域 | 每个端点独立（`scope`），同键不同端点互不影响 |
| 请求指纹 | 归一化 body 的 SHA-256；同键不同 body → **409** |
| 存储 | `idempotency_keys` 表（`user_version` v20） |
| 保留期 | 7 天（有界保留，`purge_expired`） |
| 并发重复 | 第二个请求得到 **409「同一请求正在处理中」**，**绝不重复执行** |
| 崩溃残留 | 启动时 `_clear_abandoned_idempotency_claims()` 清除 |
| 无键请求 | **行为与以前完全一致**（机制是 opt-in，老客户端不受影响） |

**已接入的端点**（`run_idempotent` 包装）：

| Method | Path | scope |
| --- | --- | --- |
| `POST` | `/api/scan-gun/inbound` | `scan-gun.inbound` |
| `POST` | `/api/batch-entry/scan` | `batch-entry.scan` |
| `POST` | `/api/production-batches` | `production-batches.create` |
| `POST` | `/api/purchase-orders` | `purchase-orders.create` |
| `POST` | `/api/production-orders` | `production-orders.create` |
| `POST` | `/api/production-orders/batch` | `production-orders.batch` |

**关键设计：先占位再执行，而不是先查询再执行。**
朴素的「查键 → 执行 → 存响应」存在竞态：两个并发重复请求都会查不到键、都会执行。
因此键行在业务逻辑**之前**插入（独立的 `BEGIN IMMEDIATE`），**插入本身就是互斥锁**。
这与领星推送已有的 `push_in_progress` + `push_started_at` 守卫是同一套恢复语义。

**失败处理**：
- 业务异常 / 非 2xx → **释放键**，客户端可用同一个键干净重试（事务已回滚，什么都没发生）。
- 存储响应失败 → 释放键（业务已提交，不能把成功变成报错）。
- 网络中断（客户端侧）→ 前端**保留**键，重试走重放而非重复执行。
- 4xx 明确拒绝 → 前端**清除**键，下一次是全新提交。

### 6.2 领域唯一键（原有的等价机制）

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

### 6.3 尚未接入幂等键的写接口

其余写接口（`PUT`/`DELETE` 类更新与删除、`POST /api/scan` 历史逐台扫码、
基础资料增删改）**暂无请求级幂等键**。
它们多为幂等的天然操作（`PUT` 同内容重复提交结果一致），或由唯一约束兜底；
`POST /api/scan` 仍建议后续接入。

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

### ✅ 出站地址策略（原 SSRF 风险已修复）

此前 `clean_lingxing_endpoint()` 接受**任意** `http(s)://` URL：管理员（或取得管理员会话的人）
可以把「推送领星」按钮指向 `http://169.254.169.254/latest/meta-data/`（云元数据）、
`http://127.0.0.1:5080/api/users`（本系统自己的 API）或工厂局域网内任意主机，
服务端会去请求并把响应回显出来。这是 SSRF 原语，不是配置失误。

现由 `traceability/endpoint_policy.py` 统一约束：

| 规则 | 说明 |
| --- | --- |
| 默认允许主机 | **仅** `openapi.lingxing.com` |
| 自定义 endpoint | 默认只接受**相对路径**（拼接到基础地址，无法改变主机） |
| 自定义主机 | 必须显式配置 `PTS_LINGXING_ALLOWED_HOSTS` 白名单 |
| 协议 | 仅 `http`/`https`；`file://`、`ftp://`、`data:` 等一律拒绝 |
| HTTP | 默认拒绝；`PTS_LINGXING_ALLOW_HTTP=1` 可开，但 `PTS_ENV=production` **强制关闭** |
| 内网地址 | 回环、链路本地、私有、保留、组播地址一律拒绝（含 IPv6） |
| 内部名称 | `localhost`、`*.localhost`、`*.local`、`*.internal`、`metadata` 等按名称拒绝 |
| DNS 解析 | 允许列表中的**域名**若解析到内网地址，同样拒绝（防 DNS 重绑定） |
| URL 用户名 | `https://openapi.lingxing.com@evil.com/` 这类构造被拒绝 |
| **重定向** | 每次跳转都重新校验；允许的主机返回 `302` 到内网**不会被跟随** |

策略在两处生效：

1. **保存时**（`clean_lingxing_endpoint()`）—— 给管理员即时反馈
2. **传输层**（`UrllibLingxingHttpClient`）—— 字节真正离开进程的地方，含重定向

> 策略拒绝被标记为 `retryable=False`，不会被重试逻辑反复尝试。
> 例外开关 `PTS_LINGXING_ALLOW_PRIVATE_HOSTS=1` 会**关闭全部内网检查**，仅限实验环境使用。

完整的 82 项策略测试见 `tests/test_ssrf_policy.py`。

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
