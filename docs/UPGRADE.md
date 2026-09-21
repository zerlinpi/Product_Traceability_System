# 升级指南

> 升级的核心风险只有一个：**数据库迁移不可逆**。
> 所以流程被设计成「先证明能回滚，再动数据」。

---

## 1. 一句话流程

```bash
python manage.py preflight     # 检查 + 自动备份（不通过就中止）
#  停止服务 → 替换程序文件（保留 data/ 与 settings）→ 启动服务
python manage.py postflight    # 校验升级结果
```

---

## 2. 详细步骤

### 步骤 1：升级前检查

```bash
python manage.py preflight
```

它会依次检查并输出：

| 检查项 | 中止条件 |
| --- | --- |
| 数据库完整性 | `integrity_check` 未通过 |
| 外键 | `foreign_key_check` 发现问题 |
| 结构版本 | **数据库版本高于程序支持**（拒绝降级） |
| 备份盘空间 | 不足 64 MB |

通过后**自动生成一份升级前备份**。**这就是回滚点，请记下文件名。**

若返回非零退出码，**不要继续升级**，按 `docs/BACKUP_RESTORE.md` 第 6 节排查。

### 步骤 2：停止服务

```bash
# Windows：关闭 start.bat 窗口
# Linux：
sudo systemctl stop product-traceability
```

### 步骤 3：替换程序文件

**必须保留**：

- `data/traceability.db`（及 `-wal` / `-shm`）
- `settings.bat`（Windows）或 `settings.env`（Linux）
- `exports/backups/`（备份目录）

**可以覆盖**：`app.py`、`server.py`、`manage.py`、`traceability/`、`static/`、
`templates/`、`deploy/`、`tests/`、`docs/`、`tools/`、`requirements.txt`。

> 不要把开发机上带有数据的 `data/` 目录复制到生产机。

### 步骤 4：更新依赖并启动

```bash
# Windows
install.bat
start.bat

# Linux
./install-linux.sh
sudo systemctl start product-traceability
sudo journalctl -u product-traceability -f
```

**首次启动会执行数据库迁移。** 迁移只增不删：只建表、加列、加索引和做旧数据映射，
不会删除或重写历史二维码、标签、溯源记录。

### 步骤 5：升级后检查

```bash
python manage.py postflight
```

检查结构版本是否达到目标、完整性、外键、必要表、是否还有账号。
失败时它会直接打印回滚命令。

### 步骤 6：人工抽查

| 项目 | 期望 |
| --- | --- |
| 登录 | 原有账号密码可用 |
| 产品查询 | 能查到族谱 |
| 扫码查询 | 能返回批次 |
| 批次登记 | 能正常扫码提交 |
| 库存 | 数量与升级前一致 |

---

## 3. 回滚

```bash
sudo systemctl stop product-traceability      # 先停服务
python manage.py restore <升级前备份> --yes
sudo systemctl start product-traceability
```

同时把程序文件换回旧版本。

> **注意**：如果升级后已经产生了新数据，回滚会丢失它们。
> 回滚前先用 `python manage.py backup` 保留当前状态。

---

## 4. 迁移链

数据库结构版本记录在 `PRAGMA user_version`。当前目标版本见
`python manage.py db-info` 输出的「程序目标」。

| 版本 | 内容 |
| --- | --- |
| v12 | 批次溯源 |
| v13 | 领星采购 / 入库结构；重建角色表 |
| v14 | 三角色收敛（`OPERATOR` → `WAREHOUSE`）、设置、生产订单、成品库存 |
| v15 | 重建采购单，加入产品关联 |
| v16 | 批次计划改为可空（成品可无 BOM） |
| v17 | 外采标记 + 库存同步表 |
| v18 | 热点索引 |
| v19 | 推送守卫起始时间 |
| v20 | 幂等键表 |

完整说明见 [`DATA_MODEL.md`](DATA_MODEL.md) 第 1 节。

### 降级保护

系统**拒绝**用旧程序启动高版本数据库：

- `preflight` 检测到会中止
- `restore` 检测到会拒绝
- `verify-backup` 会标记该备份不可用

这是有意的：旧代码不理解新结构，强行运行会静默损坏数据。

---

## 5. 常见问题

| 问题 | 说明 |
| --- | --- |
| 启动日志显示正在执行迁移 | 正常，属增量迁移 |
| 启动报 `生产模式必须设置至少 32 位的 PTS_SECRET_KEY` | 生产守卫；按提示配置 `settings.bat` / `settings.env` |
| 启动报 `生产模式必须修改 PTS_BOOTSTRAP_ADMIN_PASSWORD` | 同上，改掉默认管理员密码 |
| `postflight` 报结构版本不一致 | 迁移未完成；查看启动日志中的迁移输出 |
| `postflight` 报没有任何账号 | 数据库可能被替换错；用备份恢复 |

---

## 6. 待补项

- `docs/SECURITY.md`、`docs/OPERATIONS.md`、`docs/ARCHITECTURE.md` 尚未编写
- `RELEASE_CHECKLIST.md`（第四十目标的发布门禁清单）尚未建立
