# 权限矩阵（真实行为基线）

> **本文档是机器生成的，不是手写的规范。**
> 路由表由 `python tools/extract_routes.py --sync` 从源码直接提取并回写。
> 修改路由或守卫后必须重新运行该命令，否则本文档即失效。

基线版本：共 **107 个路由**，来自**两个**注册点：

| 源文件 | 路由数 | 内容 |
| --- | --- | --- |
| `app.py` | 100 | 业务路由 |
| `traceability/auth.py`（`initialize_auth()`） | 7 | `/api/auth/*`、`/api/users` |

> **审计陷阱**：只扫描 `app.py` 会漏掉 7 个鉴权与用户管理路由。
> 本次审计的第一版提取器就犯了这个错（同时漏掉了 service 层守卫，见 D5）。

数据库 `user_version = 19`。

## 0. 能力表（Capability Matrix）

角色判断的**唯一权威来源**是 `traceability/capabilities.py`：

```python
class Capability(str, Enum):
    PRODUCT_MANAGE, SUPPLIER_MANAGE, USER_MANAGE, SETTINGS_MANAGE,
    INVENTORY_MANAGE, LEGACY_QR_MANAGE, BATCH_GENERATE, BATCH_REGISTER,
    LEGACY_SCAN, QUALITY_RELEASE, RECEIPT_CREATE, PRODUCTION_ORDER_CREATE,
    FINISHED_GOODS_INBOUND, PURCHASE_MANAGE, LINGXING_PUSH, INVENTORY_SYNC,
    OPERATIONS_PRODUCT_MANAGE, RECORD_VIEW, RECORD_EDIT, RECORD_DELETE,
    RECORD_ADMIN, TRACE_VIEW, DASHBOARD_VIEW, AUDIT_VIEW, BLUETOOTH_USE

ROLE_CAPABILITIES = {
    ADMIN:      _ADMIN_ONLY | _WAREHOUSE | _OPERATIONS | _SHARED,   # 25 项（超集）
    WAREHOUSE:  _WAREHOUSE | _SHARED,                               # 11 项
    OPERATIONS: _OPERATIONS | _SHARED,                              #  6 项
}
```

**ADMIN 持有全部能力**——这一点在表里显式表达，而不是在检查函数里特判。

该模块**不依赖 Flask**，因此静态工具（`tools/extract_routes.py`）与测试都能导入它，
从而保证本矩阵不会与代码再次脱节。

守卫函数：

```python
require_capability(*capabilities)   # 推荐；要求同时具备全部能力
require_admin()                     # 仅 ADMIN
require_warehouse()                 # ADMIN + WAREHOUSE（require_admin_or_warehouse 现为其别名）
require_operations()                # ADMIN + OPERATIONS
```

---

## 1. 鉴权实际发生在哪三层

这是理解本系统权限的关键。三层职责不同，**只有第二层区分角色**：

### 第一层：全局闸门（`traceability/auth.py` → `before_request`）

**只对 `/api/` 前缀生效**，仅 `/api/health` 与 `/api/auth/login` 豁免。

| 检查 | 失败结果 |
| --- | --- |
| session 有 `user_id` 且账号 `active = 1` | 401 登录状态已失效 |
| `session.auth_version == users.session_version` | 401 账号凭据已更新 |
| `must_change_password = 0`（仅豁免 `/api/auth/me`、`/logout`、`/change-password`） | 428 首次登录必须先修改密码 |
| `X-CSRF-Token` 与 session 中 token 恒定时间比对（仅 `POST/PUT/PATCH/DELETE`） | 403 页面安全令牌已失效 |

**结论：不存在匿名访问。** 除 health/login 外，任何 `/api/` 请求都必须是已登录且已改密的活跃账号。
非 `/api/` 路由（`GET /`、`GET /favicon.ico`）**不经过**该闸门。

共 **4 个路由**无需登录：`GET /`、`GET /favicon.ico`、`GET /api/health`、`POST /api/auth/login`。

### 第二层：角色守卫（区分 ADMIN / WAREHOUSE / OPERATIONS）

```python
require_admin()               # 仅 ADMIN
require_warehouse()           # ADMIN + WAREHOUSE
require_admin_or_warehouse()  # ADMIN + WAREHOUSE  ← 与上一行完全等价
require_operations()          # ADMIN + OPERATIONS
```

> **✅ D1 已修复：`require_warehouse()` 与 `require_admin_or_warehouse()` 现为同一个函数**
> 两者此前是两份实现相同的代码（都判断 `role not in {ADMIN, WAREHOUSE}`）。
> 现已合并为一份实现加一个别名，无法再各自漂移。
> 注意语义：**不存在「仅仓管、不含管理员」的权限级别**——ADMIN 通过所有角色守卫。

### 第三层：范围守卫（**当前全部为空操作**）

```python
current_operator_id()          # 恒返回 None（auth.py）
require_product_model_access() # user_id is None → 直接 return，不检查
require_supplier_access()      # user_id is None → 直接 return，不检查
```

`current_operator_id()` 的注释明确说明这是**有意设计**：仓管不再按产品/供应商授权，
改为可操作全部产品。**副作用**是这两个范围守卫变成空操作，且范围表不再拦任何请求。

> **✅ D2 已修复**：这 12 个路由现已全部改挂显式 `require_capability(...)`，
> **不再有任何路由只依赖空操作守卫**。
> 范围守卫调用本身保留（API 兼容脚手架），但已不是任何路由的唯一防线。
> `tests/test_permission_matrix.py::test_no_route_relies_solely_on_a_noop_scope_guard`
> 会持续守住这一点。

### 第四层（本批次新增）：能力守卫

`require_capability(Capability.X)` 查表判定，策略集中在 `traceability/capabilities.py`。
下表 `Capability` 列显示每个路由要求的能力。

---

## 2. 有效访问级别词汇表

| 表中写法 | 实际含义 |
| --- | --- |
| `public (no login)` | 完全不需要登录（仅 4 个） |
| `ADMIN` | 仅管理员 |
| `WAREHOUSE` | 管理员 + 仓管（**注意：不是「仅仓管」**） |
| `OPERATIONS` | 管理员 + 运营（**注意：不是「仅运营」**） |
| `any authenticated` | 任何已登录账号，不区分角色 |
| `scope:product(NOOP)` | 调用了产品范围守卫，但因 `current_operator_id()` 返回 `None` 而**必然放行** |
| `scope:supplier(NOOP)` | 同上，供应商范围守卫 |
| Guard location = `handler` | 守卫写在路由函数体内 |
| Guard location = `service: ...` | 守卫写在被调用的 service 函数内 |

### 总量分布

| 类别 | 数量 | 说明 |
| --- | --- | --- |
| 总路由 | **107** | |
| 完全公开（无需登录） | **4** | `/`、`/favicon.ico`、`/api/health`、`/api/auth/login` |
| 无任何角色守卫（仅需登录） | **27** | |
| **仅靠空操作范围守卫** | **0** | ✅ 已修复（原为 12） |
| 显式能力守卫 | **12** | 由 `require_capability` 保护 |
| 守卫位于 service 层 | **10** | 含 4 个由幂等包装拆出的生产者函数 |
| 写操作路由 | **42** | |

---

## 3. 已记录的冲突与偏差

以下均为**经源码核实**的事实，不是推测。
✅ 表示本批次已修复；⚠️ 表示仍然存在，属后续批次。

| 编号 | 状态 | 问题 |
| --- | --- | --- |
| D1 | ✅ 已修复 | 两个角色守卫实现相同 → 合并为一份实现 + 别名 |
| D2 | ✅ 已修复 | 12 个路由仅靠空操作范围守卫 → 补挂显式能力 |
| D3 | ⚠️ 部分修复 | 越权面已关闭；「归属权」规则仍未实现，待业务确认 |
| D4 | ⚠️ 待处理 | 范围授权表仍写入并回显，但不参与鉴权 |
| D5 | ⚠️ 待处理 | 鉴权分散在 handler 与 service 两层 |
| D6 | ✅ 已修正 | 文档称仓管只见分配产品（实为全部） |
| D7 | ✅ 已修正 | 文档数据库版本漂移（15 / 14 → 19） |
| D8 | ℹ️ 事实 | 部署库停在 v18，代码链已到 19 |

### ✅ D1 — 两个角色守卫完全等价（已合并）
见第 1 节。

### ✅ D2 — 范围守卫失效导致的「实际仅需登录」已修复

`current_operator_id()` 恒返回 `None`，导致 `require_product_model_access()` 与
`require_supplier_access()` 空转。修复前，12 个路由**只**依赖这两个空操作守卫，
因此任何已登录账号（含运营）都能访问，其中 3 个是写操作。

**修复方式**：为这 12 个路由补挂显式 `require_capability(...)`。
范围守卫调用保留（兼容脚手架），但不再是任何路由的唯一防线。

| Method | Path | 新增能力 | 修复后的有效访问 |
| --- | --- | --- | --- |
| `POST` | `/api/batch-trace/query` | `TRACE_VIEW` | 全体角色（行为不变，意图显式化） |
| `GET` | `/api/genealogy` | `TRACE_VIEW` | 全体角色（行为不变） |
| `GET` | `/api/machines/<int:machine_id>/qr` | `TRACE_VIEW` | 全体角色（行为不变） |
| `GET` | `/api/part-label-batches/<int:batch_id>` | `TRACE_VIEW` | 全体角色（行为不变） |
| `GET` | `/api/part-label-batches/<int:batch_id>/qrcodes.zip` | `TRACE_VIEW` | 全体角色（行为不变） |
| `GET` | `/api/part-labels/<int:label_id>/qr` | `TRACE_VIEW` | 全体角色（行为不变） |
| `GET` | `/api/production-batches/<int:batch_id>` | `TRACE_VIEW` | 全体角色（行为不变） |
| `GET` | `/api/production-batches/<int:batch_id>/qr` | `TRACE_VIEW` | 全体角色（行为不变） |
| `GET` | `/api/records` | `RECORD_VIEW` | **ADMIN + WAREHOUSE**（运营被拒） |
| `PUT` | `/api/records/<int:record_id>` | `RECORD_EDIT` | **ADMIN + WAREHOUSE**（运营被拒） |
| `DELETE` | `/api/records/<int:record_id>` | `RECORD_DELETE` | **ADMIN + WAREHOUSE**（运营被拒） |
| `POST` | `/api/scan` | `LEGACY_SCAN` | **ADMIN + WAREHOUSE**（运营被拒） |

**4 个路由的行为发生了有意变更**（运营失去访问）：`GET /api/records`、
`PUT`/`DELETE /api/records/<id>`、`POST /api/scan`。

依据不是猜测，而是前端 `allowedViews()`（`static/app_v2.js:227`）：
`my-records` 只出现在 ADMIN 与 WAREHOUSE 的视图列表，OPERATIONS 没有；
`scan-gun` 同样只在 ADMIN 与 WAREHOUSE。后端此前与前端策略不一致，现已对齐。

### ✅ D3 — `editable_record()` 归属权检查仍为死代码，但已不再构成越权

`editable_record()`（`app.py`）中：

```python
operator_id = current_operator_id()
if operator_id is not None and row["completed_by_user_id"] != operator_id:
    raise ApiError("只能修改或删除自己的录入记录", 403)
```

`operator_id` 恒为 `None`，该分支**永不进入**，错误消息不可达。
即：**没有**「只能改自己的记录」这条规则，任何有权限的角色都能改任意记录。

D2 修复后，`PUT`/`DELETE /api/records/<id>` 已在入口处按能力拒绝运营，
**越权面已关闭**；但「归属权」这一层仍未实现。是否要按 `completed_by_user_id`
限制到录入人本人，属于**业务决策**，需要确认后再做——本批次**不做假设**。

> 待确认问题：仓管之间是否应当互相校对记录？
> 当前实现是「可以」（只要角色允许）。若业务要求「只能改自己录入的」，
> 则需让 `current_operator_id()` 恢复返回真实 user id，并同步调整
> `require_product_model_access()` 的语义。

### D3 — `editable_record()` 的归属权检查是死代码
`app.py:6170` `editable_record()` 中：

```python
operator_id = current_operator_id()
if operator_id is not None and row["completed_by_user_id"] != operator_id:
    raise ApiError("只能修改或删除自己的录入记录", 403)
```

`operator_id` 恒为 `None`，该分支**永不进入**，错误消息「只能修改或删除自己的录入记录」**不可达**。
因此 `PUT` / `DELETE /api/records/<id>` 对所有已登录角色开放，无归属权限制。

### D4 — 范围授权表仍被写入并对外暴露，但不再拦任何请求
`user_product_model_permissions` / `user_supplier_permissions` 两张表：

- 仍在 `POST/PUT /api/users` 中被**写入**（`_replace_user_scope()`）
- 仍通过 `user_dict()` 以 `productModels` / `productModelIds` / `suppliers` / `supplierIds` **返回给前端**
- 但**不再参与任何鉴权判断**

即：管理员界面上「给仓管分配产品」的操作会持久化并回显，但**不产生任何权限效果**。

### D5 — 鉴权分散在 handler 与 service 两层，无统一规律
**10 个路由**的守卫在被调用的函数内，而非路由函数体内：

| Method | Path | 守卫所在函数 |
| --- | --- | --- |
| `POST` | `/api/batch-trace-records/<int:record_id>/pass` | `transition_batch_quality` |
| `PUT` | `/api/records/<int:record_id>` | `editable_record` |
| `DELETE` | `/api/records/<int:record_id>` | `editable_record` |
| `POST` | `/api/purchase-orders/<int:purchase_order_id>/push` | `push_purchase_order_record` |
| `POST` | `/api/production-orders` | `_impl_create_production_order` |
| `POST` | `/api/production-orders/batch` | `_impl_create_production_orders_batch` |
| `POST` | `/api/production-batches` | `_impl_create_production_batch` |
| `POST` | `/api/batch-entry/scan` | `_impl_batch_entry_scan` |
| `POST` | `/api/purchase-orders` | `_impl_create_purchase_order` |
| `POST` | `/api/scan-gun/inbound` | `_impl_scan_gun_inbound` |

**典型对照**：`/pass` 与 `/hold` 是同一状态机的两个方向，`/hold` 把 `require_admin()` 写在
handler 里，`/pass` 写在 service 里。只看 handler 会误判 `/pass` 无守卫
（本次审计第一版提取器就产生了这个假阳性）。

后 6 个（`_impl_*`）是**幂等包装**引入的新形态：

```python
@app.post("/api/scan-gun/inbound")
def scan_gun_inbound():
    return run_idempotent("scan-gun.inbound", _impl_scan_gun_inbound)

def _impl_scan_gun_inbound():
    require_admin_or_warehouse()   # ← 守卫在这里
    ...
```

生产者函数是**作为参数传递**的，不是被调用，因此朴素的调用图跟踪会认为守卫消失了
（本次又踩了一次）。`tools/extract_routes.py` 为此加了 `IDEMPOTENT_WRAPPER_RE` 显式解析——
**没有**改用「裸标识符匹配」，那会过度报告权限，是更危险的方向。

> 这是本次审计最重要的方法论结论：**任何权限审计都必须跟踪调用图，
> 且必须覆盖「函数被传递」这种非调用形态。**

### D6 — 文档与实现冲突：仓管的可见产品范围

| 来源 | 描述 |
| --- | --- |
| `SYSTEM_ARCHITECTURE.md:46` | 仓管「**只看到管理员分配且启用的产品**」 |
| `SYSTEM_ARCHITECTURE.md:39` | 管理员「给仓管分配产品权限」 |
| `auth.py:132-141` 注释 | 「仓管处理**每一个**产品……范围检查一律按不受限处理，**与管理员完全一致**」 |
| `README.md` | 仓管「可对**全部产品**执行入库、批次生成、登记与查询」 |

**基线判定：以代码 + README 为准 —— 仓管可操作全部产品。**
`SYSTEM_ARCHITECTURE.md` 第 3 节的两处描述为**过时规范**，已在本批次修正。

### D7 — 文档数据库版本漂移

| 来源 | 声称 | 实际 |
| --- | --- | --- |
| `README.md:267` | 数据库结构版本为 **15** | **19** |
| `SYSTEM_ARCHITECTURE.md:175` | `user_version` 当前为 **14** | **19** |

已在本批次修正。

### D8 — 已部署数据库落后于代码一个迁移版本
`data/traceability.db` 实际为 **`user_version = 18`**，代码迁移链终点为 **19**。
下次服务启动会自动执行 v19（为 `purchase_orders` / `inbound_receipts` 增加
`push_started_at` 列）。v19 为纯增量迁移，不影响既有数据。

---

## 4. 完整路由表（生成）

<!-- BEGIN GENERATED ROUTE TABLE -->
| Method | Path | Effective access | Capability | Guard location | Writes |
| --- | --- | --- | --- | --- | --- |
| `GET` | `/` | public (no login) |  | handler |  |
| `GET` | `/api/audit-events` | ADMIN |  | handler |  |
| `POST` | `/api/auth/change-password` | any authenticated |  | handler | yes |
| `POST` | `/api/auth/login` | public (no login) |  | handler | yes |
| `POST` | `/api/auth/logout` | any authenticated |  | handler |  |
| `GET` | `/api/auth/me` | any authenticated |  | handler |  |
| `POST` | `/api/batch-entry/scan` | ADMIN + WAREHOUSE scope:product(NOOP) |  | service: _impl_batch_entry_scan, run_idempotent, success | yes |
| `GET` | `/api/batch-trace-records` | any authenticated |  | handler |  |
| `POST` | `/api/batch-trace-records/<int:record_id>/hold` | ADMIN |  | handler | yes |
| `POST` | `/api/batch-trace-records/<int:record_id>/pass` | ADMIN |  | service: success, transition_batch_quality | yes |
| `POST` | `/api/batch-trace/query` | any authenticated scope:product(NOOP) | `TRACE_VIEW` | handler |  |
| `POST` | `/api/bluetooth/discover` | any authenticated |  | handler |  |
| `POST` | `/api/bluetooth/read-sn` | any authenticated |  | handler |  |
| `GET` | `/api/bluetooth/status` | any authenticated |  | handler |  |
| `GET` | `/api/dashboard` | ADMIN |  | handler |  |
| `GET` | `/api/genealogy` | any authenticated scope:product+scope:supplier(NOOP) | `TRACE_VIEW` | handler |  |
| `GET` | `/api/health` | public (no login) |  | handler |  |
| `GET` | `/api/inbound-receipts` | any authenticated |  | handler |  |
| `POST` | `/api/inbound-receipts` | ADMIN + WAREHOUSE |  | handler | yes |
| `GET` | `/api/inbound-receipts/<int:receipt_id>` | any authenticated |  | handler |  |
| `POST` | `/api/inbound-receipts/<int:receipt_id>/push` | ADMIN + OPERATIONS |  | handler | yes |
| `GET` | `/api/inbound-receipts/<int:receipt_id>/sync-status` | ADMIN + OPERATIONS |  | handler |  |
| `GET` | `/api/inbound-scan-records` | ADMIN + WAREHOUSE |  | handler |  |
| `GET` | `/api/inventory-sync` | ADMIN + OPERATIONS |  | handler |  |
| `POST` | `/api/inventory-sync` | ADMIN + OPERATIONS |  | handler | yes |
| `GET` | `/api/lingxing/status` | ADMIN + OPERATIONS |  | handler |  |
| `GET` | `/api/machines` | any authenticated |  | handler |  |
| `POST` | `/api/machines` | ADMIN scope:product(NOOP) |  | handler | yes |
| `GET` | `/api/machines/<int:machine_id>/qr` | any authenticated scope:product(NOOP) | `TRACE_VIEW` | handler |  |
| `GET` | `/api/part-label-batches` | any authenticated |  | handler |  |
| `GET` | `/api/part-label-batches/<int:batch_id>` | any authenticated scope:supplier(NOOP) | `TRACE_VIEW` | handler |  |
| `GET` | `/api/part-label-batches/<int:batch_id>/qrcodes.zip` | any authenticated scope:supplier(NOOP) | `TRACE_VIEW` | handler |  |
| `GET` | `/api/part-labels` | any authenticated |  | handler |  |
| `POST` | `/api/part-labels` | ADMIN scope:supplier(NOOP) |  | handler | yes |
| `PUT` | `/api/part-labels/<int:label_id>` | ADMIN scope:supplier(NOOP) |  | handler | yes |
| `GET` | `/api/part-labels/<int:label_id>/qr` | any authenticated scope:supplier(NOOP) | `TRACE_VIEW` | handler |  |
| `GET` | `/api/part-types` | any authenticated |  | handler |  |
| `POST` | `/api/part-types` | ADMIN |  | handler | yes |
| `PUT` | `/api/part-types/<int:part_type_id>` | ADMIN |  | handler |  |
| `GET` | `/api/product-attribute-columns` | any authenticated |  | handler |  |
| `GET` | `/api/product-code-batches` | ADMIN |  | handler |  |
| `GET` | `/api/product-code-batches/<int:generation_batch_id>` | ADMIN |  | handler |  |
| `GET` | `/api/product-code-batches/<int:generation_batch_id>/qrcodes.zip` | ADMIN |  | handler |  |
| `GET` | `/api/product-code-sets` | ADMIN scope:product(NOOP) |  | handler |  |
| `GET` | `/api/product-code-sets/<int:code_set_id>/qrcodes.zip` | ADMIN |  | handler |  |
| `GET` | `/api/product-families` | any authenticated |  | handler |  |
| `POST` | `/api/product-families` | ADMIN |  | handler | yes |
| `PUT` | `/api/product-families/<int:family_id>` | ADMIN |  | handler | yes |
| `POST` | `/api/product-images` | ADMIN + OPERATIONS |  | handler |  |
| `GET` | `/api/product-images/<path:filename>` | any authenticated |  | handler |  |
| `GET` | `/api/product-models` | any authenticated |  | handler |  |
| `POST` | `/api/product-models` | ADMIN |  | handler | yes |
| `DELETE` | `/api/product-models/<int:model_id>` | ADMIN + OPERATIONS |  | handler | yes |
| `PUT` | `/api/product-models/<int:model_id>` | ADMIN |  | handler | yes |
| `GET` | `/api/production-batches` | any authenticated |  | handler |  |
| `POST` | `/api/production-batches` | ADMIN + WAREHOUSE scope:product(NOOP) |  | service: _impl_create_production_batch, run_idempotent, success | yes |
| `GET` | `/api/production-batches/<int:batch_id>` | any authenticated scope:product(NOOP) | `TRACE_VIEW` | handler |  |
| `GET` | `/api/production-batches/<int:batch_id>/qr` | any authenticated scope:product(NOOP) | `TRACE_VIEW` | handler |  |
| `GET` | `/api/production-orders` | ADMIN + WAREHOUSE |  | handler |  |
| `POST` | `/api/production-orders` | ADMIN + WAREHOUSE scope:product(NOOP) |  | service: _impl_create_production_order, business_id, generate_production_order_ | yes |
| `GET` | `/api/production-orders/<int:production_order_id>` | ADMIN + WAREHOUSE scope:product(NOOP) |  | handler |  |
| `GET` | `/api/production-orders/<int:production_order_id>/qr` | ADMIN + WAREHOUSE scope:product(NOOP) |  | handler |  |
| `POST` | `/api/production-orders/batch` | ADMIN + WAREHOUSE scope:product(NOOP) |  | service: _impl_create_production_orders_batch, business_id, generate_production | yes |
| `GET` | `/api/products` | any authenticated |  | handler |  |
| `POST` | `/api/products` | ADMIN + OPERATIONS |  | handler | yes |
| `PUT` | `/api/products/<int:product_model_id>` | ADMIN + OPERATIONS |  | handler | yes |
| `POST` | `/api/products/<int:product_model_id>/code-sets` | ADMIN |  | handler | yes |
| `GET` | `/api/products/<int:product_model_id>/qrcodes.zip` | ADMIN |  | handler |  |
| `GET` | `/api/purchase-orders` | any authenticated |  | handler |  |
| `POST` | `/api/purchase-orders` | ADMIN + OPERATIONS |  | service: _impl_create_purchase_order, business_id, business_quantity, clean_pur | yes |
| `DELETE` | `/api/purchase-orders/<int:purchase_order_id>` | ADMIN + OPERATIONS |  | handler | yes |
| `GET` | `/api/purchase-orders/<int:purchase_order_id>` | any authenticated |  | handler |  |
| `PUT` | `/api/purchase-orders/<int:purchase_order_id>` | ADMIN + OPERATIONS |  | handler | yes |
| `GET` | `/api/purchase-orders/<int:purchase_order_id>/export` | ADMIN + OPERATIONS |  | handler |  |
| `GET` | `/api/purchase-orders/<int:purchase_order_id>/factory-progress` | ADMIN + OPERATIONS |  | handler |  |
| `POST` | `/api/purchase-orders/<int:purchase_order_id>/push` | ADMIN + OPERATIONS |  | service: ensure_lingxing_operation_ready, external_identifier, guard_is_stale,  | yes |
| `GET` | `/api/purchase-orders/<int:purchase_order_id>/sync-status` | ADMIN + OPERATIONS |  | handler |  |
| `GET` | `/api/purchase-orders/export` | ADMIN + OPERATIONS |  | handler |  |
| `GET` | `/api/records` | ADMIN + WAREHOUSE scope:product(NOOP) | `RECORD_VIEW` | handler |  |
| `DELETE` | `/api/records/<int:record_id>` | ADMIN + WAREHOUSE scope:product(NOOP) | `RECORD_DELETE` | service: editable_record, success | yes |
| `PUT` | `/api/records/<int:record_id>` | ADMIN + WAREHOUSE scope:product+scope:supplier(NOOP) | `RECORD_EDIT` | service: editable_record, success | yes |
| `PUT` | `/api/records/<int:record_id>/status` | ADMIN |  | handler | yes |
| `GET` | `/api/records/export.xlsx` | any authenticated |  | handler |  |
| `PUT` | `/api/records/status/bulk` | ADMIN |  | handler | yes |
| `POST` | `/api/scan` | ADMIN + WAREHOUSE scope:product+scope:supplier(NOOP) | `LEGACY_SCAN` | handler | yes |
| `POST` | `/api/scan-gun/inbound` | ADMIN + WAREHOUSE scope:product(NOOP) |  | service: _impl_scan_gun_inbound, business_id, business_quantity, production_ord | yes |
| `POST` | `/api/scan-gun/lookup` | ADMIN + WAREHOUSE scope:product(NOOP) |  | handler |  |
| `POST` | `/api/scan/reset` | any authenticated |  | handler | yes |
| `GET` | `/api/scan/session` | any authenticated |  | handler |  |
| `POST` | `/api/scan/undo` | any authenticated |  | handler | yes |
| `GET` | `/api/settings` | ADMIN |  | handler |  |
| `PUT` | `/api/settings` | ADMIN |  | handler | yes |
| `GET` | `/api/supplier-inventory-batches` | ADMIN |  | handler |  |
| `POST` | `/api/supplier-inventory-batches` | ADMIN |  | handler | yes |
| `PUT` | `/api/supplier-inventory-batches/<int:inventory_batch_id>` | ADMIN |  | handler | yes |
| `GET` | `/api/supplier-inventory-batches/<int:inventory_batch_id>/forward-trace` | ADMIN |  | handler |  |
| `GET` | `/api/supplier-inventory-batches/<int:inventory_batch_id>/movements` | ADMIN |  | handler |  |
| `GET` | `/api/suppliers` | any authenticated |  | handler |  |
| `POST` | `/api/suppliers` | ADMIN |  | handler | yes |
| `GET` | `/api/suppliers/<int:supplier_id>` | ADMIN |  | handler |  |
| `PUT` | `/api/suppliers/<int:supplier_id>` | ADMIN |  | handler |  |
| `GET` | `/api/trace-plans` | any authenticated |  | handler |  |
| `POST` | `/api/trace-plans` | ADMIN |  | handler | yes |
| `GET` | `/api/users` | ADMIN |  | handler |  |
| `POST` | `/api/users` | ADMIN |  | handler | yes |
| `PUT` | `/api/users/<int:user_id>` | ADMIN |  | handler | yes |
| `GET` | `/favicon.ico` | public (no login) |  | handler |  |
<!-- END GENERATED ROUTE TABLE -->

---

## 5. 重新生成

```bash
python tools/extract_routes.py            # 摘要 + 偏差清单
python tools/extract_routes.py --markdown # 仅打印表格
python tools/extract_routes.py --json     # 机器可读全量
python tools/extract_routes.py --sync     # 回写本文档的生成区块
```

`tools/extract_routes.py` 会构建 `create_app()` 内嵌套函数的调用图，
传递解析守卫（因此能正确识别 D5 中 6 个 service 层守卫）。
