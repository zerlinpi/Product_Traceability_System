# 批次溯源实现核对记录

核对日期：2026-07-22

## 核对原则

- 以 `tasks.md` 为任务基线，先检查现有代码和测试，再补齐 `[~]`、`[-]`、`[ ]`；已实现项不重复重写。
- 当前数据库最终目标是 `user_version = 14`。v12（批次溯源）、v13（领星采购 / 入库）和 v14（三角色、设置、生产订单、成品库存）仍按顺序执行；因此“真实 v11/v12/v13 库迁移”测试以最终落到 v14 为启动成功标准，同时逐项检查中间迁移应新增的表、列、约束和历史数据保真。
- 所有领星测试使用可注入 mock / stub，不访问真实领星接口。真实 `appId`、`appSecret`、账号 id、端点和官方签名算法下发后，只替换配置与 `SignStrategy`，不改业务调用方。

## 文档与代码对照

| 任务范围 | 实现位置 | 验证位置 | 核对结论 |
| --- | --- | --- | --- |
| 1–8：迁移、批次码、库存扣减、生成、二维码、正反向追溯 | `traceability/db.py`、`traceability/codes.py`、`app.py` | `test_batch_migration.py`、`test_batch_codes.py`、`test_batch_inventory.py`、`test_batch_generation*.py`、`test_batch_supplier_trace.py` | 已实现；唯一约束、事务回滚、库存守恒和并发串行化均有测试 |
| 9–13：批次登记、质量、记录查询、扫码只读查询 | `app.py`、`static/app_v2.js` | `test_batch_entry.py`、`test_batch_auth.py`、`test_batch_quality.py`、`test_batch_query.py`、`test_batch_trace_query.py` | 原部分完成项已补齐：授权、初始质量状态、审计操作人、过滤排序、非法时间、只读快照、无效码和未认证无副作用 |
| 14–17：旧入口守卫、前端、集成与回归 | `app.py`、`templates/index_v2.html`、`static/app_v2.js` | `test_batch_legacy.py`、`test_batch_frontend_static.py`、`test_batch_integration.py`、`test_system.py` | 走步机旧逐台入口返回 409；非走步机兼容行为和历史查询保留 |
| 18–29：v13、领星防腐层、采购 / 收货 / 推送 / 状态 / 审计 | `traceability/db.py`、`traceability/auth.py`、`traceability/lingxing.py`、`app.py` | `test_lingxing_*.py`、`test_purchase_orders.py`、`test_inbound_receipts.py` | 已实现；补齐三角色领星授权、签名时间戳一致携带、刷新成功与失败回退、并发推送至多一次真实 mock 调用 |
| 30–34：v14、三角色、系统设置、用户管理 | `traceability/db.py`、`traceability/auth.py`、`app.py` | `test_v14_migration.py`、`test_roles_authz.py`、`test_system_settings.py`、`test_user_management.py` | `VALID_ROLES` 恰为 ADMIN / WAREHOUSE / OPERATIONS；OPERATOR 迁移为 WAREHOUSE；凭据只脱敏回显 |
| 35–41：生产订单、扫码枪入库、Excel、库存同步、工厂进度 | `app.py`、`traceability/xlsx_export.py` | `test_production_orders.py`、`test_scan_gun_inbound.py`、`test_po_export.py`、`test_inventory_sync.py`、`test_factory_progress.py` | 已实现；采购订单与生产订单一对至多一，二维码唯一；库存累加原子；导出列精确；同步有并发守卫 |
| 42–44：三角色前端与端到端接线 | `templates/index_v2.html`、`static/app_v2.js`、`static/styles_v2.css`、`app.py` | `test_role_capabilities_frontend_static.py`、`test_role_capabilities_integration.py`、`test_roles_authz.py` | 导航、页面与后端权限同时收敛；不是只在前端隐藏按钮 |

## 本轮重点修正

1. 批次质量审计事件使用当前登录人的真实姓名，不再出现空操作人。
2. 批次查询属性覆盖补齐，非法时间范围、无效码和未认证请求均无数据副作用。
3. 领星响应脱敏兼容 `access_token` / `accessToken`、`refresh_token` / `refreshToken`。
4. 采购与库存同步使用数据库进行中守卫；并发测试证明同一操作至多触发一次外部 mock 调用。
5. 扫码枪查询成功后聚焦数量，确认入库后清空并回到二维码输入框；失败后同样回到二维码输入框。批次登记页面进入时直接聚焦扫码框。
6. 移动端侧栏规则合并到唯一的 860px 断点，390px 视口不再被桌面侧栏边距挤出屏幕；密集表格不再逐字换行。
7. 可见中文辅助文字下限统一到 11px，继续使用灰白底、细边框和克制绿色，不增加渐变或大面积光晕。

## 兼容性结论

- 历史 `machines`、`trace_records`、`product_code_sets` 和既有二维码不删除、不改码；走步机旧写入口停用，查询和非走步机兼容链保留。
- 新增写路径统一使用事务和唯一约束。批次码、生产二维码、产品主码、部件码、审计事件号均由数据库约束兜底。
- 扫码枪按 HID 键盘输入处理主键盘 Enter 和小键盘 Enter；焦点恢复由页面逻辑保证，不依赖扫描枪厂商 SDK。
- SQLite + WAL 适合当前单工厂局域网规模。若后续跨厂区或高并发，优先抽离数据库适配层到 PostgreSQL/MySQL，现有服务边界和外部标识字段可继续复用。

## 验证证据

- Property 1–76 均存在对应测试，无缺号；纯转换与边界域使用 Hypothesis 100 组，数据库状态、角色矩阵和并发属性使用隔离数据库、确定性矩阵或线程竞争验证。
- 定向回归：批次授权 2 项通过；扫码 / 角色静态 35 项通过；并发推送与库存同步 9 项通过；领星角色、签名、令牌 11 项通过；脱敏与侧栏断点 23 项通过。
- Edge 视觉与交互验收：1488×1058 与 390×844；桌面和移动端均无整页横向溢出、无乱码、无小于 11px 的可见文本；账户菜单开关、批次登记聚焦、扫码枪初始聚焦和失败后回焦均通过。
- 对比图：`tests/ui-design-comparison-final.png`（参考界面 / 当前实现并排）。
- 最终全量回归：`221 passed in 1386.52s (0:23:06)`；使用曾触发 32 位前缀边界失败的固定 Hypothesis 种子复验，全部通过。
- `tasks.md` 共 174 个数字任务 / 子任务标记均已回填为 `[x]`，未完成、部分完成和跳过标记为 0。

## 本地验收账号

- 管理员：`admin.demo` / `AdminDemo@12345`。
- 仓管：`warehouse.demo` / `Warehouse@12345`（已授权当前全部产品）。
- 运营：`operations.demo` / `Operations@12345`。
- 新数据库首次启动管理员：`admin` / `Admin@12345`，首次登录必须改密；现有数据库若已改密，以管理员当前密码为准。

以上账号只用于本地验收，部署前应修改密码或删除测试账号。
