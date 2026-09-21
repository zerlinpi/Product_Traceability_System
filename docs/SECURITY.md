# 安全说明

> 本文档描述**当前实现**的安全姿态，包含**已知缺口**。
> 凡与代码冲突，以代码 + 测试为准。

---

## 0. 部署边界（决定了威胁模型）

本系统是**单工厂、局域网优先**部署的轻量溯源系统。默认部署形态：

- 一台服务器电脑运行服务，其他工位用浏览器访问
- SQLite 单文件数据库，Waitress 单进程
- **不面向公网**

威胁模型的优先级因此是：

1. **内部越权**（已登录的低权限账号做不该做的事）
2. **误操作与数据损坏**（比外部攻击更可能发生）
3. **出站请求被滥用**（SSRF）
4. 外部攻击（仅在错误地暴露到公网时成为主要风险）

> **不要把未配置 HTTPS 和访问控制的 5080 端口直接暴露到互联网。**
> 若必须公网访问，请先阅读第 4 节。

---

## 1. 认证

| 项 | 实现 |
| --- | --- |
| 密码存储 | Werkzeug `generate_password_hash`（scrypt/PBKDF2），**不存明文** |
| 会话 | Flask session cookie，有效期 12 小时 |
| 会话失效 | `users.session_version` 变化即失效：改密、改角色、改范围都会 +1 |
| 首登强制改密 | `must_change_password = 1`，除 `me`/`logout`/`change-password` 外一律 428 |
| 密码长度 | 8–128 字符 |
| 默认管理员 | `admin` / `Admin@12345`，**首次登录强制修改** |
| 登录审计 | `USER_LOGIN` / `USER_LOGOUT` / `USER_PASSWORD_CHANGED` 写入 `audit_events` |

### CSRF

所有 `POST` / `PUT` / `PATCH` / `DELETE` 必须携带 `X-CSRF-Token`，
与 session 内 token 做**恒定时间比对**（`secrets.compare_digest`），否则 403。
token 在登录与改密时重新生成。

### 生产守卫

`PTS_ENV=production` 时，启动阶段**直接抛 `RuntimeError` 拒绝启动**：

- `PTS_SECRET_KEY` 为默认值/示例值，**或**长度 < 32
- `PTS_BOOTSTRAP_ADMIN_PASSWORD` 为默认值/示例值

即：默认凭据在结构上无法用于生产部署。

### ⚠️ 已知缺口

| 缺口 | 影响 | 状态 |
| --- | --- | --- |
| **无登录失败限流** | 可对登录接口做在线暴力破解 | 待处理（第七目标） |
| 无账号级失败计数 / 短时 lockout | 同上 | 待处理 |
| 无密码复杂度校验 | 仅校验长度 | 有意为之：长度优先于复杂度 |
| `PTS_SECRET_KEY` 无自动轮换 | 需人工更换，更换后所有会话失效 | 可接受 |

---

## 2. 授权

**角色**：恰好三个互斥角色 `ADMIN` / `WAREHOUSE` / `OPERATIONS`，账号持单值 `role`。

**能力表**是权限的**唯一权威来源**（`traceability/capabilities.py`）：

- 25 个 `Capability`，`ROLE_CAPABILITIES` 定义角色到能力的映射
- `ADMIN` 持有全部能力（超集）
- 路由通过 `require_capability(Capability.X)` 判权
- 前端 `allowedViews()` **只负责隐藏 UI**，安全判定始终以后端为准

**全局闸门**：`auth.before_request` 覆盖所有 `/api/` 前缀，仅
`/api/health` 与 `/api/auth/login` 豁免。**不存在匿名访问。**

### 实测授权覆盖（107 个路由）

| 类别 | 数量 |
| --- | --- |
| 完全公开（无需登录） | 4 |
| 无角色守卫（仅需登录） | 27 |
| 显式能力守卫 | 12 |
| 守卫在 service 层 | 10 |

完整矩阵见 [`PERMISSION_MATRIX.md`](PERMISSION_MATRIX.md)，由
`python tools/extract_routes.py --sync` 从源码机器提取，并有 273 项测试锁定。

### ⚠️ 已知缺口

| 缺口 | 影响 | 状态 |
| --- | --- | --- |
| **记录归属权规则未实现** | 仓管之间可互相修改/删除录入记录（`editable_record()` 的归属检查是死代码） | **待业务确认**，见 `PRODUCT_RULES.md` §10 |
| 范围授权表仍写入并回显但**不参与鉴权** | 管理员界面的「分配产品」无权限效果 | 保留为 API 兼容；见 `PERMISSION_MATRIX.md` D4 |
| 鉴权分散在 handler 与 service 两层 | 审计时容易漏看 | 见 D5，提取器已覆盖 |

---

## 3. 出站请求（SSRF）

领星写入地址可被管理员编辑，此前接受**任意** `http(s)://` URL——这是 SSRF 原语。

现由 `traceability/endpoint_policy.py` 约束：

- 默认**仅允许** `openapi.lingxing.com`，且**仅 HTTPS**
- 自定义 endpoint 默认只接受**相对路径**
- 自定义主机需显式白名单 `PTS_LINGXING_ALLOWED_HOSTS`
- 拒绝 `file://` / `ftp://` / `data:` 等协议
- 拒绝回环、链路本地、私有、保留、组播地址（含 IPv6）
- 按名称拒绝 `localhost` / `*.local` / `*.internal` / `metadata`
- **域名解析到内网地址同样拒绝**（防 DNS 重绑定）
- **每次重定向都重新校验**——这是「只在请求前校验」会漏掉的一环

策略在**保存时**与**传输层**两处生效。82 项测试见 `tests/test_ssrf_policy.py`。

---

## 4. 传输与部署硬化

### 必须做到

- 公网访问**必须**启用 HTTPS，并把 `PTS_HTTPS_ONLY=1`
- 生产环境 `PTS_ENV=production`
- `PTS_SECRET_KEY` 用随机值：`openssl rand -hex 32`
- 只把服务监听在 `127.0.0.1`，由反向代理对外

### Nginx 参考配置

见 `deploy/nginx/product-traceability.conf`。

### systemd 硬化

见 `deploy/systemd/product-traceability.service`。

### ⚠️ 已知缺口

| 缺口 | 状态 |
| --- | --- |
| systemd 沙箱指令（`ProtectSystem` / `NoNewPrivileges` / `PrivateTmp` 等）尚未启用 | 待处理（第十九目标） |
| `X-Forwarded-*` 头未做可信代理校验 | 待处理 |
| 无 HSTS / 上传大小 / 请求限流配置 | 待处理 |
| 响应安全头仅 `nosniff` / `SAMEORIGIN` / `Referrer-Policy`，无 CSP | 待处理 |

---

## 5. 数据完整性与可恢复性

| 机制 | 说明 |
| --- | --- |
| 唯一约束 | 产品主码、部件码、批次码、审计事件号等**数据库层**强制 |
| 事务 | 库存扣减、二维码生成、记录创建、库存流水**同一事务**，不足则整体回滚 |
| 幂等 | 现场写操作带 `Idempotency-Key`；**先占位再执行**，插入即互斥锁 |
| 备份 | SHA-256 校验清单 + 生成后立即 `integrity_check` |
| 恢复 | 先校验 → 拒绝高版本降级 → 自动保留恢复前副本 → 原子替换 |
| 降级保护 | 旧程序**拒绝**启动高版本数据库（`preflight` 与 `restore` 双重拦截） |

详见 [`BACKUP_RESTORE.md`](BACKUP_RESTORE.md) 与 [`API_CONTRACT.md`](API_CONTRACT.md) §6。

---

## 6. 审计

`audit_events` 记录：登录/登出/改密、用户创建与更新、产品创建、二维码生成、
部件扫码、录入完成、记录修改与删除、批次质量状态变更、领星推送与库存同步。

- 登录账号是**真实审计身份**，前端不能伪造操作员名称
- 关键变更记录前/后状态与原因

### ⚠️ 已知缺口

| 缺口 | 影响 | 状态 |
| --- | --- | --- |
| `audit_events` 不是 append-only | 应用层未禁止 `UPDATE`/`DELETE`，无数据库触发器 | 待处理（第十七目标） |
| 无 hash chain | 无法证明事件未被篡改 | 待处理 |
| 无 `request_id` 关联 | 跨事件串联困难 | 待处理 |

> 作为产品追溯的证据链，**这是当前最值得优先补齐的安全缺口之一**。

---

## 7. 输入处理

| 面 | 现状 |
| --- | --- |
| SQL | 全部使用参数化查询 |
| 用户名校验 | 正则限定 1–50 位字母数字与 `._-` |
| 文本字段 | 统一 `clean_text` 限长 |
| 图片上传 | magic bytes 校验 + 随机文件名 + 5 MB 上限 |
| 前端渲染 | **部分使用 `innerHTML`** |

### ⚠️ 已知缺口

| 缺口 | 影响 | 状态 |
| --- | --- | --- |
| Excel/CSV 导出未防公式注入 | 以 `=` `+` `-` `@` 开头的文本字段可能被 Excel 当公式执行 | 待处理（第三十七目标） |
| 图片未做像素尺寸/解压炸弹限制 | 恶意图片可耗尽内存 | 待处理（第三十八目标） |
| SVG 用户上传未明确禁止 | SVG 可携带脚本 | 待核实 |
| `innerHTML` 未全部审计 | 潜在 XSS | 待处理（第十二目标） |

---

## 8. 安全测试

| 测试文件 | 覆盖 |
| --- | --- |
| `tests/test_permission_matrix.py` | 273 项：每个角色 × 每个无参路由的授权矩阵 |
| `tests/test_ssrf_policy.py` | 82 项：出站地址策略、重定向、DNS 重绑定、传输层 |
| `tests/test_idempotency.py` | 25 项：重放、并发、崩溃残留、键释放 |
| `tests/test_backup_restore.py` | 52 项：校验和篡改检出、降级保护、恢复回路 |
| `tests/test_lingxing_authz.py` | 领星操作的角色授权与无副作用 |

---

## 9. 缺口优先级（按风险）

| 优先级 | 缺口 | 理由 |
| --- | --- | --- |
| **高** | `audit_events` 非 append-only | 追溯证据可被静默修改 |
| **高** | 记录归属权规则未实现 | 已登录账号可改删他人记录 |
| **高** | 登录失败无速率限制 | 在线暴力破解 |
| 中 | 导出公式注入 | 打开导出的 Excel 可能执行公式 |
| 中 | systemd 沙箱未启用 | 进程被攻破后横向移动更容易 |
| 中 | `innerHTML` 未全审计 | 潜在 XSS |
| 中 | 图片解压炸弹 | 内存耗尽 |
| 低 | 无 CSP / HSTS | 需配合 HTTPS 部署 |

---

## 10. 报告问题

发现安全问题请**不要**开公开 issue。请通过内部渠道联系维护者，
附上复现步骤、影响范围与受影响版本（`python manage.py db-info` 输出的结构版本）。
