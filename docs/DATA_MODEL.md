# 数据模型（真实基线）

> 基线：`user_version = 19`，共 **33 张业务表**。
> 本文档的表清单、主键与外键数量由 `data/traceability.db` 实际 schema 提取，
> 非手写规范。迁移实现见 `traceability/db.py`。

---

## 1. 迁移链（v11 → v19）

迁移**只增不删**：不删除或重写历史二维码、标签、溯源记录。
每个版本在一个 `BEGIN IMMEDIATE` 事务内完成，含 `PRAGMA user_version` 的写入，因此失败即整体回滚。

| 版本 | 内容 | 性质 |
| --- | --- | --- |
| **v11** | 基线 schema | — |
| **v12** | 批次溯源：`production_batches`、`batch_trace_records`、`production_batch_supplier_consumption` 等 | 纯增量 |
| **v13** | 领星集成：采购/入库结构；**重建角色表**（SQLite 无法修改已有 CHECK 约束） | 增量 + 重建 |
| **v14** | 三角色收敛（`OPERATOR` → `WAREHOUSE`）、设置、生产订单、成品库存 | 增量 + 数据映射 |
| **v15** | `purchase_orders` 重建，加入 `product_model_id` | 重建 |
| **v16** | `production_batches.trace_plan_id` 改为可空（成品可无 BOM） | 列放宽 |
| **v17** | `production_orders.is_external` 列 + `product_stock_sync` 表 | 纯增量 |
| **v18** | 6 个热点外键/过滤列补索引 | 纯增量（索引） |
| **v19** | `purchase_orders.push_started_at`、`inbound_receipts.push_started_at` | 纯增量（加列） |

### ⚠️ 当前部署状态

`data/traceability.db` 实际为 **`user_version = 18`**，落后代码一个版本。
下次启动将自动执行 v19。v19 仅新增两个可空 `TEXT` 列，不影响既有数据。

### 启动时附加动作

`_clear_abandoned_guards()` 在**每次启动**运行（不属于任何迁移版本），
强制释放残留的推送/同步进行中标记：

- `purchase_orders.push_in_progress` → 0
- `inbound_receipts.push_in_progress` → 0
- `app_settings['inventory_sync.in_progress']` → '0'

理由：数据库初始化期间不可能有请求在途，因此仍被置位的标记必然来自崩溃的进程。
若不清除，该单据会永远返回 409「正在进行中」，需人工改库。

### v19 解决的问题

v19 之前，进程在推送中途死亡会把 `push_in_progress` 永久留在 1。
v19 记录守卫获取时间（`push_started_at`），使下一次尝试能识别被遗弃的守卫并接管。

---

## 2. 表清单（33 张，按领域分组）

格式：`表名` — 列数 / 主键 / 外键数

### 2.1 主数据

| 表 | 列 | 主键 | 外键 |
| --- | --- | --- | --- |
| `users` | 11 | `id` | 0 |
| `suppliers` | 8 | `id` | 0 |
| `product_families` | 7 | `id` | 0 |
| `part_types` | 11 | `id` | 1 |
| `product_models` | 14 | `id` | 2 |

### 2.2 供应与库存

| 表 | 列 | 主键 | 外键 |
| --- | --- | --- | --- |
| `supplier_inventory_batches` | 11 | `id` | 1 |
| `supplier_inventory_movements` | 11 | `id` | 5 |
| `product_stock` | 3 | `product_model_id` | 1 |
| `product_stock_sync` | 6 | `product_model_id` | 1 |

### 2.3 产品配置与批次溯源

| 表 | 列 | 主键 | 外键 |
| --- | --- | --- | --- |
| `trace_plans` | 8 | `id` | 1 |
| `trace_plan_slots` | 6 | `id` | 3 |
| `production_batches` | 9 | `id` | 3 |
| `production_batch_supplier_consumption` | 5 | `id` | 3 |
| `batch_trace_records` | 12 | `id` | 3 |

### 2.4 采购、生产与领星同步

| 表 | 列 | 主键 | 外键 |
| --- | --- | --- | --- |
| `purchase_orders` | 16 | `id` | 4 |
| `inbound_receipts` | 13 | `id` | 2 |
| `production_orders` | 7 | `id` | 3 |
| `inbound_scan_records` | 7 | `id` | 3 |
| `app_settings` | 6 | `setting_key` | 1 |
| `system_settings` | 3 | `setting_key` | 0 |

### 2.5 历史逐台兼容（只读/非走步机）

| 表 | 列 | 主键 | 外键 |
| --- | --- | --- | --- |
| `machines` | 8 | `id` | 2 |
| `product_code_batches` | 11 | `id` | 3 |
| `product_code_sets` | 11 | `id` | 5 |
| `product_code_set_parts` | 4 | `product_code_set_id, position` | 2 |
| `part_label_batches` | 10 | `id` | 2 |
| `part_labels` | 15 | `id` | 3 |
| `trace_records` | 14 | `id` | 4 |
| `trace_record_parts` | 3 | `trace_record_id, position` | 2 |

### 2.6 扫码会话与审计

| 表 | 列 | 主键 | 外键 |
| --- | --- | --- | --- |
| `scan_sessions` | 7 | `station_id` | 3 |
| `scan_session_items` | 3 | `station_id, position` | 2 |
| `audit_events` | 13 | `id` | 1 |

### 2.7 范围授权（**写入但不再参与鉴权**）

| 表 | 列 | 主键 | 外键 |
| --- | --- | --- | --- |
| `user_product_model_permissions` | 2 | `user_id, product_model_id` | 2 |
| `user_supplier_permissions` | 2 | `user_id, supplier_id` | 2 |

> **⚠️ 偏差 D4（详见 `PERMISSION_MATRIX.md`）**：这两张表仍被
> `_replace_user_scope()` 写入，并通过 `user_dict()` 以
> `productModelIds` / `supplierIds` 返回前端，但**不再拦任何请求**
> （`current_operator_id()` 恒返回 `None`）。
> 管理员界面上的「分配产品给仓管」会持久化并回显，但无权限效果。
> 保留原因：API 向后兼容。

---

## 3. 关键唯一性约束

以下约束由数据库层强制，是溯源正确性的基础：

| 约束 | 表 | 说明 |
| --- | --- | --- |
| 产品主码全局唯一 | `product_code_sets` | 一个主码只能属于一套 |
| 部件码全局唯一 | `part_labels` | 一个部件码只能归属一个成品 |
| 非空供应商单件序列号全局唯一 | `part_labels` | 允许为空，非空则唯一 |
| 一个产品主码只能归档一次 | `trace_records` | 防止重复归档 |
| 一个生产批次至多一条登记 | `batch_trace_records` | 批次一次性登记 |
| 一个采购订单至多一个生产订单 | `production_orders` | 含唯一生产二维码 |
| 批次码全局唯一 | `production_batches` | — |
| 审计事件号唯一 | `audit_events` | — |
| 同部件下供应批次号唯一 | `supplier_inventory_batches` | — |
| 部件编码唯一 | `part_types` | — |
| 产品型号编码唯一 | `product_models` | — |
| 供应商编码唯一 | `suppliers` | — |

---

## 4. 库存一致性规则

- **事实来源是流水表** `supplier_inventory_movements`：入库、领用、调整每次变动都记录
  `quantity_delta` 与**变动后结存**（`balance_after`）。
- `supplier_inventory_batches` 的可用量是**投影**，由流水推导。
- 扣减发生在**产品码生成时**，而非产品定义时：按「套数 × 每套用量」计算。
  库存不足则**整次事务回滚**，不产生半批二维码或错账。
- 供应批次调整**不能把入库数量改到已领用数量以下**。
- 成品库存 `product_stock` 由 `inbound_scan_records` 累加，两者**同事务**写入。

---

## 5. 状态机字段

以下字段的取值由 CHECK 约束或应用层状态机限制，**散落在各处，尚未集中为 Enum**
（属第二十六目标）：

| 字段 | 取值 | 约束位置 |
| --- | --- | --- |
| `users.role` | `ADMIN` / `WAREHOUSE` / `OPERATIONS` | 表 CHECK + `auth.VALID_ROLES` |
| `batch_trace_records.quality_status` | `ASSEMBLED` / `PASSED` / `HOLD` | 表 CHECK + `transition_batch_quality()` |
| `purchase_orders.sync_status` | `PENDING` / `PUSHED` / `FAILED` | 表 CHECK |
| `product_stock_sync.sync_status` | `PENDING` / `PUSHED` / `FAILED` | 表 CHECK |
| `production_orders.is_external` | `0` / `1` | 表 CHECK |
| `supplier_inventory_movements.movement_type` | 入库 / 领用 / 调整 | 应用层 |

### 质量状态转换（`transition_batch_quality()`，`app.py:3872`）

```
ASSEMBLED --pass--> PASSED     （需 ADMIN）
ASSEMBLED --hold--> HOLD       （需 ADMIN，原因 1-500 字符）
其他状态 --任意--> 拒绝（409），状态不变
```

---

## 6. 时间字段

- 所有时间戳由 `app.config["NOW_PROVIDER"]()` 产生，测试中可注入。
- 存储形式为**字符串**（ISO 8601 风格），**当前未统一为 timezone-aware UTC**
  （属第三十六目标）。
- 历史时间戳**不得无计划重写**。
