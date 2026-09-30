# 日常运维

面向**已经上线、正在运行**的系统。回答「今天该看什么、出问题先敲哪条命令」。

## 这份文档不重复什么

| 要做的事 | 看哪里 |
| --- | --- |
| 备份、恢复演练、保留策略、异机副本 | `docs/BACKUP_RESTORE.md` |
| 升级流程与升级前后的检查 | `docs/UPGRADE.md` |
| 权限模型、密钥、systemd 沙箱、对外暴露面 | `docs/SECURITY.md` |
| 数据表结构、字段含义 | `docs/DATA_MODEL.md` |
| 业务规则（批次、库存、二维码、扫码枪、领星） | `docs/PRODUCT_RULES.md` |
| 接口字段 | `docs/API_CONTRACT.md` |
| 上线前的发布门禁 | `docs/RELEASE_CHECKLIST.md` |

本文只讲**运行期的操作**。

---

## 1. 服务启停

### Windows（现场部署的实际形态）

| 操作 | 做法 |
| --- | --- |
| 启动 | 双击 `start.bat`，**保持窗口开启** |
| 停止 | 关闭该窗口，或在该窗口按 `Ctrl+C` |
| 看输出 | 就是那个窗口——**没有日志文件** |

`start.bat` 会先自检：环境缺失或缺依赖时自动调用 `install.bat`；
端口被占用或健康检查不通过会直接报错并 `pause`，不会留下一个半死的进程。

> **窗口被关掉 = 服务停止。** 现场最常见的「系统打不开」就是这个。
> 需要无人值守，用 Windows 任务计划程序以「不管用户是否登录都要运行」启动，
> 或改用下面的 Linux + systemd。

### Linux（systemd）

```bash
sudo systemctl start   product-traceability
sudo systemctl stop    product-traceability
sudo systemctl restart product-traceability
sudo systemctl status  product-traceability
```

单元文件在 `deploy/systemd/product-traceability.service`，要点：

- `User=traceability`、`WorkingDirectory=/opt/product-traceability`
- `EnvironmentFile=/opt/product-traceability/settings.env`
- `Restart=on-failure`、`RestartSec=5` —— **崩溃会自动重启**
- `PrivateTmp=true`、`NoNewPrivileges=true` —— 沙箱已启用（见 `docs/SECURITY.md`）

改完单元文件要 `sudo systemctl daemon-reload` 再 `restart`。

---

## 2. 健康检查与日志

### 存活探针

```bash
curl -fsS http://127.0.0.1:5080/api/health
# {"data":{"status":"ok","time":"..."},"ok":true}
```

`/api/health` **不需要登录**（否则探针自己先被拦下），也不查数据库——
它回答的是「进程还在监听吗」，不是「数据库还好吗」。数据库要看第 5 节。

### 日志

系统**不写日志文件**，全部输出到标准输出/错误：

| 部署形态 | 日志在哪 |
| --- | --- |
| Windows | `start.bat` 的窗口 |
| Linux systemd | `journalctl -u product-traceability` |

```bash
journalctl -u product-traceability -n 200 --no-pager      # 最近 200 行
journalctl -u product-traceability -f                     # 实时跟随
journalctl -u product-traceability --since "1 hour ago"   # 近一小时
```

应用会记录两类值得注意的行：

- `Database constraint rejected a request` —— 请求违反了数据库约束（通常是并发下的重复提交，业务层会返回明确错误）
- `Unhandled traceability error` —— 未预期的异常，**需要看堆栈**

---

## 3. 日常巡检

全部通过 `manage.py`。在项目目录下执行（Windows 用 `.venv\Scripts\python.exe`）：

```bash
python manage.py db-info            # 结构版本 + 各表体量
python manage.py integrity-check    # 数据库完整性与外键
python manage.py audit-verify       # 审计账本哈希链是否完整
python manage.py list-backups       # 现有备份
```

> **三条命令要求数据库已迁移**：`audit-verify`、`audit-info`、`login-status`
> 读取的是迁移后才存在的表/列。库比程序旧时它们会**直接说明版本差异并指向
> 升级步骤**，而不是抛出 `no such column` 之类的报错——
> 因为「库比程序旧」正是升级过程中的正常状态。
> 其余命令在两种结构上都能用。

### 建议频率

| 频率 | 做什么 | 为什么 |
| --- | --- | --- |
| 每天 | 打开一次首页，看「今日」指标是否有数 | 一次 HTTP 走通全链路，比看进程更实在 |
| 每周 | `integrity-check` + `audit-verify` | 存储层与审计链的早期预警 |
| 每月 | 一次恢复演练（见 `docs/BACKUP_RESTORE.md`） | **没演练过的备份不算备份** |
| 每月 | `db-info`，看表体量与磁盘余量 | 领星同步与审计账本会持续增长 |

`audit-verify` 失败意味着审计账本被改动过或损坏，**不要忽略**：
先保留现场（复制 `data/` 整目录），再排查。

---

## 4. 账号与登录

### 登录被锁定

连续失败会触发锁定（阈值与时长见 `traceability/login_guard.py`）。
`LOGIN_POLICY` 可通过环境变量调整。

```bash
python manage.py login-status                          # 谁被锁了、失败多少次
python manage.py login-unlock --username 张三
python manage.py login-unlock --ip 192.168.1.50
python manage.py login-unlock --username 张三 --ip 192.168.1.50
```

> 解锁是**运维动作，不是业务动作**。解锁前确认是本人忘记密码，
> 而不是有人在试密码——后者应该去看审计账本（`audit-info`）而不是解锁。

### 忘记密码

管理员登录后到「用户管理」重置。首次登录会强制改密。

### 首次部署后的必做项

1. 用初始管理员登录，**立即修改密码**
2. 在 `settings.bat` / `settings.env` 里改掉 `PTS_BOOTSTRAP_ADMIN_PASSWORD`，
   否则下次重建数据库会退回默认值
3. 按需建立仓库与运营账号（权限见 `docs/PERMISSION_MATRIX.md`）

---

## 5. 数据库

SQLite，WAL 模式，文件固定在 `data/traceability.db`
（服务器读的是配置里的 `DATABASE`，`app.py` 里写死为这个路径）。

> `manage.py` 另有一个 `--database PATH` / `PTS_DATABASE` 参数，
> 指向的是**维护工具要操作的库**，不是服务器在用的那个。
> 想让两者一致（通常都要一致），把 `--database` 指到 `data/traceability.db`。
> 用 `--database` 指向别处时务必确认清楚——**`restore` 会覆盖它指向的那个库**。

```bash
python manage.py integrity-check    # 完整性 + 外键
python manage.py db-info            # 结构版本与体量
```

### 看到「结构版本不一致」

当前期望的 `user_version` 由代码决定（`traceability/db.py`）。
启动日志里会打印迁移过程。不一致通常意味着：

- 迁移中断 → 从升级前备份恢复（`docs/UPGRADE.md`）
- 换错了数据库文件 → 用备份恢复

### 看到 `database is locked`

WAL 模式下并发写会短暂互斥，正常会重试成功。**持续**出现要查两件事：

1. 是否有第二个进程打开了同一个库（比如同时跑了 `manage.py` 与服务器）
2. 所在磁盘是否为网络盘或同步盘（OneDrive / 网盘同步目录）——
   SQLite 的共享内存映射在部分文件系统上不可靠，
   **`data/` 必须放在本机磁盘**，不要放进同步目录

> 本机开发时若 WAL 并发写出现 `OperationalError`，
> 先用最小 sqlite3 脚本确认是不是宿主环境（把 `journal_mode` 换成 `delete`
> 若恢复正常，就是环境问题，不要改业务并发逻辑）。

---

## 6. 磁盘与日志清理

需要关注的是 `data/`（数据库 + WAL）与 `exports/backups/`（备份，默认目录，
可用 `--backup-dir` / `PTS_BACKUP_DIR` 改）：

```bash
python manage.py list-backups
python manage.py prune-backups --keep 30    # 保留最新 30 份
```

- **不要手工删 `-wal` / `-shm` 文件**，让 SQLite 自己管理
- 日志不占磁盘（不写文件），但 systemd 的 journal 会增长：
  `journalctl --vacuum-time=30d`

---

## 7. 出问题了先看哪

| 现象 | 先做什么 |
| --- | --- |
| 浏览器打不开 | 确认服务在跑（`start.bat` 窗口还在 / `systemctl status`），再 `curl /api/health` |
| 局域网其他机器连不上 | 防火墙放行 TCP 5080 入站；确认工位与服务器同一网段 |
| 页面能开但接口全 401 | 会话过期或 `SECRET_KEY` 变了（改过 `settings` 会让所有会话失效） |
| 页面空白或样式错乱 | `static/dist` 是否完整——它随仓库发布，缺失说明部署时漏了文件 |
| 扫码枪没反应 | 蓝牙配对与浏览器权限，见 `docs/PRODUCT_RULES.md` |
| 领星推送失败 | 检查 `settings` 里的凭据与 `PTS_LINGXING_*` 端点配置 |
| 升级后启动报错 | `docs/UPGRADE.md` 的排查表 |

---

## 8. 上线前的发布门禁

见 `docs/RELEASE_CHECKLIST.md`。
