# 聚星同创仓库管理系统：产品与逻辑架构

本文档描述当前版本的业务边界、角色权限、核心数据链路、状态规则、事务约束和后续兼容方向。它与 `README.md` 的部署说明互补，主要用于产品评审、开发维护和现场验收。

> **配套基线文档**（机器提取，非手写规范）：
> - `docs/PERMISSION_MATRIX.md` — 全部 107 个路由的真实权限矩阵与已知偏差
> - `docs/DATA_MODEL.md` — v11→v19 迁移链与 33 张表的实际结构
> - `docs/API_CONTRACT.md` — 响应信封、状态码语义、幂等性与兼容规则
> - `docs/PRODUCT_RULES.md` — 业务规则基线，含文档与代码的冲突记录
>
> 凡本文档与代码冲突，**以代码 + 测试为准**，并应在 `docs/PRODUCT_RULES.md` 记录冲突。

## 1. 产品定位

当前系统定位为单工厂、局域网部署的轻量 MES/QMS 溯源工作台，解决以下核心问题：

- 供应商部件按批次收货并记录可用库存；
- 产品定义绑定供应商、部件、供应批次和每套用量；
- 走步机按生产批次生成唯一二维码并一次性登记，历史逐台主码与实物部件码继续兼容；
- 仓管可对全部产品完成批次生成、登记、生产订单与扫码枪入库；
- 管理员按产品、批次、单件进行正反向追溯；
- 录入记录从待检进入合格或暂扣，并保留处理原因和审计；
- 运营维护采购订单、领星推送、采购单导出、库存同步与工厂进度；
- 关键库存、质量异常和近期操作集中显示在运营工作台。

当前版本不试图替代完整 ERP、APS、WMS 或实验室 QMS。工单、工序、抽样方案、不合格品处置和跨仓库调拨应在现有主链稳定后分阶段扩展。

## 2. 同类系统参考与取舍

本轮参考以下开源项目和官方产品文档，并只吸收与当前规模匹配的模式：

- [InvenTree](https://github.com/inventree/InvenTree)：列表筛选、对象详情下钻、构建订单进度和库存调整历史；当前系统据此强化产品/供应商筛选、供应批次详情和库存流水。
- [ERPNext](https://github.com/frappe/erpnext)：制造看板中的数字指标、进行中任务和质量分析；当前系统据此把数据概览升级为可处理的运营工作台，而不是只展示累计数字。
- [OCA Manufacture](https://github.com/OCA/manufacture)：通用质量检查基础、批次传播和制造变更跟踪；当前系统据此建立轻量的待检、合格、暂扣状态闭环。
- [OpenBoxes](https://github.com/openboxes/openboxes)：库存异常、待审批事项和可下钻数字卡；当前系统据此增加缺货/低库存预警，并让预警直接进入供应商详情。
- [Microsoft Dynamics 365 Item Tracing](https://learn.microsoft.com/en-us/dynamics365/supply-chain/inventory/trace-items-raw-materials-inventory-production-sales)：基于批次、序列号和库存事务的前后向追溯；当前系统继续以唯一主码、部件码、供应批次和库存流水作为族谱依据。

明确未照搬的内容包括：可自由拖拽看板、复杂工作中心、成本核算、多级审批、完整采购销售链。它们会显著增加配置和培训成本，不符合当前现场录入优先的阶段。

## 3. 角色和权限

账号使用单值 `role`，最终角色集合恰好为 `{ADMIN, WAREHOUSE, OPERATIONS}`；一个账号不能同时持有仓管和运营角色。历史 `OPERATOR` 在 v14 迁移时映射为 `WAREHOUSE`。

**角色到权限的映射集中在 `traceability/capabilities.py`**：`Capability` 枚举定义「能做什么」，
`ROLE_CAPABILITIES` 定义「哪个角色能做什么」，路由通过 `require_capability(Capability.X)` 判权。
`ADMIN` 持有全部能力。该模块不依赖 Flask，因此 `tools/extract_routes.py` 与测试都能直接导入，
保证 `docs/PERMISSION_MATRIX.md` 不会与代码脱节。**注意：ADMIN 通过所有角色守卫**——
系统中不存在「仅仓管、不含管理员」的权限级别。

### 管理员（ADMIN）

- 维护产品、供应商、部件、供应批次、安全库存和产品用料；
- 创建或编辑管理员、仓管、运营账号。仓管账号表单仍可填写产品范围并会持久化回显，但**该范围当前不参与鉴权**（详见下方仓管小节与 `docs/PERMISSION_MATRIX.md`）；
- 处理批次质量状态、查看完整追溯、库存流水与审计；
- 配置通用设置和脱敏保存的领星凭据；
- 可进入仓管和运营页面排障，但普通角色不能反向获得管理权限。

### 仓管（WAREHOUSE）

- **可操作全部产品**（不再按管理员分配范围限制）。生成与登记生产批次、查询任意产品溯源；
- 创建供应收货、由采购订单生成生产订单；
- 扫描生产二维码并登记成品入库数量；
- 不能创建供应商、产品、账号、采购订单，不能质量放行或同步领星。

> **规范漂移修正记录**：本节此前写为「只看到管理员分配且启用的产品」。
> 实现自 v14 起已改为仓管可操作全部产品 —— `traceability/auth.py` 中
> `current_operator_id()` 恒返回 `None`，使 `require_product_model_access()` /
> `require_supplier_access()` 成为空操作。范围授权表仍被写入并回显，但不再拦任何请求。
> 该漂移的完整记录与影响面见 `docs/PERMISSION_MATRIX.md` 的 D2 / D4。

### 运营（OPERATIONS）

- 创建、查询和推送采购订单；
- 推送仓管已建立的入库收货记录；
- 导出领星采购单、查看工厂进度并同步成品库存；
- 可执行只读扫码溯源查询；
- 不能维护基础资料、生成生产批次或登记成品入库。

## 4. 核心数据链

```text
供应商 → 供应部件 → 供应库存批次 → 产品用料（每套用量）
                                  ├→ 生产批次（唯一批次码）
                                  │    └→ 批次登记 → 质量状态 → 正/反向追溯
                                  └→ 历史逐台二维码套件（兼容只读/非走步机）

采购订单 → 供应收货 → 领星入库推送
    └→ 生产订单（唯一生产二维码）→ 扫码枪入库 → 成品库存 → 领星库存同步
```

### 关键对象

| 对象 | 主要职责 | 关键约束 |
| --- | --- | --- |
| `suppliers` | 供应商主数据 | 供应商编码唯一 |
| `part_types` | 供应部件、规格、安全库存 | 部件编码唯一，安全库存非负 |
| `supplier_inventory_batches` | 供应批次入库与结存 | 同一部件下批次号唯一，可用量非负 |
| `supplier_inventory_movements` | 入库、领用、调整流水 | 每次变动记录变动量和变动后结存 |
| `product_models` | 可录入产品 | 产品型号编码唯一 |
| `trace_plans` / `trace_plan_slots` | 产品部件顺序和供应批次快照 | 一个启用计划对应固定顺序 |
| `production_batches` | 走步机生产批次 | 批次码全局唯一，计划台数 `1..999999` |
| `production_batch_supplier_consumption` | 生产批次与供应批次消耗关联 | 同部件消耗总量守恒 |
| `batch_trace_records` | 批次一次性登记与质量状态 | 一个生产批次至多一条登记 |
| `purchase_orders` / `inbound_receipts` | 采购与供应收货、领星同步状态 | 推送幂等、状态与审计持久化 |
| `production_orders` | 采购订单生成的生产订单 | 一个采购订单至多一个生产订单和生产二维码 |
| `inbound_scan_records` / `product_stock` | 扫码枪成品入库与当前库存 | 入库记录与库存累加同事务 |
| `app_settings` | 通用设置和领星凭据 | 机密字段只脱敏回显 |
| `product_code_batches` | 一次二维码生成任务 | 保存数量、流水范围、操作人和时间 |
| `product_code_sets` | 一套产品二维码 | 产品主码全局唯一 |
| `part_labels` | 单个实物部件码 | 部件码全局唯一 |
| `trace_records` | 一次完成的产品装配记录 | 一个产品主码只能归档一次 |
| `trace_record_parts` | 实际装配部件顺序 | 一个部件码只能归属一个成品 |
| `audit_events` | 关键业务审计 | 事件号唯一，记录操作者和时间 |

## 5. 业务流程

### 5.1 供应与库存

1. 管理员创建供应商。
2. 在供应商详情创建一个或多个供应部件，并设置规格和安全库存。
3. 为部件登记到货批次，形成 `RECEIPT` 流水。
4. 生成生产批次或兼容产品二维码时，系统按“计划台数 × 每套用量”扣减指定供应批次，并形成 `ISSUE` 流水。
5. 管理员修改到货数量时，只允许新入库数量不小于累计领用量；结存差异形成 `ADJUSTMENT` 流水。
6. 部件所有启用批次合计结存为 0 时显示缺货；大于 0 且不高于安全库存时显示低库存。

库存扣减、二维码生成和套件绑定必须处于同一个数据库事务。任何部件不足时整次生成失败，不允许出现部分扣减或半批二维码。

### 5.2 产品配置与生产批次

1. 管理员创建产品并按实际装配顺序添加部件。
2. 每个部件位置选择供应商、供应部件、到货批次和每套数量。
3. 走步机由管理员或授权仓管输入自定义前缀和计划台数（`1..999999`），每个生产批次只生成一个唯一二维码。
4. 生成事务同时扣减供应库存、写 `ISSUE` 流水和供应批次消耗关联；任何一步失败整批回滚。
5. 历史逐台流程与非走步机仍按固定扫码位置生成产品主码和部件码，单次最多 1000 套，可下载单套、单批或产品历史全部二维码。

### 5.3 现场扫码与成品入库

1. 仓管扫描生产批次二维码后一次性登记整批，缺省登记台数等于计划台数，可修正为 `1..planned_quantity`。
2. 重复登记、无效码、未授权产品均拒绝且无数据副作用；登记后质量状态为 `ASSEMBLED`。
3. 扫描生产订单二维码时先检索订单和产品，再填写入库数量；记录与 `product_stock` 累加同事务。
4. 扫码枪 Enter 自动提交；查询成功后焦点进入数量框，确认入库或查询失败后焦点回到二维码框。
5. 兼容逐台记录修改时，扫码只写入用户当前点击的部件输入框，回车后自动移到下一个部件。

### 5.4 记录修改与质量闭环

录入记录初始状态为 `ASSEMBLED`（待检）：

```text
待检 ASSEMBLED ──合格确认──> 合格 PASSED
      │
      └──发现问题──> 暂扣 HOLD ──仓管校对修改兼容记录──> 待检 ASSEMBLED
```

- 暂扣必须填写原因；
- 合格可填写检验依据或说明；
- 管理员可以把记录退回待检；
- 仓管修改兼容逐台部件码后，记录自动回到待检并清空原暂扣原因；
- 每次状态变化和记录修改均写入审计事件；
- 删除记录仍要求填写原因，删除后该套二维码可重新录入。

**归属权**：仓管只能修改或删除**自己录入的**记录（`completed_by_user_id`）；
管理员不受限；运营无 `RECORD_EDIT` / `RECORD_DELETE` 能力，进不来。
无主的历史记录仓管不可改，管理员可清理。

> 这条规则曾在 `editable_record()` 中**被静默关掉**：守卫写成
> `if current_operator_id() is not None`，而该函数为停用产品/供应商范围限制
> 对**所有角色**恒返回 `None`。**两个无关策略共用一个 helper，改一个关掉了另一个。**
> 现已改为直接读角色，详见 `SECURITY.md` §「记录归属权」与
> `tests/test_record_ownership.py`。

当前状态闭环属于轻量 QMS。后续增加检验项目、抽样方案和质检员角色时，应保留这些状态作为归档层兼容字段。

## 6. 管理工作台与信息架构

管理员侧边栏按“运营中心、基础资料、批次溯源、历史追溯、采购与生产”分组，用户管理只在侧边菜单和对应页面出现。仓管侧边栏仅保留批次生成、现场批次登记、供应收货、生产订单、扫码枪入库、扫码溯源和授权范围内的历史记录；运营侧边栏仅保留采购订单、扫码溯源与库存同步。现场页面不展示管理指标、基础资料维护或其他非当前任务内容。

## 7. UI 设计约束

- 以灰白中性色作为大面积背景和容器；
- 绿色只用于主操作、启用、合格和成功反馈；
- 琥珀色只用于待检、低库存；红色只用于暂扣、缺货和危险操作；
- 不使用渐变、大面积绿色光晕、装饰性插画或重阴影；
- 页面层级依靠标题、留白、边框、表格密度和状态标签建立；
- 列表先筛选再下钻，详情页提供与当前对象相关的操作；
- 所有核心按钮、筛选、表单、扫码和状态操作必须真实可用。

## 8. 数据一致性与并发

- SQLite 开启外键、WAL 和忙等待；写入使用显式事务；
- 主码、部件码、套件号、溯源单号和审计事件号均由唯一约束兜底；
- 生成二维码前在事务内重新检查批次结存；
- 同一成品或部件的重复扫码由会话占用和数据库唯一约束双重防护；
- 工位会话按浏览器工位标识和登录账号隔离；
- 所有数量修改必须保留库存流水，不允许直接覆盖历史领用事实；
- 历史产品码保存生成时的产品计划和部件绑定，后续产品编辑不回写历史记录。

## 9. API 兼容约定

- 现有 `/api/products`、`/api/suppliers`、`/api/part-types`、`/api/records` 路径继续保留；新增字段采用向后兼容的 JSON 属性。
- `part_types.minimum_stock` 默认值为 0，旧数据库升级后不立即产生低库存阈值，只在结存为 0 时提示缺货。
- `trace_records.status_reason`、`status_updated_at` 和 `status_updated_by_user_id` 均允许旧记录为空。
- `PUT /api/records/{id}/status` 是管理员单条质量处理入口，`PUT /api/records/status/bulk` 支持最多 200 条记录同事务批量处理；`PUT /api/records/{id}` 支持仓管校对授权范围内的兼容记录，也支持管理员校对全部记录。
- `GET /api/records` 兼容原有参数，并增加 `generationBatchId`、`dateFrom`、`dateTo` 作为可选筛选条件；不修改历史数据结构。
- 数据库 `user_version` 当前为 22。v12 增加批次溯源，v13 增加领星采购 / 入库结构，v14 收敛三角色并增加设置、生产订单和成品库存，v15 重建采购单并加入产品关联，v16 放宽批次计划为可空，v17 增加外采标记与库存同步表，v18 补建热点索引，v19 增加推送守卫起始时间，v20 增加幂等键表，v21 将审计日志改为只追加哈希链，v22 增加登录失败计数表。迁移不删除或重写历史二维码与溯源记录。完整迁移链见 `docs/DATA_MODEL.md`。
- 领星 OpenAPI 基础认证使用 AppID/AppSecret、官方 Token/Refresh Token 路径和 MD5 + AES-ECB 签名；采购单、入库和库存写入路径必须使用领星为当前企业实际开通的精确路径，不在系统中猜测或伪造端点。

## 10. 代码结构与分层

### 10.1 目录职责

| 路径 | 职责 | 约束 |
| --- | --- | --- |
| `app.py` | 应用工厂：配置与生产启动守卫、错误处理、`GET /` 与 `GET /favicon.ico`、注册全部蓝图 | 不再定义 API 路由，见 10.3 |
| `traceability/api/*.py` | HTTP 蓝图，每个领域一个模块 | 不得导入 `app`；守卫写在路由或同文件的模块级函数里（见 10.6、10.8） |
| `traceability/` | 与 Flask 应用解耦的支撑模块 | **不得**反向导入 `app` |
| `traceability/auth.py` | 会话、CSRF、角色装饰器、`AuthError` | |
| `traceability/db.py` | 连接、迁移链、启动清理 | 迁移只能增量 |
| `traceability/errors.py` | `ApiError` | 无依赖 |
| `traceability/responses.py` | 成功/失败信封、安全响应头 | 依赖 `flask.jsonify` |
| `traceability/validators.py` | 入参校验与规范化 | **纯函数**，仅依赖 stdlib + `ApiError` |
| `traceability/endpoint_policy.py` | 出站地址策略（SSRF） | 不依赖 Flask |
| `traceability/login_guard.py` | 登录限流 | 不依赖 Flask |
| `traceability/passwords.py` | 密码策略 | 不依赖 Flask |
| `traceability/audit_chain.py` | 审计哈希链 | 不依赖 Flask |
| `tools/` | 开发与 CI 工具（不参与运行时） | |

**分层规则**：`traceability/*` 可以互相依赖，但**不得**导入 `app`；
`app.py` 导入 `traceability/*`。这条规则保证支撑模块可独立测试，
也是把路由拆成蓝图的前提。

### 10.2 为什么响应信封只有一处

`success()` / `failure()` 是唯一构造 `{"ok": ..., "data"/"message": ...}`
的地方。前端依赖这个结构的精确形状，散落的 `jsonify` 调用意味着任何格式调整
都要在多处同步修改——之前错误处理器里有 7 处手写的失败信封，现已收拢。

### 10.3 拆分进度

`app.py` 原为 8949 行、单文件、全部 107 个路由都定义在 `create_app()` 内部，
并通过闭包使用 `success` / `secure_response` 等嵌套函数。
闭包是拆蓝图的主要障碍，所以顺序是**先抽辅助层，再抽蓝图**。

| 阶段 | 内容 | 状态 |
| --- | --- | --- |
| 1a | `errors.py` + `validators.py`（错误类型与纯校验函数） | ✅ 已完成 |
| 1b | `responses.py`（信封与安全头） | ✅ 已完成 |
| 1c | `serializers.py`（行→JSON 序列化函数） | ✅ 已完成 |
| 1d | `audit_events.py`（审计写入，蓝图必需） | ✅ 已完成 |
| 1e | `idempotent_http.py`（幂等请求包装，写接口蓝图必需） | ✅ 已完成 |
| 1f | `lingxing_writes.py`（领星推送共享管道） | ✅ 已完成 |
| 2 | 按领域抽蓝图（`traceability/api/*.py`），`create_app` 只注册 | ✅ 已完成（21 个蓝图） |
| 3 | 领域逻辑外移（库存、批次、采购、生产订单、质量门、产品、BOM、逐台扫码与录入记录、设置、库存同步） | 🔄 进行中 |

已迁出的领域（共 98 条路由；`app.py` 只余 `GET /` 与 `GET /favicon.ico` 2 条，`auth.py` 7 条）：

| 蓝图 | 路由数 | 需先抽出的支撑模块 |
| --- | --- | --- |
| `api/bluetooth.py` | 3 | — |
| `api/product_families.py` | 3 | — |
| `api/part_types.py` | 3 | `audit_events.py` |
| `api/machines.py` | 3 | — |
| `api/suppliers.py` | 4 | — |
| `api/product_models.py` | 4 | — |
| `api/production_batches.py` | 4 | `inventory.py`、`production.py` |
| `api/purchase_orders.py` | 10 | `lingxing_writes.py`、`purchasing.py` |
| `api/production_orders.py` | 5 | `quality.py`、`production_orders.py` |
| `api/inbound_receipts.py` | 5 | `receipts.py` |
| `api/scan_gun.py` | 3 | —（复用 `production_orders.py`、`quality.py`） |
| `api/dashboard.py` | 3 | `trace_records.py` |
| `api/settings.py` | 3 | `settings_service.py` |
| `api/products.py` | 6 | `products.py`、`trace_plans.py` |
| `api/code_sets.py` | 7 | `code_sets.py`、`legacy_scan.py` |
| `api/batch_records.py` | 5 | —（复用 `production.py`） |
| `api/supplier_inventory.py` | 5 | `production.py`（并入正向追溯） |
| `api/part_labels.py` | 9 | —（复用 `trace_plans.py`） |
| `api/scan_sessions.py` | 4 | `legacy_scan.py`（补入工位会话） |
| `api/records.py` | 7 | —（复用 `trace_records.py`、`trace_plans.py`、`legacy_scan.py`） |
| `api/inventory_sync.py` | 2 | `inventory_sync.py` |

`/api/health` 在 `api/dashboard.py`；它免登录是因为 `auth.before_request`
按**路径**豁免，与所在文件无关。

`app.py`：8949 → 245 行，只剩应用工厂。测试仍从 `app` 导入的名字
（`LEGACY_ENTRY_DISABLED_MESSAGE`、`LINGXING_TOKEN_CACHE`、
`PRODUCT_ATTRIBUTE_COLUMNS` / `PRODUCT_ATTRIBUTE_DERIVED_COLUMNS`、
`clean_lingxing_endpoint`、`ApiError`）由 `app.py` 再导出。

### 10.4 领域模块一览

| 模块 | 职责 |
| --- | --- |
| `inventory.py` | 供应商库存扣减（不超卖的保证所在） |
| `production.py` | **批次**追溯：整批登记、反向追溯到供应商批次、从供应批次正向追溯到生产批次 |
| `production_orders.py` | **生产订单**：由采购订单开出的生产指令 |
| `receipts.py` | 供应收货（入库收货）的读取与序列化 |
| `purchasing.py` | 采购订单：规则、推送语义、48 列模板 |
| `quality.py` | 质量放行门：批次能否入库 |
| `lingxing_writes.py` | 领星推送管道（采购单/入库单/库存同步共用） |
| `lingxing.py` | 领星 API 客户端（HTTP/签名/令牌/重试） |
| `xlsx_export.py` | Excel 构造 |
| `products.py` | 产品目录：扩展资料列（外部模板契约）与清洗、产品配置载荷（含批量列表） |
| `trace_plans.py` | BOM（追溯计划）读取：槽位、计划载荷、每个产品的部件数上限 |
| `code_sets.py` | **逐台**产品套码（一台一套主码 + 部件码）的载荷 |
| `legacy_scan.py` | **逐台**扫码流程：走步机停用规则、工位会话状态 |
| `trace_records.py` | **逐台**录入记录的读模型（看板、扫码工位、记录/谱系/导出共用） |
| `settings_service.py` | 领星设置：写入接口地址校验（出站策略）、各写入接口是否已配置、设置页脱敏载荷 |
| `inventory_sync.py` | 库存同步：进行中标记与结果记录、按产品同步状态、领星收货单查询与快捷入库 |

> `production.py` 与 `production_orders.py` 名字相近但**实体不同**：
> 前者是**批次**，后者是**订单**。改其中一个前先确认改对了。
>
> 蓝图与领域模块同名时（`production_orders`、`products`、`code_sets`、`inventory_sync`），
> `traceability/api/` 下的是 HTTP 层（路由、守卫、事务与写入），
> `traceability/` 下的是领域逻辑。
>
> 中文语境下还有一对容易混的：**供应收货**（`receipts.py`，`/api/inbound-receipts`，
> 供应商部件到货并推领星）与**成品扫码入库**（`/api/scan-gun/*`，
> 扫生产二维码让成品进入库存）。两者英文都叫 inbound，但业务完全不同，
> 且**互不共用代码**——已用依赖分析确认过，因此是两个独立领域。
>
> 领域之间允许有真实依赖（`production_orders` → `purchasing`，
> 因为生产订单由采购订单开出），但**不得成环**。

### 10.5 顺序与「共享 helper」陷阱

**路由依赖的每个辅助函数都必须在它之前外移**，否则蓝图够不到
（蓝图没有对 `create_app` 的闭包）。所以每抽一个领域，常常要先抽支撑模块。

但更危险的是**反向错误**：把**跨领域共用**的 helper 塞进某一个领域模块。
采购订单搬迁时确认了 6 个 helper 是共用的：

| helper | 采购订单 | 入库单 | 库存同步 |
| --- | --- | --- | --- |
| `business_id` / `business_quantity` | ✓ | ✓ | ✓ |
| `ensure_lingxing_operation_ready` | ✓ | ✓ | ✓ |
| `external_identifier` | ✓ | ✓ | ✓ |
| `guard_is_stale` | ✓ | ✓ | ✓ |
| `lingxing_service` | ✓ | ✓ | ✓ |
| `PUSH_GUARD_TIMEOUT` | ✓ | ✓ | |

它们进了 `lingxing_writes.py`（推送管道）与 `validators.py`（入参校验），
**不是** `purchasing.py`。判据是「谁在用」，不是「谁先用到」。

生产订单搬迁时同理：`stock_in_block_reason` 与 `require_quality_release`
被扫码枪入库和设置页共用 → `quality.py`；
`production_order_row` / `production_order_data` / `production_order_progress_map`
被扫码枪入库共用 → 留在 `production_orders.py`，由两边共同导入。
**共用的 helper 不跟着某一个领域走，它跟着「用它的所有人」走。**

迁出最后 51 条路由时按同一判据：

* `fetch_records` 被看板、扫码工位、录入记录/谱系/导出共用 → `trace_records.py`；
* `trace_plan_slots` / `trace_plan_dict` / `get_plan_summary` 与 `MAX_REQUIRED_PARTS`
  被产品、BOM 路由、扫码工位与记录校对共用 → `trace_plans.py`；
* 走步机停用规则（`is_treadmill_product_model`、`LEGACY_ENTRY_DISABLED_MESSAGE`）
  被套码生成与扫码工位共用 → `legacy_scan.py`；
* 原本就是 `create_app()` 内嵌函数、且只被同一蓝图使用的 helper
  （`write_code_sets_zip`、`product_image_dir`、`record_status_payload`、
  `apply_record_status_updates`）随路由成为蓝图的模块级函数，调用图与写入检测不变。

### 10.6 守卫必须留在 HTTP 层

`tools/extract_routes.py` 通过**跟踪声明路由的那个文件内的调用图**解析守卫
（`app.py` 为 `create_app()` 内的嵌套函数，蓝图为模块级函数，见 10.8），不跨文件跟随。
守卫一旦随业务逻辑进入领域模块，提取器就看不见它——
采购订单搬迁时 `/push` 的有效权限一度从 `ADMIN + OPERATIONS`
被误报成 `any authenticated`。

**运行时权限没变，但权限文档失真了**，而这份文档是安全评审的依据。
因此：**授权调用写在路由里**，领域函数只做业务。这既是分层要求，
也是让静态门禁保持可信的前提。守卫在 helper 里的少数路由
（`_impl_batch_entry_scan`、`transition_batch_quality`、`editable_record` 等），
helper 是蓝图文件的模块级函数，而不是领域模块里的函数。

每个阶段都由全量测试与 `tools/extract_routes.py --check`
（路由与权限文档一致）共同守护。

### 10.7 搬代码会静默解除故障注入测试的武装

Python 的 `from X import y` 把名字**绑定到当前模块的命名空间**。
把调用方搬走后，`monkeypatch.setattr(app, "y", ...)` **不再影响它**。

症状很特别：**期待报错的测试返回了成功**。本项目已出现两次：

| 测试 | 被打补丁的名字 | 搬迁后 |
| --- | --- | --- |
| `test_batch_generation` | `app.new_batch_code` | 期待 409，返回 201 |
| `test_production_orders` | `app.record_audit_event` | 期待 500，返回 201 |

**搬领域代码时必须搜索** `monkeypatch.setattr` / `patch.object`，
把补丁目标改到**真正调用该函数的模块**。**断言不要动**——
改断言是掩盖问题，改目标是修正问题。

### 10.8 蓝图与权限门禁

`tools/extract_routes.py` 生成 `docs/PERMISSION_MATRIX.md`，
其 `SOURCES` **自动 glob `traceability/api/*.py`**，
所以新增蓝图不会被漏扫——手工维护的清单正是会过期、会让门禁说谎的东西。

提取器按**路由自身的缩进层级**建立调用图：
`app.py` 中的路由在 `create_app()` 内（4 空格），蓝图路由在模块级（0 空格）。
两者互不串味——否则 `app.py` 的模块级辅助函数会被算进路由的调用图，
使 service 列变噪并误判无关路由的写入行为。

蓝图的约束（写在 `traceability/api/__init__.py`）：

* **不得导入 `app`**——依赖单向，循环会破坏启动。
* 不得直接读 `app.config`，需要时用 `flask.current_app`。
* 移动路由**不得改变权限**。门禁每次构建都会比对生成的权限矩阵，变了就失败。

**调用图会跨模块跟随**（修复 D9）。原先它是**按文件**解析的：写操作一旦搬进
`traceability/*.py`，路由的调用图里就不再出现 `INSERT`，`Writes` 列误报为空——
实际漏报了 6 条路由（生产订单创建、采购单推送、登出、部件/供应商更新等）。

现在提取器通过 `from traceability.x import y` 跟随到定义模块。它**只跟随项目
自己的模块**：`_project_module_name()` 对任何不在 `traceability/` 下的 `.py`
返回 `None`，所以 `flask`、`sqlite3` 与第三方库都解析为空，分析不会下探到库内部——
**没有黑名单需要维护**。遍历以 `(module, function)` 对去重，不会无限递归。

> **这改变了「守卫放哪」的权衡吗？没有。** 跨模块跟随让分析能看见领域模块里的
> 调用，但 §10.6 的规则依然成立：守卫留在 HTTP 层。理由是**可读性**——
> 权限矩阵的 service 列要能一眼看出路由的授权来自哪里，而不是让读者去追三层调用。
> 静态分析能跟上，不代表这样写更清楚。

> **静态分析的边界**：通过 `getattr`、字典分派或运行时决定的调用不会被跟踪到。
> 当前代码库不使用这些模式；若将来使用，需要在评审时留意。

### 10.9 文档一致性门禁
`docs/PERMISSION_MATRIX.md` 是**生成的**，所以不会漂移。
其余文档都是手写的——**而它们在本次重构中确实漂移了**：

| 文档 | 曾声称 | 实际 |
| --- | --- | --- |
| `SYSTEM_ARCHITECTURE.md` | `user_version` 当前为 21 | v22 已发布 |
| `docs/API_CONTRACT.md` | `app.py` 100 + `auth.py` 7 | 9 个蓝图已迁出 |
| `docs/SECURITY.md` | systemd 沙箱尚未启用 | `PrivateTmp`/`NoNewPrivileges` 已启用 |
| `README.md` | Python 3.11+ | CI 只测 3.13 |
| `docs/UPGRADE.md` | `SECURITY.md` 尚未编写 | 早已存在 |

`tools/check_docs_consistency.py` 在 CI 中校验这些值**与代码一致**，
并已加入 `tests/test_docs_consistency.py` —— 该测试**逐项注入漂移并断言检查器会发现**，
而不是只断言「在真实仓库上通过」。**一个永远通过的检查器比没有检查器更糟**：
它把绿灯变成一个没人验证过的承诺。

校验项：数据库版本、路由总数、蓝图是否都被扫描、角色集合、
Python 支持版本（`pyproject` ↔ CI ↔ README ↔ **两个安装脚本** 一致）、
正式前端入口、关键文档是否存在（含 `docs/OPERATIONS.md` 与
`docs/RELEASE_CHECKLIST.md`）、**当前文档是否引用了已被删除的文件**、
**运行期依赖是否混入了测试工具**。

> **Python 版本这一项曾漏掉一半**：原先只检查 `install.bat`，
> 而且用的是 `re.search`，**只看第一处版本断言**。
> `install.bat` 里有两处——一处管基础解释器（3.13），
> 一处判断已有 `.venv` 是否健康（**仍写着 3.11，来自首次提交**）。
> 于是用 3.11 建的 venv 会被判为「健康」而永不重建。
> `install-linux.sh` 则**完全没有版本检查**，用裸 `python3`——
> 在 Debian 11 这类机器上会用 3.9 建出 venv，报错在很久之后才出现，且不会提到 Python。
>
> 现在两处断言**全部**检查，两个安装脚本**都在覆盖范围内**。

> **最后一项的由来**：仓库根目录曾长期放着一份 `docs/archive/design-qa-legacy-ui.md`
> 的前身——一份**被替换掉的原生 JS 界面**的视觉验收报告。界面替换后它引用的每一张截图
> 都被删除了，路径指向旧工作副本与另一台机器，结尾还写着
> 「Pytest suite: 28 passed」——而套件已是 1000 多个测试。
> **它被 git 跟踪、位于根目录，读起来完全像当前文档。**
>
> 现在的判定是：文档引用的路径若**曾被 git 跟踪、后被删除**，即为漂移。
> 「从未存在」不算——`docs/UPGRADE.md` 列出尚未建立的文档并注明「尚未建立」，
> 那是诚实的记账，把它当漂移会逼人删掉待办而不是去写文档。
> 这个区分需要 git 历史，所以 docs 作业用 `fetch-depth: 0` 检出；
> **浅克隆下这个问题永远回答「否」，检查会变成静默的空操作**。
>
> **约定：反引号里的路径 = 指向文件的指针，必须指向存在的文件。**
> 只是叙述性地提到一个已删除的文件时，用引号而非反引号
> （本文下面提到旧版前端文件名就是这样处理的）。
>
> `.kiro/specs/` 与 `docs/archive/` **豁免**：前者是已完成工作的设计记录，
> 一份写着「修改 static/app_v2.js」的 spec 在写下时是正确的，
> 改写成今天的模样等于伪造记录；后者是废弃文档的归档处，带明确标注。

> **只钉总数，不钉逐文件条数**：按领域拆分会让 `app.py` 的路由数持续变化，
> 把它写进文档就是制造下一次漂移。
>
> **Python 版本只支持 3.13**：CI 只在该版本验证。若要放开到 3.11/3.12，
> 必须先把它们加进 CI matrix 并通过——**不得宣称未测试的版本受支持**。

### 10.10 前端门禁与「产物入库」的代价

`GET /` 服务 `static/dist/index.html`（Vite 构建产物，**已入库**）。
之所以入库，是因为工厂服务器只有 Python 环境——`install.bat` 与
`install-linux.sh` 不装 Node，部署因此不需要构建步骤。

**代价是一个特定的风险**：开发者改了 `frontend/`、本地构建后看到效果、
却只提交了源码。仓库里跑的仍是旧 bundle，而 **Python 测试断言的是
「当前 `static/dist` 的内容」，不会发现这个漂移**。

CI 的 `frontend` 作业（仅 Ubuntu，浏览器行为与平台无关）因此执行：

```
pnpm install --frozen-lockfile   # 严格按 lockfile，不允许改写
pnpm typecheck                   # vue-tsc
python tools/check_frontend_build.py   # 指纹比对，必须在构建之前
pnpm build                       # 证明源码确实能编译；产物丢弃
pnpm e2e                         # 浏览器 smoke（PTS_BROWSER=chrome）
```

**检查必须在构建之前**：`pnpm build` 的 `emptyOutDir` 会清空 `static/dist`，
连同 `build-info.json` 一起删掉，之后就无法判断已提交的 bundle 来自哪一版源码。

`--frozen-lockfile` 是刻意的：允许改写 lockfile 的安装会让 CI 在一套
**没人提交过的依赖**上变绿，而漂移只会在下一次干净检出时暴露。

**CI 不提交构建产物。** 会自动 push 生成物的流水线会让每次构建都可能产生冲突，
且「什么都没改」的运行也会留下难以审查的 diff。流水线只负责拒绝漂移，
重新构建并提交是开发者的一次有意识动作（`python tools/build_frontend.py`）。

> **为什么比对指纹而不是产物字节**：这个构建**不是字节可复现的**，
> 试过两次都没成：
>
> 1. `lastBuildTime` 曾是 `dayjs()`，即构建当下的挂钟时间，被烤进 bundle。
>    同机相隔两秒的两次构建因此不同，Windows 与 Linux 更是不同（还差一个时区）。
>    **已修**：改为 `SOURCE_DATE_EPOCH` 或 git 提交时间，UTC。修完 JS 输出即稳定。
> 2. UnoCSS 输出主题自定义属性的**顺序不稳定**。同一份源码两次构建，
>    `:root` 里会有两行 `--fontWeight-*` / `--colors-*` 互换位置——内容相同、字节不同。
>    这在库内部，项目侧无法可靠固定。
>
> 所以比对的是「它构建自哪一版源码」：`build-info.json` 记录构建输入的指纹
> （`frontend/` 下真正参与构建的文件，路径 + 规范化内容，行尾统一为 LF
> 以免 Windows 检出即产生差异）。指纹确定、跨平台一致，
> 且**直接针对真实风险**——源码改了而 bundle 没重建。

E2E 用 `tools/dev_server.py` 起服务：它用临时库、自带种子账号、
拒绝在 `PTS_ENV=production` 下运行，**从不触碰 `data/traceability.db`**。
端口与凭据的默认值与 e2e 脚本一致，CI 不发明开发者用不到的配置。

> E2E 的强度值得一提：它捕获 console 错误、未捕获异常、CSP 违规，
> 并把**任何 ≥400 的响应**判为失败（仅豁免预期的 `/api/auth/me` 401）。
> 因此前端与后端之间的字段漂移会在 3 个角色 × 16 个页面上暴露出来。

## 11. 后续兼容路线

建议按以下顺序扩展：

1. 质检员角色与质量权限分离；
2. 检验模板、检验项目、抽样数量和测量值；
3. 不合格品隔离、原因分类、评审、返工与复检；
4. 生产订单排程、班次、工序进度和 BOM 版本快照；
5. 仓库、库位、领退料单和库存盘点；
6. 召回范围分析、批次质量报告与受控导出；
7. PostgreSQL/MySQL 适配及 ERP/MES 接口。

新增模块应继续复用供应批次、库存流水、产品码批次、唯一单件码、录入记录和审计事件，不应另建相互独立的重复主数据。
