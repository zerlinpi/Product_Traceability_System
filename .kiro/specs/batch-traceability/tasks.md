# Implementation Plan: 按批次溯源（batch-traceability）

## Overview

按设计文档在既有 **Flask + SQLite** 单体架构上新增一条批次溯源链路，同时只读保留历史逐台数据、停用走步机产品的逐台 / 逐部件现场录入入口。实现顺序遵循「先数据层与迁移 → 编码辅助 → 库存扣减核心 → 批次生成 → 供应关联 / 追溯 → 扫码登记 → 质量闭环 → 记录查询 → 扫码溯源 → 旧流程停用 → 前端 → 集成接线」，每步都在前一步之上增量推进，最终把所有路由 / 视图接线到应用中，避免出现游离、未整合的代码。

实现语言：**Python**（沿用 `app.py`、`traceability/*`）；前端 `static/app_v2.js` + `templates/index_v2.html`；测试沿用 `tests/` 下 pytest + Flask `test_client` 风格。

属性测试（PBT）使用 **Hypothesis**，每条属性一个独立测试、`@settings(max_examples=100)`，并以注释标注来源属性：`# Feature: batch-traceability, Property {number}: {property_text}`。

## Tasks

- [x] 1. 建立测试基础设施与依赖
  - [x] 1.1 添加 Hypothesis 依赖与共享测试生成器
    - 在 `requirements.txt` 中固定新增 `hypothesis`（PBT 库，不自研）
    - 新建 `tests/batch_strategies.py`：提供台数 `st.integers()`（覆盖越界、边界 1 与 999999）、前缀 `st.from_regex(ENTITY_CODE_PATTERN)`、原因 `st.text`（覆盖 `[1,500]` 与越界）、时间范围有序 / 乱序时间戳对，以及构造随机产品 / 供应批次结存 / 每套用量的辅助夹具
    - _Requirements: 测试策略（Testing Strategy）_

- [x] 2. 实现数据库迁移与新增结构（`user_version` 11 → 12）
  - [x] 2.1 在 `traceability/db.py` 的 `initialize_database` 中新增 v12 增量迁移
    - 在单个显式事务（`BEGIN IMMEDIATE`）内仅新增 `production_batches`、`batch_trace_records`、`production_batch_supplier_consumption` 表与相关索引
    - 通过 `_ensure_column` 为 `supplier_inventory_movements` 新增 `production_batch_id` 列
    - 事务内 `PRAGMA user_version = 12`；`user_version >= 12` 时跳过；失败则 `ROLLBACK` 并重抛以中止启动
    - _Requirements: 8.1, 8.2, 8.4, 8.5, 8.6_

  - [x] 2.2 编写属性测试：迁移只读保留历史数据
    - **Property 23: 迁移只读保留历史数据**（每张既有表行数不减少，历史逐台数据行数与字段值不变且可查）
    - **Validates: Requirements 7.4, 8.1, 8.3**
    - 文件：`tests/test_batch_migration.py`

  - [x] 2.3 编写属性测试：迁移回滚原子性
    - **Property 24: 迁移回滚原子性**（注入失败点后 `user_version`、表结构与数据均回到迁移前，无新表 / 新列残留）
    - **Validates: Requirements 8.4**
    - 文件：`tests/test_batch_migration.py`

  - [x] 2.4 编写属性测试：迁移跳过幂等
    - **Property 25: 迁移跳过幂等**（`user_version >= 12` 时再次初始化为 no-op）
    - **Validates: Requirements 8.5**
    - 文件：`tests/test_batch_migration.py`

  - [x] 2.5 编写单元测试：目标版本与失败中止启动
    - 断言迁移成功后 `user_version == 12`（8.2）；模拟迁移步骤失败时 `initialize_database` 重抛异常、`create_app` 不返回可用服务（8.6）
    - _Requirements: 8.2, 8.6_
    - 文件：`tests/test_batch_migration.py`

- [x] 3. 实现批次码值编码与解析辅助
  - [x] 3.1 在 `traceability/codes.py` 新增批次码辅助函数
    - `new_batch_code(now_compact, prefix)` 生成 `B-{prefix}-{date_code}-{token}`（`token=secrets.token_hex(3)` 大写）
    - `batch_identification_code(batch_code)` 生成 `PTS:B:{batch_code}` payload；`parse_batch_payload(payload)` 归一化完整 payload 或裸 `batch_code`
    - _Requirements: 3.1_

  - [x] 3.2 编写单元测试：payload 编码 / 解析往返
    - 具体用例验证 `parse_batch_payload(batch_identification_code(code)) == code` 且能接受裸 `batch_code`
    - _Requirements: 3.1_
    - 文件：`tests/test_batch_codes.py`

- [x] 4. Checkpoint - 确保迁移与编码相关测试通过
  - Ensure all tests pass, ask the user if questions arise.

- [x] 5. 实现库存扣减核心（事务内）
  - [x] 5.1 在 `app.py` 新增批次库存扣减辅助
    - 依据有效追溯计划计算每种供应部件的每套用量；对每套用量 > 0 的部件按 `N × 每套用量` 计算所需扣减
    - 在 `BEGIN IMMEDIATE` 事务内做充足性检查 → 扣减 `supplier_inventory_batches` 可用结存 → 写 `ISSUE` 到 `supplier_inventory_movements`（含 `production_batch_id`）；结存不足则整次失败
    - _Requirements: 4.1, 4.2, 4.3, 4.5_

  - [x] 5.2 编写属性测试：库存扣减按整批用量并写领用流水
    - **Property 11: 库存扣减按整批用量并写领用流水**（结存下降量 = `N × 每套用量`，每次扣减恰一条 `-(N×每套用量)` 的 ISSUE 流水）
    - **Validates: Requirements 4.1**
    - 文件：`tests/test_batch_inventory.py`

  - [x] 5.3 编写属性测试：结存不足整次失败且无副作用
    - **Property 12: 结存不足整次失败且无副作用**（结存、movements 计数、production_batches 计数、批次码值均不变）
    - **Validates: Requirements 4.2**
    - 文件：`tests/test_batch_inventory.py`

- [x] 6. 实现 BatchCodeGenerator 生成与批次二维码
  - [x] 6.1 实现 `POST /api/production-batches` 生成端点
    - `require_admin_or_warehouse()`（ADMIN 或 WAREHOUSE 均可，原「仅管理员」授权放宽）；仓管须对该产品型号有授权（`require_product_model_access`）；`prefix` 经 `normalize_entity_code`；`quantity` 校验为 `1..999999` 整数；产品存在且有 ACTIVE 追溯计划
    - 事务内调用 5.1 扣减 → 插入 `production_batches`（`batch_code`、prefix、planned_quantity、generated_by、generated_at、trace_plan_id）→ 插入 `production_batch_supplier_consumption` → 写审计；码值重复 / 为已有码批次再生成异码时拒绝
    - _Requirements: 1.1, 1.2, 1.3, 1.5, 1.7, 1.9, 4.4, 5.1, 5.2, 9.1, 9.3, 11.1, 11.2, 11.4_

  - [x] 6.2 实现批次列表 / 详情 / 二维码下载端点
    - `GET /api/production-batches`（管理员全量；仓管按授权产品过滤）
    - `GET /api/production-batches/<id>`（批次详情，含登记状态与反向追溯清单占位）
    - `GET /api/production-batches/<id>/qr`（复用 `make_qr_svg` 返回 `image/svg+xml`；批次不存在返回 404；复用既有 `batch_code`）
    - _Requirements: 1.6, 1.8, 3.1, 11.3_

  - [x] 6.3 编写属性测试：批次生成的结构不变式
    - **Property 1: 批次生成的结构不变式**（恰新增一个批次行、一个全局唯一码值，且不新增逐台 machines / product_code_sets）
    - **Validates: Requirements 1.1, 1.2, 11.1, 11.2**
    - 文件：`tests/test_batch_generation.py`

  - [x] 6.4 编写属性测试：生成字段保真
    - **Property 2: 生成字段保真**（还原产品型号、前缀、planned_quantity，携带非空生成时间与操作人）
    - **Validates: Requirements 1.3, 1.5**
    - 文件：`tests/test_batch_generation.py`

  - [x] 6.5 编写属性测试：生成校验拒绝且无副作用
    - **Property 3: 生成校验拒绝且无副作用**（台数越界 / 非整数 / 缺产品型号或前缀被拒，计数与结存不变）
    - **Validates: Requirements 1.4, 1.7**
    - 文件：`tests/test_batch_generation.py`

  - [x] 6.6 编写属性测试：批次二维码可下载与码值 round-trip
    - **Property 4: 批次二维码可下载与码值 round-trip**（`/qr` 返回有效 SVG，`batch_code` 编码再解析还原）
    - **Validates: Requirements 1.6, 3.1**
    - 文件：`tests/test_batch_qr.py`

  - [x] 6.7 编写属性测试：生成授权
    - **Property 26: 生成授权**（管理员或仓管允许；既非管理员亦非仓管拒绝且无批次 / 无库存扣减）
    - **Validates: Requirements 9.1, 9.3**
    - 文件：`tests/test_batch_generation_authz.py`

  - [x] 6.8 编写属性测试：后续打印复用既有码值
    - **Property 32: 后续打印复用既有码值**（重复请求二维码 / 标签复用既有 `batch_code`，不生成新码值，批次计数不变）
    - **Validates: Requirements 11.3**
    - 文件：`tests/test_batch_generation_authz.py`

  - [x] 6.9 编写属性测试：事务回滚原子性
    - **Property 13: 事务回滚原子性**（生成事务中途注入失败，回滚后库存 / 流水 / 批次 / 关联 / 码值与快照逐字段相等）
    - **Validates: Requirements 4.3, 4.4**
    - 文件：`tests/test_batch_inventory.py`

  - [x] 6.10 编写属性测试：同部件消耗量守恒
    - **Property 16: 同部件消耗量守恒**（针对同一部件各关联记录消耗数量之和 = `计划台数 × 每套用量`）
    - **Validates: Requirements 5.2**
    - 文件：`tests/test_batch_inventory.py`

  - [x] 6.11 编写属性测试：并发扣减串行化不超结存
    - **Property 14: 并发扣减串行化不超结存**（`ThreadPoolExecutor` 争用同一供应批次，累计扣减不超初始结存，最终结存不为负）
    - **Validates: Requirements 4.5**
    - 文件：`tests/test_batch_concurrency.py`

  - [x] 6.12 编写单元测试：生成的边界与冲突用例
    - 批次码重复冲突（1.9）、下载不存在批次返回 404（1.8）、为已有码批次再生成异码被拒（11.4）
    - _Requirements: 1.8, 1.9, 11.4_
    - 文件：`tests/test_batch_generation.py`

- [x] 7. 实现批次级供应关联与正 / 反向追溯
  - [x] 7.1 实现反向 / 正向追溯查询端点
    - 在 `GET /api/production-batches/<id>` 详情中返回反向追溯清单（供应批次标识、供应部件、消耗数量）；批次不存在返回 404
    - 新增 `GET /api/supplier-inventory-batches/<id>/forward-trace`（管理员查看消耗该供应批次的每个生产批次：批次码值、产品型号、生成时间、消耗数量；无消耗返回空清单）
    - _Requirements: 5.3, 5.4, 5.5, 5.6_

  - [x] 7.2 编写属性测试：批次级供应关联即反向追溯来源
    - **Property 15: 批次级供应关联即反向追溯来源**（关联记录集合与被扣减供应批次一一对应，与详情 / 扫码查询反向追溯清单一致）
    - **Validates: Requirements 5.1, 5.3, 10.2**
    - 文件：`tests/test_batch_supplier_trace.py`

  - [x] 7.3 编写属性测试：供应批次正向追溯映射
    - **Property 17: 供应批次正向追溯映射**（正向追溯集合恰等于消耗过它的全部生产批次；无消耗返回空清单）
    - **Validates: Requirements 5.5, 5.6**
    - 文件：`tests/test_batch_supplier_trace.py`

  - [x] 7.4 编写单元测试：反向追溯批次不存在
    - 用于反向追溯的生产批次不存在时拒绝并返回描述性错误
    - _Requirements: 5.4_
    - 文件：`tests/test_batch_supplier_trace.py`

- [x] 8. Checkpoint - 确保生成与追溯相关测试通过
  - Ensure all tests pass, ask the user if questions arise.

- [x] 9. 实现 BatchEntryService 扫码一次性登记
  - [x] 9.1 实现 `POST /api/batch-entry/scan` 端点
    - `require_warehouse()`（原「录入员」现场登记能力并入仓管）；解析 `code` → 批次存在；`require_product_model_access(batch.product_model_id)`；该批次尚无登记记录
    - 事务内插入单条 `batch_trace_records`（`registered_quantity` 缺省 = `planned_quantity`，修正值须为 `1..planned_quantity` 整数）、`operator_name`、`completed_by_user_id`、`registered_at`、`quality_status='ASSEMBLED'`，不创建任何逐台记录；重复登记 / 无效码 / 未授权按错误表拒绝
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 6.1, 9.2, 9.4_

  - [x] 9.2 编写属性测试：扫码登记形成单条批次记录
    - **Property 5: 扫码登记形成单条批次记录**（恰新增一条记录，缺省 registered_quantity == planned_quantity，machines / trace_records 计数不变）
    - **Validates: Requirements 2.1, 2.2**
    - 文件：`tests/test_batch_entry.py`

  - [x] 9.3 编写属性测试：登记台数修正的取值边界
    - **Property 6: 登记台数修正的取值边界**（`1..planned_quantity` 生效；非整数 / 越界拒绝且不创建或更新记录）
    - **Validates: Requirements 2.5, 2.6**
    - 文件：`tests/test_batch_entry.py`

  - [x] 9.4 编写属性测试：重复登记幂等拒绝
    - **Property 7: 重复登记幂等拒绝**（已登记批次再次登记被拒，记录数保持 1、内容不变）
    - **Validates: Requirements 2.4**
    - 文件：`tests/test_batch_entry.py`

  - [x] 9.5 编写属性测试：登记授权
    - **Property 27: 登记授权**（已授权仓管允许；未授权仓管拒绝且不创建记录）
    - **Validates: Requirements 9.2, 9.4, 2.3**
    - 文件：`tests/test_batch_entry.py`

  - [x] 9.6 编写属性测试：登记记录质量状态初始化为待检
    - **Property 18: 登记记录质量状态初始化为待检**（初始值恒为 `ASSEMBLED`）
    - **Validates: Requirements 6.1**
    - 文件：`tests/test_batch_entry.py`

- [x] 10. 实现 BatchQualityService 质量状态闭环
  - [x] 10.1 实现质量放行 / 暂扣端点
    - `POST /api/batch-trace-records/<id>/pass`（ASSEMBLED → PASSED）与 `POST /api/batch-trace-records/<id>/hold`（ASSEMBLED → HOLD，`reason` 1..500 字符）
    - `require_admin()`；仅当当前状态为 `ASSEMBLED` 才允许转换；成功后同一事务写 `audit_events`（操作人、变更前后状态、时间，HOLD 含原因）
    - _Requirements: 6.2, 6.3, 6.4, 6.5, 6.6_

  - [x] 10.2 编写属性测试：合格转换状态机
    - **Property 19: 合格转换状态机**（ASSEMBLED → PASSED；非待检状态执行合格 / 暂扣被拒且状态不变）
    - **Validates: Requirements 6.2, 6.5**
    - 文件：`tests/test_batch_quality.py`

  - [x] 10.3 编写属性测试：暂扣转换与原因长度校验
    - **Property 20: 暂扣转换与原因长度校验**（`1..500` 原因暂扣成功并原样保存；空或超长拒绝且状态不变）
    - **Validates: Requirements 6.3, 6.4**
    - 文件：`tests/test_batch_quality.py`

  - [x] 10.4 编写属性测试：质量状态变更写审计事件
    - **Property 21: 质量状态变更写审计事件**（成功转换恰新增一条审计事件，含操作人 / 时间 / 前后状态，HOLD 含原因）
    - **Validates: Requirements 6.6**
    - 文件：`tests/test_batch_quality.py`

- [x] 11. 实现批次登记记录查询
  - [x] 11.1 实现 `GET /api/batch-trace-records` 端点
    - 支持 `batchCode` / 时间范围 `[from, to]`（含起止边界）过滤，结果按生成时间由近及远排序；无匹配返回空列表（非错误）；`from > to` 返回描述性错误
    - 返回记录含批次码值、产品型号、登记台数、生成时间、前缀与操作人
    - _Requirements: 3.2, 3.3, 3.4, 3.5, 3.6_

  - [x] 11.2 编写属性测试：批次登记记录字段完整性
    - **Property 8: 批次登记记录字段完整性**（返回结构含批次码值、产品型号、登记台数、生成时间、前缀与操作人，值与来源一致）
    - **Validates: Requirements 3.2**
    - 文件：`tests/test_batch_query.py`

  - [x] 11.3 编写属性测试：批次 / 时间范围查询的过滤与排序
    - **Property 9: 批次 / 时间范围查询的过滤与排序**（返回集恰等于满足条件集合，不命中为空列表且不报错，按生成时间由近及远排序）
    - **Validates: Requirements 3.4, 3.5**
    - 文件：`tests/test_batch_query.py`

  - [x] 11.4 编写属性测试：时间范围非法拒绝
    - **Property 10: 时间范围非法拒绝**（`from > to` 拒绝并返回描述性错误）
    - **Validates: Requirements 3.6**
    - 文件：`tests/test_batch_query.py`

- [x] 12. 实现 BatchTraceQueryService 扫码只读溯源
  - [x] 12.1 实现 `POST /api/batch-trace/query` 端点
    - 已认证用户扫码只读查询；解析 `code` → 批次存在；管理员可查任意，仓管仅可查授权产品
    - 返回只读溯源信息（批次码值、产品型号、计划台数、登记台数、生成时间、前缀、质量状态）+ 批次级反向追溯清单；纯 `SELECT`，无任何数据变更；无效 / 不可解析 / 不存在返回错误
    - _Requirements: 10.1, 10.2, 10.3, 10.4, 10.5, 10.6, 10.7, 10.8_

  - [x] 12.2 编写属性测试：扫码溯源查询的只读性与字段完整性
    - **Property 29: 扫码溯源查询的只读性与字段完整性**（返回含批次码值 / 产品型号 / 计划台数 / 登记台数 / 生成时间 / 前缀 / 质量状态；查询前后相关表全库快照不变）
    - **Validates: Requirements 10.1, 10.3**
    - 文件：`tests/test_batch_trace_query.py`

  - [x] 12.3 编写属性测试：查询授权
    - **Property 30: 查询授权**（管理员查任意；仓管仅授权产品可查，否则拒绝且不返回溯源信息）
    - **Validates: Requirements 10.4, 10.5, 10.6**
    - 文件：`tests/test_batch_trace_query.py`

  - [x] 12.4 编写属性测试：无效批次码查询拒绝
    - **Property 31: 无效批次码查询拒绝**（不存在 / 不可解析 / 批次不存在的码被拒并返回描述性错误）
    - **Validates: Requirements 10.7**
    - 文件：`tests/test_batch_trace_query.py`

  - [x] 12.5 编写属性测试：未认证拒绝且无副作用
    - **Property 28: 未认证拒绝且无副作用**（未认证请求生成 / 登记 / 扫码溯源均被拒，无任何数据变更）
    - **Validates: Requirements 9.5, 10.8**
    - 文件：`tests/test_batch_auth.py`

- [x] 13. Checkpoint - 确保登记 / 质量 / 查询相关测试通过
  - Ensure all tests pass, ask the user if questions arise.

- [x] 14. 停用走步机旧的逐台 / 逐部件录入入口
  - [x] 14.1 在 `app.py` 守卫旧录入端点
    - 命中走步机产品型号的 `/api/scan`、`/api/scan/*`、`/api/products/<id>/code-sets`（逐台生成）等抛出 `ApiError("走步机产品已改为按批次溯源，逐台/逐部件录入入口已停用", 409)`，不创建任何逐台记录；非走步机产品行为不变；历史 `machines` / `trace_records` / `product_code_sets` 保持只读可查
    - _Requirements: 7.3, 7.4, 7.5_

  - [x] 14.2 编写属性测试：走步机旧录入入口停用且无逐台副作用
    - **Property 22: 走步机旧录入入口停用且无逐台副作用**（对已停用入口的直接调用被拒并返回入口已停用错误，不新增 machines / trace_records）
    - **Validates: Requirements 7.3**
    - 文件：`tests/test_batch_legacy.py`

- [x] 15. 前端批次视图与旧入口移除
  - [x] 15.1 实现批次相关前端视图
    - 在 `static/app_v2.js` + `templates/index_v2.html` 中新增批次生成（管理员或仓管）、现场批次登记（仓管）、扫码溯源查询、批次质量处理视图
    - 走步机产品现场录入视图仅呈现批次扫码登记，移除每台主码 / 逐部件扫码装配入口
    - _Requirements: 7.1, 7.2, 3.2_

  - [x] 15.2 编写静态 / DOM 断言测试
    - 参照 `tests/test_sidebar_styles_static.py` 的静态断言风格，断言走步机现场录入界面仅含批次扫码、不含逐台入口
    - _Requirements: 7.1, 7.2_
    - 文件：`tests/test_batch_frontend_static.py`

- [x] 16. 集成与接线
  - [x] 16.1 在 `app.py` 注册全部批次路由并接线
    - 将 BatchCodeGenerator / BatchEntryService / BatchTraceQueryService / BatchQualityService / 查询 / 追溯 / 旧入口守卫的所有路由注册进应用，接入前端导航，确保无游离未整合代码
    - _Requirements: 1.1, 2.1, 5.5, 6.2, 10.1, 7.1_

  - [x] 16.2 编写集成测试：端到端链路
    - 生成 → 扫码登记 → 质量放行 → 扫码溯源查询 1–2 个代表性用例，验证跨服务与事务协作
    - _Requirements: 1.1, 2.1, 6.2, 10.1_
    - 文件：`tests/test_batch_integration.py`

  - [x] 16.3 编写集成测试：真实 v11 库迁移
    - 以真实 v11 库跑一次 `initialize_database`，验证 `user_version` 落到 12 且历史数据可查
    - _Requirements: 8.1, 8.2, 8.3_
    - 文件：`tests/test_batch_integration.py`

  - [x] 16.4 编写回归测试：无关功能不变
    - 复用 / 断言现有 `tests/test_system.py` 中产品 / 供应商 / 账号 CRUD 用例保持通过
    - _Requirements: 7.5_
    - 文件：`tests/test_batch_integration.py`

- [x] 17. Final checkpoint - 确保全部测试通过
  - Ensure all tests pass, ask the user if questions arise.

- [x] 18. 实现领星集成迁移与新增结构（`user_version` 12 → 13）
  - [x] 18.1 在 `traceability/db.py` 的 `initialize_database` 中新增 v13 增量迁移
    - 在单个显式事务（`BEGIN IMMEDIATE`）内**仅新增** `purchase_orders`、`inbound_receipts` 表与相关索引（`idx_purchase_order_sync` / `idx_purchase_order_supplier` / `idx_inbound_receipt_po` / `idx_inbound_receipt_sync`）
    - 在同一事务内扩展 `users.role` 的 `CHECK` 约束以允许 `OPERATIONS` / `WAREHOUSE`（SQLite 无法直接 `ALTER CHECK`，采用既有非破坏性做法：新表 + 数据拷贝 + 重命名，或放宽 `CHECK` 并配合应用层 `VALID_ROLES` 强校验），保持既有用户行不变
    - 事务内 `PRAGMA user_version = 13`；`user_version >= 13` 时跳过（幂等 no-op）；失败则 `ROLLBACK`（结构与 `user_version` 均回退）并重抛以中止启动；仅新增、不删除 / 不重写既有表 / 列 / 行
    - 说明：本 v13 迁移仅**放宽** `users.role` 的 `CHECK` 以容纳 `OPERATIONS` / `WAREHOUSE`（此阶段 `OPERATOR` 仍并存）；三角色的**最终收敛**（撤销 `OPERATOR`、`CHECK` 收敛为恰好三角色、历史 `OPERATOR` 行迁移为 `WAREHOUSE`）在 v14 迁移（任务 30）完成
    - _Requirements: 8.1, 8.4, 8.5, 12.1, 16.1, 17.1, 20.1_

- [x] 19. 扩展角色与鉴权守卫（`traceability/auth.py`）
  - [x] 19.1 新增 `OPERATIONS` / `WAREHOUSE` 角色与守卫函数
    - 将 `ROLE_OPERATIONS = "OPERATIONS"`、`ROLE_WAREHOUSE = "WAREHOUSE"` 加入 `VALID_ROLES`；因 `users.role` 为单值列，天然满足单账号不可同时持有运营与仓管
    - 说明：本步骤在 v13 阶段将 `OPERATIONS` / `WAREHOUSE` 加入 `VALID_ROLES`（`OPERATOR` 暂并存）；`VALID_ROLES` 的三角色**最终收敛**（撤销 `OPERATOR`、原录入员能力并入仓管、并新增 `require_admin_or_warehouse()`）在任务 31 与 v14 迁移（任务 30）一并完成
    - 新增 `require_operations()`（仅 `OPERATIONS`，`ADMIN` 可放行以便管理与排障）与 `require_warehouse()`（仅 `WAREHOUSE`），与既有 `require_admin()` 风格一致：未认证抛 401、角色不符抛 403；未认证请求由既有 `before_request` 统一拦截
    - _Requirements: 12.1, 12.2, 12.3, 12.4, 12.5_

  - [x] 19.2 编写属性测试：角色互斥不变式
    - **Property 33: 角色互斥不变式（恰好三角色）**（用户角色取自 `{ADMIN, WAREHOUSE, OPERATIONS}` 中的恰好一个，不存在 `OPERATOR`，且不可同时持有运营与仓管）
    - 注释标注：`# Feature: batch-traceability, Property 33: 角色互斥不变式（恰好三角色）`
    - **Validates: Requirements 9.6, 12.1**
    - 文件：`tests/test_lingxing_authz.py`

  - [x] 19.3 编写属性测试：领星操作的角色授权
    - **Property 34: 领星操作的角色授权**（角色与操作要求匹配时放行；不匹配或未认证时拒绝、无数据变更、返回权限不足或需先认证错误）
    - 注释标注：`# Feature: batch-traceability, Property 34: 领星操作的角色授权`
    - **Validates: Requirements 12.2, 12.3, 12.4, 12.5**
    - 文件：`tests/test_lingxing_authz.py`
    - 说明：领星 HTTP 客户端 mock / stub，不访问真实接口

- [x] 20. 实现 `traceability/lingxing.py` 防腐层 / 适配器
  - [x] 20.1 实现凭据加载、脱敏与安全降级
    - 从运行时配置 / 环境变量读取 `PTS_LINGXING_APP_ID` / `PTS_LINGXING_APP_SECRET`（沿用 `os.environ.get` 风格），**不硬编码**任何 appId / appSecret 明文；凭据缺失判定（`None` / 空串 / 仅空白）
    - 提供 `mask_secret(value)`：至多保留末尾 4 位、其余以掩码字符替换；凭据缺失时在发起任何网络请求前拒绝领星调用、不改本地数据、返回「领星凭据未配置」错误，且不影响无关功能
    - _Requirements: 13.1, 13.2, 13.3, 13.4, 13.5_

  - [x] 20.2 编写属性测试：凭据缺失时安全降级且零外部调用
    - **Property 35: 凭据缺失时安全降级且零外部调用**（缺失形态凭据 × 任意领星操作，发起网络请求前拒绝、HTTP 客户端调用次数为 0、本地数据快照不变、返回未配置错误）
    - 注释标注：`# Feature: batch-traceability, Property 35: 凭据缺失时安全降级且零外部调用`
    - **Validates: Requirements 13.2, 13.5**
    - 文件：`tests/test_lingxing_credentials.py`
    - 说明：领星 HTTP 客户端 mock / stub

  - [x] 20.3 编写属性测试：密钥脱敏
    - **Property 36: 密钥脱敏**（`appSecret` / `access_token` / `refresh_token` 脱敏输出至多保留末尾 4 位、其余掩码，长度 > 4 时与原文不等）
    - 注释标注：`# Feature: batch-traceability, Property 36: 密钥脱敏`
    - **Validates: Requirements 13.4**
    - 文件：`tests/test_lingxing_credentials.py`

  - [x] 20.4 实现访问令牌缓存、获取、刷新与回退（`LingxingTokenCache`）
    - 进程内、线程安全（`threading.Lock`）缓存 `access_token` / `refresh_token` / 到期时间；有效性判定「当前时间 < 到期时间 − 60 秒安全余量」；单次令牌请求超时 10 秒
    - 缓存为空 / 过期无有效刷新令牌时用 `appId + appSecret` 获取；过期且刷新令牌有效时用 `refresh_token` 刷新；刷新失败回退重新获取；令牌请求最多重试 2 次（累计 ≤ 3）、相邻间隔 ≥ 1 秒；耗尽失败中止本次调用、不改本地数据、返回描述性错误
    - 通过**可注入时钟**（`NOW_PROVIDER` 风格）驱动时序，避免真实 `sleep`
    - _Requirements: 14.1, 14.2, 14.3, 14.4, 14.5, 14.6_

  - [x] 20.5 编写属性测试：访问令牌缓存有效性判定
    - **Property 37: 访问令牌缓存有效性判定**（缓存复用当且仅当「当前时间 < 到期时间 − 60 秒」，否则触发获取 / 刷新）
    - 注释标注：`# Feature: batch-traceability, Property 37: 访问令牌缓存有效性判定`
    - **Validates: Requirements 14.1, 14.2**
    - 文件：`tests/test_lingxing_token.py`
    - 说明：使用可注入假时钟，令牌端点 mock

  - [x] 20.6 编写属性测试：令牌刷新与回退路径
    - **Property 38: 令牌刷新与回退路径**（刷新令牌有效则刷新，无效 / 失败则回退用 appId+appSecret 重新获取；两路径成功后缓存被更新）
    - 注释标注：`# Feature: batch-traceability, Property 38: 令牌刷新与回退路径`
    - **Validates: Requirements 14.3, 14.4**
    - 文件：`tests/test_lingxing_token.py`

  - [x] 20.7 编写属性测试：令牌请求重试有界且耗尽无副作用
    - **Property 39: 令牌请求重试有界且耗尽无副作用**（累计请求 ≤ 3 次、相邻间隔 ≥ 1 秒；耗尽失败时中止调用、不改本地数据、返回描述性错误）
    - 注释标注：`# Feature: batch-traceability, Property 39: 令牌请求重试有界且耗尽无副作用`
    - **Validates: Requirements 14.5, 14.6**
    - 文件：`tests/test_lingxing_token.py`
    - 说明：mock 令牌端点持续失败，用假时钟断言间隔

  - [x] 20.8 实现请求签名与可配置 `SignStrategy`
    - `compute_sign(params, access_token, timestamp)` 依据请求参数、有效 `access_token` 与 `timestamp`（单位 / 格式作为可配置项）计算 `sign`；`sign` 与同一 `timestamp` 一并发送、不修改参与签名的参数值；缺有效令牌或必要参数时中止、不发外部调用、不改本地数据、返回「无法生成请求签名」错误
    - 签名算法建模为可配置 `SignStrategy`（默认占位实现），端点路径 / 字段映射由可配置项驱动，凭据 + 官方文档确认后可替换而无需改动调用方
    - _Requirements: 15.1, 15.2, 15.3, 15.4_

  - [x] 20.9 编写属性测试：请求签名确定性与一致携带
    - **Property 40: 请求签名确定性与一致携带**（相同输入得相同 `sign`；携带的 `timestamp` 与计算用 `timestamp` 同值；参数值不被修改）
    - 注释标注：`# Feature: batch-traceability, Property 40: 请求签名确定性与一致携带`
    - **Validates: Requirements 15.1, 15.2**
    - 文件：`tests/test_lingxing_sign.py`

  - [x] 20.10 编写属性测试：签名前置校验缺失即中止
    - **Property 41: 签名前置校验缺失即中止**（缺有效令牌 / 必要参数时中止、HTTP 客户端零调用、不改本地数据、返回无法生成签名错误）
    - 注释标注：`# Feature: batch-traceability, Property 41: 签名前置校验缺失即中止`
    - **Validates: Requirements 15.3**
    - 文件：`tests/test_lingxing_sign.py`
    - 说明：领星 HTTP 客户端 mock / stub

  - [x] 20.11 实现有界重试执行器、可注入 HTTP 客户端与时钟
    - 可配置项（带边界校验与默认）：`max_retries`（0–10，默认 3）、`request_timeout`（1–120 秒，默认 30）、`retry_interval`（1–60 秒，默认 2）
    - 可重试错误（网络超时 / 连接失败 / 限流 / 临时性 5xx）按 `retry_interval` 间隔在 `max_retries` 内重试、每次以 `request_timeout` 为上限；不可重试错误（鉴权失败 / 凭据无效 / 参数校验失败）不重试
    - 定义**可注入** `LingxingHttpClient`（测试可 mock / stub）与可注入时钟，供令牌 / 签名 / 推送复用
    - _Requirements: 19.1, 19.2, 19.3, 19.4, 19.5_

  - [x] 20.12 编写属性测试：重试配置的边界与默认值
    - **Property 52: 重试配置的边界与默认值**（合法 `0..10` / `1..120` / `1..60` 接受，越界拒绝或收敛，缺省取默认 3 / 30 / 2）
    - 注释标注：`# Feature: batch-traceability, Property 52: 重试配置的边界与默认值`
    - **Validates: Requirements 19.1**
    - 文件：`tests/test_lingxing_retry.py`

  - [x] 20.13 编写属性测试：可重试错误的有界重试执行
    - **Property 53: 可重试错误的有界重试执行**（真实请求次数 ≤ `max_retries + 1`、相邻间隔 ≥ `retry_interval`、每次以 `request_timeout` 为上限）
    - 注释标注：`# Feature: batch-traceability, Property 53: 可重试错误的有界重试执行`
    - **Validates: Requirements 19.2**
    - 文件：`tests/test_lingxing_retry.py`
    - 说明：mock 可重试失败，用假时钟断言间隔

- [x] 21. Checkpoint - 确保领星防腐层（凭据 / 令牌 / 签名 / 重试）相关测试通过
  - Ensure all tests pass, ask the user if questions arise.

- [x] 22. 实现采购订单创建、查询与推送端点
  - [x] 22.1 实现采购订单创建与查询端点
    - `POST /api/purchase-orders`（`require_operations()`）：校验供应商 / 商品 / 采购数量非空且存在、`quantity` 为 `1..999999` 整数；事务内插入 `purchase_orders`（`sync_status='PENDING'`、创建人 / 创建时间）并写审计（`object_type='PURCHASE_ORDER'`, `event_type='PO_CREATED'`）；缺字段 / 越界拒绝且无副作用
    - `GET /api/purchase-orders`（列表 / 过滤 `syncStatus?`/`supplierId?`/`from?`/`to?`，按创建时间由近及远）与 `GET /api/purchase-orders/<id>`（含 `syncStatus`、`lingxingPoId`、`pushedAt`，未成功推送时后两者为空）
    - _Requirements: 16.1, 16.2, 16.3, 20.1_

  - [x] 22.2 实现 `POST /api/purchase-orders/<id>/push` 推送端点
    - `require_operations()`；凭据缺失即拒绝；前置 `sync_status ∈ {PENDING, FAILED}` 才推送，已 `PUSHED` 时不发起领星调用、返回既有 `lingxingPoId` 与「已推送」提示（幂等）
    - 借助进行中守卫（`push_in_progress`，`BEGIN IMMEDIATE` 内翻转）防止并发重复推送；成功（30 秒内）保存 `lingxing_po_id` + 原始响应、置 `PUSHED`、记录 `pushed_at`、写审计；失败 / 超时保持记录、置 `FAILED`、写含领星错误审计并返回描述性错误
    - _Requirements: 16.4, 16.5, 16.6, 19.5, 20.2, 20.3_

  - [x] 22.3 编写属性测试：采购订单创建保真且初始化为待推送
    - **Property 42: 采购订单创建保真且初始化为待推送**（合法请求恰新增一条采购订单，保真供应商 / 商品 / 数量 / 创建人 / 创建时间，`sync_status='PENDING'`）
    - 注释标注：`# Feature: batch-traceability, Property 42: 采购订单创建保真且初始化为待推送`
    - **Validates: Requirements 16.1**
    - 文件：`tests/test_purchase_orders.py`

  - [x] 22.4 编写属性测试：采购订单创建校验拒绝且无副作用
    - **Property 43: 采购订单创建校验拒绝且无副作用**（缺字段 / 数量为空 / 非整数 / 越界拒绝、不创建、无数据变更、返回描述性错误）
    - 注释标注：`# Feature: batch-traceability, Property 43: 采购订单创建校验拒绝且无副作用`
    - **Validates: Requirements 16.2, 16.3**
    - 文件：`tests/test_purchase_orders.py`

- [x] 23. 实现入库收货创建与查询端点
  - [x] 23.1 实现 `POST /api/inbound-receipts` 与详情端点
    - `POST /api/inbound-receipts`（`require_warehouse()`）：校验 `purchaseOrderId` 提供且对应采购订单存在、`quantity` 为 `1..999999` 整数；事务内插入 `inbound_receipts`（关联采购订单、收货人 = 当前仓管、收货时间 = 系统当前时间、`sync_status='PENDING'`），返回新建记录标识；缺引用 / 不存在 / 数量越界拒绝且无副作用
    - `GET /api/inbound-receipts/<id>` 详情（含 `syncStatus`、`lingxingInboundId`、`pushedAt`）
    - _Requirements: 17.1, 17.2, 17.3_

  - [x] 23.2 编写属性测试：入库收货创建保真且初始化为待推送
    - **Property 44: 入库收货创建保真且初始化为待推送**（合法入库恰新增一条记录，关联采购订单、收货人 = 提交仓管、收货时间 = 当前时间、记录数量，`sync_status='PENDING'`，返回标识）
    - 注释标注：`# Feature: batch-traceability, Property 44: 入库收货创建保真且初始化为待推送`
    - **Validates: Requirements 17.1**
    - 文件：`tests/test_inbound_receipts.py`

  - [x] 23.3 编写属性测试：入库收货创建校验拒绝且无副作用
    - **Property 45: 入库收货创建校验拒绝且无副作用**（缺采购订单引用 / 引用不存在 / 数量为空 / 非整数 / 越界拒绝、不创建、无数据变更、返回描述性错误）
    - 注释标注：`# Feature: batch-traceability, Property 45: 入库收货创建校验拒绝且无副作用`
    - **Validates: Requirements 17.2, 17.3**
    - 文件：`tests/test_inbound_receipts.py`

- [x] 24. 实现领星入库推送端点
  - [x] 24.1 实现 `POST /api/inbound-receipts/<id>/push` 领星入库
    - `require_operations()`；凭据缺失即拒绝；前置：入库收货记录存在（否则拒绝）、其所属采购订单 `sync_status='PUSHED'`（否则拒绝且不改状态）、`push_in_progress=0`（否则「推送进行中」拒绝、不发起新调用）
    - 幂等：已 `PUSHED` 不发起领星调用、返回既有 `lingxingInboundId`；成功保存 `lingxing_inbound_id` + 原始响应、置 `PUSHED`、`pushed_at`、写审计；失败 / 超时保持记录与既有标识、置 `FAILED`、写含领星错误审计并返回描述性错误
    - _Requirements: 18.1, 18.2, 18.3, 18.4, 18.5, 18.6, 19.5, 20.2, 20.3_

  - [x] 24.2 编写属性测试：推送成功持久化并置为已推送
    - **Property 46: 推送成功持久化并置为已推送**（`PENDING`/`FAILED` 推送成功后保存领星标识 + 原始响应、记录推送完成时间、置 `PUSHED`）
    - 注释标注：`# Feature: batch-traceability, Property 46: 推送成功持久化并置为已推送`
    - **Validates: Requirements 16.4, 18.1, 20.2**
    - 文件：`tests/test_lingxing_push.py`
    - 说明：mock 领星返回成功

  - [x] 24.3 编写属性测试：推送幂等（已推送不重复创建）
    - **Property 47: 推送幂等（已推送不重复创建）**（已 `PUSHED` 重复推送不发起领星调用、不重复创建、保持既有标识、返回既有标识 / 已推送提示）
    - 注释标注：`# Feature: batch-traceability, Property 47: 推送幂等（已推送不重复创建）`
    - **Validates: Requirements 16.5, 18.4, 19.5**
    - 文件：`tests/test_lingxing_push.py`

  - [x] 24.4 编写属性测试：推送失败标记为 FAILED 且无部分修改
    - **Property 48: 推送失败标记为 FAILED 且无部分修改**（返回失败 / 超时 / 重试耗尽 / 不可重试错误时记录存续、标识不变、置 `FAILED`、返回含领星错误）
    - 注释标注：`# Feature: batch-traceability, Property 48: 推送失败标记为 FAILED 且无部分修改`
    - **Validates: Requirements 16.6, 18.5, 19.3, 19.4**
    - 文件：`tests/test_lingxing_push.py`
    - 说明：mock 领星返回失败 / 超时

  - [x] 24.5 编写属性测试：领星入库前置条件——采购订单须已推送
    - **Property 49: 领星入库前置条件——采购订单须已推送**（所属采购订单非 `PUSHED` 时拒绝、领星调用次数为 0、状态不变、返回需先推送采购订单错误）
    - 注释标注：`# Feature: batch-traceability, Property 49: 领星入库前置条件——采购订单须已推送`
    - **Validates: Requirements 18.3**
    - 文件：`tests/test_lingxing_push.py`

  - [x] 24.6 编写属性测试：领星入库引用不存在即拒绝
    - **Property 50: 领星入库引用不存在即拒绝**（引用入库收货不存在时拒绝、不发起领星调用、返回记录不存在错误）
    - 注释标注：`# Feature: batch-traceability, Property 50: 领星入库引用不存在即拒绝`
    - **Validates: Requirements 18.2**
    - 文件：`tests/test_lingxing_push.py`

  - [x] 24.7 编写属性测试：进行中守卫防止并发重复推送
    - **Property 51: 进行中守卫防止并发重复推送**（推送进行中再次请求被拒、不发起新调用、状态不变；并发重复推送至多触发一次真实领星调用）
    - 注释标注：`# Feature: batch-traceability, Property 51: 进行中守卫防止并发重复推送`
    - **Validates: Requirements 18.6**
    - 文件：`tests/test_lingxing_push.py`
    - 说明：`ThreadPoolExecutor` 并发，mock 领星客户端计数真实调用

- [x] 25. 实现同步状态查询与审计
  - [x] 25.1 实现同步状态查询端点并统一审计
    - `GET /api/purchase-orders/<id>/sync-status` 与 `GET /api/inbound-receipts/<id>/sync-status`（`require_operations()`）：返回当前 `syncStatus`、已记录领星标识与最近一次推送时间；从未成功推送时领星标识与推送时间返回为空；记录不存在返回描述性 404 错误
    - 复用 `audit_events`（append-only，`object_type ∈ {'PURCHASE_ORDER','INBOUND_RECEIPT'}`）记录每次推送操作人 / 操作类型 / 目标标识 / 时间 / 结果（失败含领星错误），不提供任何修改 / 删除审计事件的路径
    - _Requirements: 20.2, 20.3, 20.4, 20.5, 20.6_

  - [x] 25.2 编写属性测试：同步状态取值不变式
    - **Property 54: 同步状态取值不变式**（每条记录任意时刻恰有一个当前同步状态且恒属 `{PENDING, PUSHED, FAILED}`，创建时初始化为 `PENDING`）
    - 注释标注：`# Feature: batch-traceability, Property 54: 同步状态取值不变式`
    - **Validates: Requirements 20.1**
    - 文件：`tests/test_lingxing_sync_audit.py`

  - [x] 25.3 编写属性测试：推送执行写入完整审计事件
    - **Property 55: 推送执行写入完整审计事件**（每次推送无论成败恰新增一条审计事件，含操作人 / 操作类型 / 目标标识 / 时间 / 结果，失败含领星错误）
    - 注释标注：`# Feature: batch-traceability, Property 55: 推送执行写入完整审计事件`
    - **Validates: Requirements 20.3**
    - 文件：`tests/test_lingxing_sync_audit.py`

  - [x] 25.4 编写属性测试：同步状态查询映射
    - **Property 56: 同步状态查询映射**（查询返回与推送历史一致的当前状态、领星标识与最近推送时间；从未成功推送时后两者为空）
    - 注释标注：`# Feature: batch-traceability, Property 56: 同步状态查询映射`
    - **Validates: Requirements 20.4**
    - 文件：`tests/test_lingxing_sync_audit.py`

  - [x] 25.5 编写属性测试：查询不存在记录即拒绝
    - **Property 57: 查询不存在记录即拒绝**（不存在的采购订单 / 入库收货标识查询被拒、不返回推送情况、返回记录不存在错误）
    - 注释标注：`# Feature: batch-traceability, Property 57: 查询不存在记录即拒绝`
    - **Validates: Requirements 20.5**
    - 文件：`tests/test_lingxing_sync_audit.py`

  - [x] 25.6 编写属性测试：审计事件只追加不可篡改
    - **Property 58: 审计事件只追加不可篡改**（已写入的领星推送审计事件集合只增不减、不被修改或删除）
    - 注释标注：`# Feature: batch-traceability, Property 58: 审计事件只追加不可篡改`
    - **Validates: Requirements 20.6**
    - 文件：`tests/test_lingxing_sync_audit.py`

- [x] 26. Checkpoint - 确保采购 / 入库 / 推送 / 同步审计相关测试通过
  - Ensure all tests pass, ask the user if questions arise.

- [x] 27. 实现领星集成前端视图
  - [x] 27.1 在 `static/app_v2.js` + `templates/index_v2.html` 新增领星相关视图
    - 采购订单创建 / 推送视图（运营）、入库收货视图（仓管）、领星入库推送视图（运营）、同步状态 / 领星标识查询视图；接入前端导航
    - UI 中对 `appSecret` / `access_token` / `refresh_token` 等密钥 / 令牌一律脱敏展示（复用后端 `mask_secret` 输出，不在前端呈现完整明文）
    - _Requirements: 12.2, 12.3, 13.4, 16.1, 17.1, 18.1, 20.4_

  - [x] 27.2 编写静态 / DOM 断言测试
    - 参照 `tests/test_sidebar_styles_static.py` 静态断言风格，断言领星视图按角色呈现（运营见采购 / 推送 / 领星入库、仓管见入库）、且 UI 不呈现完整密钥 / 令牌明文
    - _Requirements: 12.2, 12.3, 13.4_
    - 文件：`tests/test_lingxing_frontend_static.py`

- [x] 28. 领星集成接线与集成测试
  - [x] 28.1 在 `app.py` 注册全部领星路由并接线前端导航
    - 将采购订单创建 / 查询 / 推送、入库收货创建 / 查询、领星入库推送、同步状态查询的所有路由注册进应用，接入前端导航，确保无游离未整合代码；注入可 mock 的 `LingxingHttpClient` 与可注入时钟以便测试
    - 说明：`appId` / `appSecret` 尚未下发，实时接口对接（真实端点路径 / 字段映射 / 签名算法）作为可配置项延后填充，不阻塞本地链路
    - _Requirements: 12.2, 16.1, 17.1, 18.1, 20.4_

  - [x] 28.2 编写集成测试：领星端到端链路（mock 领星客户端）
    - 创建采购订单 → 推送采购订单 → 入库 → 领星入库推送 → 查询同步状态，验证跨服务与事务协作；领星 HTTP 客户端 mock / stub、用可注入时钟，绝不访问真实接口
    - _Requirements: 16.1, 16.4, 17.1, 18.1, 20.4_
    - 文件：`tests/test_lingxing_integration.py`

  - [x] 28.3 编写集成测试：真实 v12 库迁移到 user_version 13
    - 以真实 v12 库跑一次 `initialize_database`，验证 `user_version` 落到 13、`purchase_orders` / `inbound_receipts` 表存在、`users.role` 允许 `OPERATIONS` / `WAREHOUSE`，且批次溯源与历史数据行数与字段值不变
    - _Requirements: 8.1, 8.3, 12.1, 16.1, 17.1_
    - 文件：`tests/test_lingxing_migration.py`

- [x] 29. Final checkpoint - 确保领星集成全部测试通过
  - Ensure all tests pass, ask the user if questions arise.

- [x] 30. 实现三角色收敛与新增能力迁移（`user_version` 13 → 14，`traceability/db.py`）
  - [x] 30.1 在 `traceability/db.py` 的 `initialize_database` 中新增 v14 增量迁移
    - 在单个显式事务（`BEGIN IMMEDIATE`）内**仅新增** `app_settings`、`production_orders`（`purchase_order_id` `UNIQUE`、`production_batch_id` `UNIQUE`）、`product_stock`、`inbound_scan_records` 表与相关索引（`idx_production_order_po` / `idx_inbound_scan_order` / `idx_inbound_scan_product`）
    - 在同一事务内将 `users.role` 的 `CHECK` 约束**收敛**为 `IN ('ADMIN','WAREHOUSE','OPERATIONS')`：采用既有非破坏性做法（新表 + 数据拷贝 + 重命名），并将任何历史遗留 `role='OPERATOR'` 的行就地迁移为 `'WAREHOUSE'`（录入员能力并入仓管），保持既有其余用户行不变
    - 事务内 `PRAGMA user_version = 14`；`user_version >= 14` 时跳过（幂等 no-op）；失败则 `ROLLBACK`（结构、数据与 `user_version` 均回退）并重抛以中止启动；仅新增表 / 索引与角色收敛，不删除 / 不重写批次溯源、领星集成与历史逐台数据，既有各表行数不减少
    - _Requirements: 8.1, 8.4, 8.5, 12.1, 21.1, 22.1, 23.1, 24.3, 26.1_

- [x] 31. 角色最终收敛与鉴权守卫（`traceability/auth.py`）
  - [x] 31.1 `VALID_ROLES` 三角色收敛、撤销 `OPERATOR`、新增守卫函数
    - 将 `VALID_ROLES` 收敛为 `{ROLE_ADMIN, ROLE_WAREHOUSE, ROLE_OPERATIONS}`，**撤销** `ROLE_OPERATOR`（不再作为合法角色）；因 `users.role` 为单值列，天然满足单账号恰好持有一个角色（Req 9.6、12.1）
    - 新增 `require_warehouse()`（仅 `WAREHOUSE`，`ADMIN` 放行；**覆盖原录入员的现场扫码登记 / 入库能力**）与 `require_admin_or_warehouse()`（`ADMIN` 或 `WAREHOUSE`，供生成生产批次 / 生产订单使用）；与既有 `require_admin()` / `require_operations()` 风格一致：未认证抛 401、角色不符抛 403
    - 将产品型号授权（`user_product_model_permissions`）的范围检查由原「仅 `OPERATOR` 生效」调整为「仅 `WAREHOUSE` 生效」：`_replace_user_scope` 中 `role != ROLE_OPERATOR` 判定改为 `role not in {ROLE_WAREHOUSE}`（运营与管理员无按产品的范围限制）
    - 说明：三角色授权矩阵的属性测试（Property 59）在全部新路由接线完成后由任务 43.2 落地
    - _Requirements: 9.1, 9.2, 9.6, 12.1, 12.2, 12.4_

- [x] 32. 实现 SystemSettingsService 与领星凭据设置加载（Req 21）
  - [x] 32.1 实现 `app_settings` 键值存储与 `GET` / `PUT /api/settings`
    - `GET /api/settings`（`require_admin()`）：返回通用设置键值 + 领星凭据字段的**脱敏值**（`mask_secret`，至多末 4 位可见），绝不返回完整明文
    - `PUT /api/settings`（`require_admin()`）：领星凭据 `appId` / `appSecret` / `id` 各自长度 `1..256` 且非纯空白；任一缺失 / 空 / 纯空白 / 超 256 字符则拒绝、**保持既有凭据不变**、返回长度要求错误；`BEGIN IMMEDIATE` 内 upsert 到 `app_settings`（敏感键 `is_secret=1`）、写审计（`object_type='APP_SETTINGS'`，凭据以脱敏形式记录）
    - 出参：保存成功确认，凭据以脱敏形式呈现（复用后端 `mask_secret`）
    - _Requirements: 21.1, 21.2, 21.3, 21.4, 21.5, 21.6_

  - [x] 32.2 更新 `traceability/lingxing.py` 凭据加载优先读取 `app_settings`
    - 将凭据加载优先级调整为「`app_settings` 设置存储（键 `lingxing.app_id` / `lingxing.app_secret` / `lingxing.id`） → 环境变量 `PTS_LINGXING_APP_ID` / `PTS_LINGXING_APP_SECRET` / `PTS_LINGXING_ID` → 可选配置文件」；沿用任务 20.1 的凭据缺失判定与安全降级，`appId` / `appSecret` / `id` 三元组任一缺失即拒绝领星调用
    - 说明：本任务在任务 20.1 凭据加载基础上增补 `app_settings` 首选来源，不改变脱敏与降级语义
    - _Requirements: 13.1, 13.2, 21.1_

  - [x] 32.3 编写属性测试：领星凭据长度校验与设置存储读取
    - **Property 60: 领星凭据长度校验与设置存储读取**（三元组长度均 `1..256` 且非纯空白时接受、安全存储并被 `LingxingIntegrationService` 读取；任一缺失 / 空 / 纯空白 / 超 256 时拒绝、既有凭据不变、返回长度要求错误）
    - 注释标注：`# Feature: batch-traceability, Property 60: 领星凭据长度校验与设置存储读取`
    - **Validates: Requirements 21.3, 21.5, 13.1**
    - 文件：`tests/test_system_settings.py`

  - [x] 32.4 编写属性测试：系统设置凭据脱敏回显
    - **Property 61: 系统设置凭据脱敏回显**（回显 / 保存确认 / 日志中凭据至多末 4 位可见、其余掩码，长度 >4 时与原文不等）
    - 注释标注：`# Feature: batch-traceability, Property 61: 系统设置凭据脱敏回显`
    - **Validates: Requirements 21.3, 21.4**
    - 文件：`tests/test_system_settings.py`

- [x] 33. 实现 UserManagementService（Req 22，原「录入员管理」更名为「用户管理」）
  - [x] 33.1 实现 `POST /api/users` 与 `PUT /api/users/<id>`（复用 `auth.py` 账号基础设施）
    - `POST /api/users`（`require_admin()`）：`username` 长度 `1..50`、`password` 长度 `8..128`、`role ∈ {WAREHOUSE, OPERATIONS, ADMIN}`（`VALID_ROLES`）；缺字段 / 长度违规 / 非法角色拒绝；重复用户名命中唯一约束 → `AuthError("该账号已存在", 409)`；事务内插入 `users` + 按角色写授权范围（仅 `WAREHOUSE` 持有产品 / 供应商授权）+ 写审计
    - `PUT /api/users/<id>`（`require_admin()`）：`role ∈ {WAREHOUSE, OPERATIONS, ADMIN}`，单值列保证编辑后恰好持有一个角色；沿用既有「不允许停用 / 降级自身管理员」保护
    - 前端入口更名：将原「录入员管理」呈现为「用户管理」（前端视图在任务 42 落地）
    - _Requirements: 22.1, 22.2, 22.3, 22.4, 22.5, 22.6_

  - [x] 33.2 编写属性测试：新增用户校验与恰好单角色
    - **Property 62: 新增用户校验与恰好单角色**（用户名 `1..50` / 密码 `8..128` / 角色属于三角色时创建 / 更新并恰好持有一个角色；缺字段 / 长度越界 / 非法角色拒绝且无副作用）
    - 注释标注：`# Feature: batch-traceability, Property 62: 新增用户校验与恰好单角色`
    - **Validates: Requirements 22.2, 22.3, 22.4**
    - 文件：`tests/test_user_management.py`

  - [x] 33.3 编写属性测试：重复用户名拒绝
    - **Property 63: 重复用户名拒绝**（与既有账号用户名重复的新增请求被拒、不创建账号、用户总数不变、返回用户名已存在错误）
    - 注释标注：`# Feature: batch-traceability, Property 63: 重复用户名拒绝`
    - **Validates: Requirements 22.5**
    - 文件：`tests/test_user_management.py`

- [x] 34. Checkpoint - 确保迁移收敛 / 角色 / 系统设置 / 用户管理相关测试通过
  - Ensure all tests pass, ask the user if questions arise.

- [x] 35. 实现 ProductionOrderService（Req 23）
  - [x] 35.1 实现 `POST /api/production-orders` 生成生产订单
    - `require_admin_or_warehouse()`（Req 23.8）；入参 `{ purchaseOrderId }`；采购订单存在（缺失 / 不存在 → 描述性错误）；该采购订单**尚未**被任何生产订单关联（已关联 → 拒绝「该采购订单已生成生产订单」、既有订单与二维码不变）
    - 事务内（`BEGIN IMMEDIATE`）：与生产批次机制同步生成一个 `production_batches`（含全局唯一 `batch_code`，复用 `codes.py`，payload 前缀 `PTS:B:`）→ 插入 `production_orders`（关联 `purchase_order_id` 与 `production_batch_id`）→ 写审计；`production_orders.purchase_order_id` `UNIQUE`（一采购订单至多一生产订单）、`production_batch_id` `UNIQUE`（一码一单）；任一步失败整体回滚，不遗留无二维码的生产订单，也不遗留未关联生产订单的孤立二维码 / 生产批次
    - 出参：生产订单详情（含 `id`、`productionQrCode`、来源 `purchaseOrderId`、`downloadUrl`）
    - _Requirements: 23.1, 23.2, 23.3, 23.5, 23.6, 23.7, 23.8_

  - [x] 35.2 实现生产订单列表 / 详情 / 生产二维码下载端点
    - `GET /api/production-orders`（列表 / 查询，支持按来源采购订单过滤）
    - `GET /api/production-orders/<id>`（详情，**单产品单行**展示：每个产品恰好占一行、每行至少含产品型号与数量）
    - `GET /api/production-orders/<id>/qr`（复用 `make_qr_svg` 返回 `image/svg+xml`；不存在返回 404）
    - _Requirements: 23.3, 23.4_

  - [x] 35.3 编写属性测试：生产订单创建事务性与唯一生产二维码
    - **Property 64: 生产订单创建事务性与唯一生产二维码**（未关联的采购订单生成成功后恰好新增一条 `production_orders`、关联来源采购订单、同步生成恰好一个全局唯一生产二维码；注入事务失败点回滚后无孤立订单 / 二维码 / 生产批次残留）
    - 注释标注：`# Feature: batch-traceability, Property 64: 生产订单创建事务性与唯一生产二维码`
    - **Validates: Requirements 23.1, 23.2, 23.5**
    - 文件：`tests/test_production_orders.py`

  - [x] 35.4 编写属性测试：采购订单—生产订单幂等（一对至多一）
    - **Property 65: 采购订单—生产订单幂等（一对至多一）**（已被关联的采购订单再次生成被拒、不创建新订单 / 二维码、既有订单与二维码不变、返回已生成错误）
    - 注释标注：`# Feature: batch-traceability, Property 65: 采购订单—生产订单幂等（一对至多一）`
    - **Validates: Requirements 23.6**
    - 文件：`tests/test_production_orders.py`

  - [x] 35.5 编写属性测试：生产订单创建校验拒绝且无副作用
    - **Property 66: 生产订单创建校验拒绝且无副作用**（未选采购订单 / 采购订单不存在时拒绝、不创建生产订单、不生成生产二维码、返回描述性错误）
    - 注释标注：`# Feature: batch-traceability, Property 66: 生产订单创建校验拒绝且无副作用`
    - **Validates: Requirements 23.7**
    - 文件：`tests/test_production_orders.py`

  - [x] 35.6 编写属性测试：生产订单来源关联与单产品单行展示
    - **Property 67: 生产订单来源关联与单产品单行展示**（详情保真记录来源采购订单关联，并以单产品单行展示——每产品占一行、至少含型号与数量）
    - 注释标注：`# Feature: batch-traceability, Property 67: 生产订单来源关联与单产品单行展示`
    - **Validates: Requirements 23.3, 23.4**
    - 文件：`tests/test_production_orders.py`

- [x] 36. 实现 ScanGunInboundService（Req 24）— 扫码枪入库
  - [x] 36.1 实现 `product_stock` / `inbound_scan_records` 及 `POST /api/scan-gun/lookup` 与 `POST /api/scan-gun/inbound`
    - `POST /api/scan-gun/lookup`（`require_admin_or_warehouse()`）：入参 `{ code }`（生产二维码 payload 或裸 `batch_code`），解析 → 检索对应生产订单及关联产品标识供弹窗预置；无法检索到生产订单 → 拒绝「生产二维码无效或对应生产订单不存在」
    - `POST /api/scan-gun/inbound`（`require_admin_or_warehouse()`）：入参 `{ productionOrderId, quantity }`；生产订单存在、`quantity` 为 `1..999999` 整数；单一事务内插入一条 `inbound_scan_records`（数量、操作人、入库时间、关联生产订单 / 产品）→ `UPSERT` `product_stock.on_hand` 累加 `quantity` → 写审计；记录与累加要么全成功要么全不生效；出参含累加后最新库存总数
    - 说明：本节扫码枪入库（`inbound_scan_records` → `product_stock` 成品库存）与供应入库（Req 17 的 `inbound_receipts`，用于领星入库推送）对象 / 来源不同、各自独立记账，互不串扰
    - _Requirements: 24.1, 24.2, 24.3, 24.4, 24.5, 24.6, 24.7_

  - [x] 36.2 编写属性测试：扫码枪按生产二维码检索生产订单
    - **Property 68: 扫码枪按生产二维码检索生产订单**（扫已生成生产订单的二维码检索回同一订单及产品；无法检索到时拒绝、无入库记录、库存不变、返回二维码无效错误）
    - 注释标注：`# Feature: batch-traceability, Property 68: 扫码枪按生产二维码检索生产订单`
    - **Validates: Requirements 24.1, 24.2, 24.5**
    - 文件：`tests/test_scan_gun_inbound.py`

  - [x] 36.3 编写属性测试：扫码枪入库累加库存的原子性与最新库存返回
    - **Property 69: 扫码枪入库累加库存的原子性与最新库存返回**（一组合法入库数量各以单一事务新增一条记录并累加 `product_stock.on_hand`，返回最新库存；最终库存 = 各次数量之和；注入事务失败回滚后记录与库存均不变）
    - 注释标注：`# Feature: batch-traceability, Property 69: 扫码枪入库累加库存的原子性与最新库存返回`
    - **Validates: Requirements 24.3, 24.4**
    - 文件：`tests/test_scan_gun_inbound.py`

  - [x] 36.4 编写属性测试：扫码枪入库数量校验拒绝且无副作用
    - **Property 70: 扫码枪入库数量校验拒绝且无副作用**（数量为空 / 非整数 / `<1` / `>999999` 拒绝、无入库记录、库存不变、返回取值范围错误）
    - 注释标注：`# Feature: batch-traceability, Property 70: 扫码枪入库数量校验拒绝且无副作用`
    - **Validates: Requirements 24.6**
    - 文件：`tests/test_scan_gun_inbound.py`

- [x] 37. Checkpoint - 确保生产订单 / 扫码枪入库相关测试通过
  - Ensure all tests pass, ask the user if questions arise.

- [x] 38. 实现 PurchaseOrderExportService（Req 25）— 采购单导出
  - [x] 38.1 添加 `openpyxl` 依赖并实现 `GET /api/purchase-orders/<id>/export`
    - 在 `requirements.txt` 中固定新增 `openpyxl`（Excel 写库）
    - `require_operations()`（运营 / 管理员，Req 25.7）；采购订单存在（否则拒绝、不生成文件）；必填列所需值齐备（否则拒绝并指明缺失必填列、不生成文件）
    - 生成 `.xlsx`，列的数量 / 名称 / 顺序与规定清单**完全一致**（不多列、不少列、逐一相等）：`标识号, 采购单号, 供应商, 联系人, 采购方, 联系方式, 结算方式, 预付比例, 结算账期, 结算描述, 支付方式, 含税, 费用分配方式, 采购币种, 当前汇率, 运费, 运费币种, 其他费用, 其他费用币种, 采购员, 质检类型, 单据备注, 颜色, 材质, 内含配件, 包装要求, 特殊要求, HS海关编码, 交货周期（天数）, 采购仓库, 计划编号, SKU, 店铺, FNSKU, 是否赠品, 单箱数量, 箱数, 实际采购量, 含税单价, 税率, 预计到货时间, 产品备注, 更新报价, 内含配件(产品), 包装要求(产品), 特殊要求（规避专利）, HS海关编码(产品), 交货周期（天数）(产品)`
    - 必填列（`标识号, 供应商, 含税, 费用分配方式, 采购币种, 采购仓库, SKU, 实际采购量, 含税单价`）填充非空且类型相符：实际采购量为 `>0` 整数、含税单价为 `>0` 且保留 2 位小数；返回 `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet` 文件流
    - _Requirements: 25.1, 25.2, 25.3, 25.4, 25.5, 25.6, 25.7_

  - [x] 38.2 编写属性测试：采购单导出列精确性
    - **Property 71: 采购单导出列精确性**（用 `openpyxl` 读回，列的数量 / 名称 / 顺序与规定清单逐一相等，不多不少）
    - 注释标注：`# Feature: batch-traceability, Property 71: 采购单导出列精确性`
    - **Validates: Requirements 25.1, 25.2, 25.4**
    - 文件：`tests/test_po_export.py`

  - [x] 38.3 编写属性测试：采购单导出必填列填充与类型
    - **Property 72: 采购单导出必填列填充与类型**（必填列非空且类型相符：实际采购量 `>0` 整数、含税单价 `>0` 两位小数）
    - 注释标注：`# Feature: batch-traceability, Property 72: 采购单导出必填列填充与类型`
    - **Validates: Requirements 25.3**
    - 文件：`tests/test_po_export.py`

  - [x] 38.4 编写属性测试：采购单导出校验拒绝且无副作用
    - **Property 73: 采购单导出校验拒绝且无副作用**（不存在的采购订单 / 缺任一必填列值时拒绝、不生成文件、无数据变更、返回描述性错误）
    - 注释标注：`# Feature: batch-traceability, Property 73: 采购单导出校验拒绝且无副作用`
    - **Validates: Requirements 25.5, 25.6**
    - 文件：`tests/test_po_export.py`

- [x] 39. 实现 InventorySyncService（Req 26）— 库存同步至领星
  - [x] 39.1 实现 `POST /api/inventory-sync`
    - `require_operations()`（运营 / 管理员，Req 26.7）；凭据缺失即在发起网络请求前拒绝、不改本地库存（Req 26.4）；以 `BEGIN IMMEDIATE` 内翻转的进行中守卫防并发重复推送（重复触发拒绝「库存同步正在进行中」、状态不变）
    - 通过 `LingxingIntegrationService` 将各产品 `product_stock.on_hand` 单向推送领星；成功（30 秒内）保存领星返回标识 + 原始响应、记录同步时间、置本次同步状态 `PUSHED`、写审计（`object_type='INVENTORY_SYNC'`）；失败 / 超时**保持库存不变**、置 `FAILED`、写含领星错误审计并返回描述性错误；仅推送、不拉取
    - _Requirements: 26.1, 26.2, 26.3, 26.4, 26.5, 26.6, 26.7_

  - [x] 39.2 编写属性测试：库存同步成功持久化并置为已推送
    - **Property 74: 库存同步成功持久化并置为已推送**（领星成功返回时推送各产品当前库存、保存标识 + 原始响应、记录同步时间、置 `PUSHED`，恰写一条含操作人 / 操作类型 / 时间 / 结果的审计；仅推送不拉取）
    - 注释标注：`# Feature: batch-traceability, Property 74: 库存同步成功持久化并置为已推送`
    - **Validates: Requirements 26.1, 26.2, 26.3**
    - 文件：`tests/test_inventory_sync.py`
    - 说明：mock 领星返回成功、用可注入时钟

  - [x] 39.3 编写属性测试：库存同步凭据缺失降级、失败保库存与进行中守卫
    - **Property 75: 库存同步凭据缺失降级、失败保库存与进行中守卫**（缺失凭据在发起网络请求前拒绝、领星调用次数为 0、库存不变、返回未配置错误；失败 / 超时保持库存不变、置 `FAILED`；进行中重复触发拒绝、不发起新调用、状态不变，并发至多一次真实调用）
    - 注释标注：`# Feature: batch-traceability, Property 75: 库存同步凭据缺失降级、失败保库存与进行中守卫`
    - **Validates: Requirements 26.4, 26.5, 26.6**
    - 文件：`tests/test_inventory_sync.py`
    - 说明：mock 领星 HTTP 客户端计数真实调用、`ThreadPoolExecutor` 并发

- [x] 40. 实现 FactoryProgressService（Req 27）— 工厂进度可见性
  - [x] 40.1 实现 `GET /api/purchase-orders/<id>/factory-progress`
    - `require_operations()`（运营 / 管理员，Req 27.4）；采购订单存在（否则拒绝、返回不存在错误）
    - 计算：`productionOrderGenerated = EXISTS(production_orders WHERE purchase_order_id = ?)`；`latestQuantity = COALESCE(SUM(inbound_scan_records.quantity for that production order), 0)`（`0..999,999,999` 整数）；未生成生产订单时 `productionOrderGenerated=false`、`latestQuantity=0`
    - 出参：`{ productionOrderGenerated: bool, latestQuantity: int }`
    - _Requirements: 27.1, 27.2, 27.3, 27.4_

  - [x] 40.2 编写属性测试：工厂进度映射
    - **Property 76: 工厂进度映射**（布尔值等于是否存在关联生产订单、最新数量等于关联生产订单累计入库产品数量；未生成时为 `false` / `0`；不存在的采购订单查询被拒、返回不存在错误）
    - 注释标注：`# Feature: batch-traceability, Property 76: 工厂进度映射`
    - **Validates: Requirements 27.1, 27.2, 27.3**
    - 文件：`tests/test_factory_progress.py`

- [x] 41. Checkpoint - 确保采购单导出 / 库存同步 / 工厂进度相关测试通过
  - Ensure all tests pass, ask the user if questions arise.

- [x] 42. 前端：三角色新增能力视图（`static/app_v2.js` + `templates/index_v2.html`）
  - [x] 42.1 在 `static/app_v2.js` + `templates/index_v2.html` 新增各能力视图并接入导航
    - 通用**系统设置页**（管理员）：承载通用设置与领星凭据（`appId` / `appSecret` / `id`）录入，凭据一律脱敏展示（复用后端 `mask_secret` 输出）
    - 将「录入员管理」更名为**用户管理**（管理员）：含新增用户与编辑角色（仓管 / 运营 / 管理员）
    - **生产订单生成**（仓管）：弹窗选择采购订单 → 生成生产订单 + 生产二维码展示 + 单产品单行数据
    - **扫码枪入库**（仓管）：扫码 → 检索生产订单 → 弹窗填写数量，另提供单行入库按钮，展示累加后库存
    - **采购单导出**按钮（运营）、**库存同步**触发（运营）、**工厂进度**查看视图（运营）
    - _Requirements: 21.1, 21.2, 22.1, 23.4, 24.1, 24.2, 25.1, 26.1, 27.1_

  - [x] 42.2 编写静态 / DOM 断言测试
    - 参照 `tests/test_sidebar_styles_static.py` 静态断言风格，断言各能力视图按角色呈现（管理员见系统设置 / 用户管理、仓管见生产订单生成 / 扫码枪入库、运营见采购单导出 / 库存同步 / 工厂进度）、「用户管理」已更名、且系统设置页凭据不呈现完整明文
    - _Requirements: 21.1, 22.1, 23.4, 24.1_
    - 文件：`tests/test_role_capabilities_frontend_static.py`

- [x] 43. 集成与接线
  - [x] 43.1 在 `app.py` 注册全部新路由并接线前端导航
    - 将系统设置、用户管理、生产订单、扫码枪入库、采购单导出、库存同步、工厂进度的所有路由注册进应用，接入前端导航，确保无游离未整合代码；注入可 mock 的 `LingxingHttpClient` 与可注入时钟以便测试
    - _Requirements: 21.1, 22.1, 23.1, 24.1, 25.1, 26.1, 27.1_

  - [x] 43.2 编写属性测试：新增受限操作的三角色授权矩阵（端到端接线后）
    - **Property 59: 新增受限操作的三角色授权矩阵**（角色取自 `{ADMIN, WAREHOUSE, OPERATIONS}` × 受限操作组合——运营专属、仓管专属、管理员专属；仅匹配角色放行，不匹配 / 未认证拒绝且相关表计数不变、返回权限不足或需先认证错误），覆盖全部新路由；领星相关操作对 `LingxingHttpClient` mock / stub
    - 注释标注：`# Feature: batch-traceability, Property 59: 新增受限操作的三角色授权矩阵`
    - **Validates: Requirements 12.2, 12.3, 12.4, 12.5, 12.6, 21.6, 22.6, 23.8, 24.7, 25.7, 26.7, 27.4**
    - 文件：`tests/test_roles_authz.py`

  - [x] 43.3 编写集成测试：三角色端到端链路（mock 领星客户端）
    - 运营创建采购订单 → 仓管生成生产订单 → 扫码枪入库累加库存 → 运营库存同步（mock 领星）+ 工厂进度查看，验证跨服务与事务协作；领星 HTTP 客户端 mock / stub、用可注入时钟，绝不访问真实接口
    - _Requirements: 16.1, 23.1, 24.3, 26.1, 27.1_
    - 文件：`tests/test_role_capabilities_integration.py`

  - [x] 43.4 编写集成测试：真实 v13 库迁移到 user_version 14
    - 以真实 v13 库跑一次 `initialize_database`，验证 `user_version` 落到 14、`app_settings` / `production_orders` / `product_stock` / `inbound_scan_records` 表存在、`users.role` `CHECK` 收敛为三角色、历史 `role='OPERATOR'` 行被迁移为 `WAREHOUSE`，且批次溯源 / 领星集成 / 历史数据行数与字段值不变
    - _Requirements: 8.1, 8.3, 8.4, 8.5, 12.1_
    - 文件：`tests/test_v14_migration.py`

- [x] 44. Final checkpoint - 确保三角色收敛与新增能力全部测试通过
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- 标记 `*` 的子任务为可选（属性测试、单元测试、集成测试、DOM 测试），可为更快的 MVP 跳过；核心实现任务不标记为可选。
- 每个任务引用了具体的（子）需求条款以保证可追溯性。
- 每条属性由单个 Hypothesis 属性测试实现，`@settings(max_examples=100)`，并以注释标注来源属性。
- Checkpoint 用于增量验证；写路径统一 `BEGIN IMMEDIATE → 变更 → COMMIT`，异常 `ROLLBACK` 保证被拒 / 失败请求无副作用。
- `app.py` 与 `traceability/db.py` 为单体 / 共享文件，其实现型任务被交错安排在不同波次以避免并行写冲突；同一测试文件的多个属性测试任务也分置于不同波次。
- 领星集成任务（18–29）由 Requirement 12–20 与对应设计（LingxingIntegrationService、`traceability/lingxing.py` 防腐层、`OPERATIONS`/`WAREHOUSE` 角色、`purchase_orders`/`inbound_receipts` 表、`user_version` 13 迁移、Property 33–58）级联而来，保留全部既有批次溯源任务（1–17）。
- 领星相关测试**一律 mock / stub `LingxingHttpClient`**、令牌时序用**可注入时钟**驱动，绝不访问真实领星接口；`appId` / `appSecret` 尚未下发，真实接口对接为可配置项、相关实时对接延后填充。
- 三角色模型与新增能力任务（30–44）由 Requirement 21–27 与对应设计（三角色收敛 `ADMIN`/`WAREHOUSE`/`OPERATIONS`、`require_warehouse()`/`require_admin_or_warehouse()`、SystemSettingsService / UserManagementService / ProductionOrderService / ScanGunInboundService / PurchaseOrderExportService / InventorySyncService / FactoryProgressService、新增表 `app_settings`/`production_orders`/`product_stock`/`inbound_scan_records`、`user_version` 14 迁移、Property 59–76）级联而来，保留全部既有批次溯源与领星集成任务（1–29）。
- 角色模型收敛：批次生成授权由「仅管理员」放宽为「管理员或仓管」（`require_admin_or_warehouse()`）；原「录入员」现场登记 / 入库能力并入仓管（`require_warehouse()`）；`auth.py` `VALID_ROLES` 收敛为 `{ADMIN, WAREHOUSE, OPERATIONS}` 并撤销 `OPERATOR`。早期 v13 阶段对 `users.role` 的放宽在 v14 三角色收敛（任务 30）最终确定，历史 `OPERATOR` 行迁移为 `WAREHOUSE`。
- 采购单导出新增 `openpyxl` 依赖（任务 38.1）；扫码枪成品入库（`inbound_scan_records` → `product_stock`）与供应入库（`inbound_receipts`，用于领星入库推送）对象 / 来源不同、各自独立记账。
- `app.py`、`traceability/db.py`、`traceability/auth.py`、`static/app_v2.js`、`templates/index_v2.html` 为共享 / 单体文件，其新增实现型任务被交错安排在不同波次以避免并行写冲突；同一测试文件的多个属性测试任务亦分置于不同波次。

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "2.1", "3.1"] },
    { "id": 1, "tasks": ["5.1", "15.1", "2.2", "3.2"] },
    { "id": 2, "tasks": ["6.1", "2.3", "5.2", "15.2"] },
    { "id": 3, "tasks": ["6.2", "2.4", "5.3"] },
    { "id": 4, "tasks": ["7.1", "2.5", "6.9", "6.3", "6.7", "6.6", "6.11"] },
    { "id": 5, "tasks": ["9.1", "6.10", "6.4", "6.8", "7.2"] },
    { "id": 6, "tasks": ["10.1", "6.5", "7.3", "9.2"] },
    { "id": 7, "tasks": ["11.1", "6.12", "7.4", "10.2", "9.3"] },
    { "id": 8, "tasks": ["12.1", "11.2", "10.3", "9.4"] },
    { "id": 9, "tasks": ["14.1", "12.2", "12.5", "11.3", "10.4", "9.5"] },
    { "id": 10, "tasks": ["16.1", "12.3", "14.2", "11.4", "9.6"] },
    { "id": 11, "tasks": ["16.2", "12.4"] },
    { "id": 12, "tasks": ["16.3"] },
    { "id": 13, "tasks": ["16.4"] },
    { "id": 14, "tasks": ["18.1", "19.1", "20.1"] },
    { "id": 15, "tasks": ["20.4", "22.1", "19.2", "20.2"] },
    { "id": 16, "tasks": ["20.8", "23.1", "20.3", "20.5", "22.3"] },
    { "id": 17, "tasks": ["20.11", "20.6", "20.9", "22.4", "23.2"] },
    { "id": 18, "tasks": ["22.2", "27.1", "20.7", "20.10", "20.12", "23.3"] },
    { "id": 19, "tasks": ["24.1", "20.13", "27.2"] },
    { "id": 20, "tasks": ["25.1", "19.3", "24.2"] },
    { "id": 21, "tasks": ["28.1", "24.3", "25.2"] },
    { "id": 22, "tasks": ["24.4", "25.3"] },
    { "id": 23, "tasks": ["24.5", "25.4"] },
    { "id": 24, "tasks": ["24.6", "25.5"] },
    { "id": 25, "tasks": ["24.7", "25.6"] },
    { "id": 26, "tasks": ["28.2", "28.3"] },
    { "id": 27, "tasks": ["30.1"] },
    { "id": 28, "tasks": ["31.1"] },
    { "id": 29, "tasks": ["32.1", "32.2"] },
    { "id": 30, "tasks": ["33.1", "32.3"] },
    { "id": 31, "tasks": ["35.1", "32.4", "33.2"] },
    { "id": 32, "tasks": ["35.2", "33.3"] },
    { "id": 33, "tasks": ["36.1", "35.3"] },
    { "id": 34, "tasks": ["38.1", "35.4", "36.2"] },
    { "id": 35, "tasks": ["39.1", "35.5", "36.3", "38.2"] },
    { "id": 36, "tasks": ["40.1", "35.6", "36.4", "38.3", "39.2"] },
    { "id": 37, "tasks": ["42.1", "38.4", "39.3", "40.2"] },
    { "id": 38, "tasks": ["43.1", "42.2"] },
    { "id": 39, "tasks": ["43.2", "43.3", "43.4"] }
  ]
}
```
