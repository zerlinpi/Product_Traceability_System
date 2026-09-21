# 备份与恢复

> **备份但不能恢复，等于没有备份。**
> 本文档的第二节是**恢复演练**——请按季度实际执行一次，而不是只读一遍。

---

## 1. 命令速查

所有命令在项目根目录执行。Windows 用 `.venv\Scripts\python.exe`，Linux 用 `./.venv/bin/python`。

| 命令 | 作用 |
| --- | --- |
| `python manage.py backup` | 备份 + 自检 + 生成校验清单 |
| `python manage.py backup --keep 30` | 备份后只保留最新 30 份 |
| `python manage.py verify-backup FILE` | 校验备份是否可用（**不改动任何数据**） |
| `python manage.py restore FILE --yes` | 用备份覆盖当前数据库 |
| `python manage.py integrity-check` | 检查当前数据库完整性与外键 |
| `python manage.py audit-verify` | 校验审计账本哈希链是否完整 |
| `python manage.py audit-info` | 审计账本规模、事件分布与触发器状态 |
| `python manage.py db-info` | 结构版本、体量、各表记录数 |
| `python manage.py list-backups` | 列出备份 |
| `python manage.py prune-backups --keep N` | 清理旧备份 |
| `python manage.py preflight` | **升级前**检查（含自动备份） |
| `python manage.py postflight` | **升级后**检查 |

全局参数：`--database PATH`、`--backup-dir PATH`（也可用环境变量 `PTS_DATABASE` / `PTS_BACKUP_DIR`）。

---

## 2. 恢复演练（Restore Drill）

**目的**：证明备份真的能恢复，并让操作者熟悉流程——在出事之前。

**建议频率**：每季度一次，或每次升级前。**务必在非生产机或维护窗口执行。**

### 准备

```bash
cd <项目目录>
python manage.py db-info            # 记下当前记录数，演练后对照
python manage.py backup             # 先给"当前"数据也留一份
```

记下 `db-info` 输出的 **结构版本**、**users 数量**、**audit_events 数量**。

### 步骤 1：挑一份备份并校验

```bash
python manage.py list-backups
python manage.py verify-backup exports/backups/traceability_XXXXXXXXTXXXXXX.db
```

**预期输出**（关键是最后一行）：

```
校验：exports/backups/traceability_20260722T103000_0800.db
    结构版本  21
    自检      ok
    SHA-256   3da4b315...
[OK] 备份可用
```

若显示 `[失败] 备份不可用`，**停止演练**，按第 6 节排查。这一份备份不能用。

### 步骤 2：确认服务已停止

```bash
# Windows：关闭 start.bat 的窗口，或
netstat -ano | findstr :5080
# Linux：
sudo systemctl stop product-traceability
```

**服务必须停止。** 恢复会替换数据库文件，而运行中的服务持有自己的句柄和 `-wal` 边车文件。

### 步骤 3：执行恢复

```bash
python manage.py restore exports/backups/traceability_20260722T103000_0800.db --yes
```

不加 `--yes` 会被拒绝（退出码 2）——这是防止误操作，不是错误。

**预期输出**：

```
[OK] 恢复完成
    来源          exports/backups/traceability_20260722T103000_0800.db
    当前数据库    data/traceability.db
    恢复前副本    exports/backups/traceability_pre-restore_20260722T110000.db
    结构版本      21
```

**注意 `恢复前副本` 这一行**：恢复前的数据库已自动另存。恢复错了可以从它回滚。

### 步骤 4：恢复后核对

```bash
python manage.py integrity-check     # 期望：[OK] 通过
python manage.py db-info             # 期望：结构版本 21，记录数与步骤 0 一致
```

### 步骤 5：启动并抽查

```bash
python manage.py postflight          # 期望：[OK] 检查通过，升级完成
# 启动服务
```

浏览器登录后抽查三件事：

1. **登录成功**（证明 `users` 表可用）
2. **产品查询**能查到族谱（证明溯源链完整）
3. **扫码查询**能返回批次（证明批次数据在位）

### 步骤 6：记录演练结果

在运维日志中记下：日期、备份文件名、`verify-backup` 结果、`db-info` 记录数、抽查结果、执行人。

### 演练失败的处置

| 现象 | 原因 | 处置 |
| --- | --- | --- |
| `verify-backup` 报 SHA-256 不匹配 | 文件被改过或损坏 | 换一份备份；查存储介质 |
| `verify-backup` 报缺少校验清单 | 备份被单独复制走、丢了 manifest | 换一份；或人工确认后手动恢复 |
| `verify-backup` 报结构版本高于程序 | 用旧程序恢复新数据 | 升级程序后再恢复 |
| `restore` 后 `integrity-check` 失败 | 备份本身已损坏 | 从 `恢复前副本` 回滚，换一份备份 |
| 恢复后登录不了 | 备份是更早的账号状态 | 用该备份对应的管理员密码登录 |

**回滚到恢复前状态**：

```bash
python manage.py restore exports/backups/traceability_pre-restore_XXXXXXXXTXXXXXX.db --yes
```

---

## 3. 备份内容与校验

`backup` 生成的是一对文件：

| 文件 | 内容 |
| --- | --- |
| `traceability_<时间戳>.db` | 数据库快照 |
| `traceability_<时间戳>.db.manifest.json` | SHA-256、结构版本、大小、各表记录数、自检结果、SQLite 版本 |

**校验清单是恢复安全的基础**：`restore` 会先核对 SHA-256，
确保要装回去的正是当初备份的那一份。

### 为什么不是直接复制文件

系统使用 SQLite 的**在线备份 API**。直接 `cp` 一个运行中的数据库可能拿到撕裂的写入——
已提交的数据可能只在 `-wal` 边车文件里，主文件还没有。在线备份 API 即使在服务运行时
也能得到一致快照。

### 一致性保证

- 备份后**立即**执行 `PRAGMA integrity_check`，不通过就报错并中止（不会留下一份"看起来像备份"的坏文件）
- 记录 `foreign_key_check` 与结构版本
- 备份前检查磁盘空间：**磁盘写满导致备份中途失败，比没有备份更糟，因为它看起来像有备份**

---

## 4. 保留策略与异机副本

### 自动清理

```bash
python manage.py backup --keep 30      # 备份后只保留最新 30 份
python manage.py prune-backups --keep 30
```

清理会同时删除对应的 `.manifest.json`。**`prune` 只管理 `traceability_*.db`**，
不会碰 `*_pre-restore_*.db` 安全副本。

### 每日自动备份（Linux / systemd timer）

`/etc/systemd/system/pts-backup.service`：

```ini
[Unit]
Description=Product traceability daily backup

[Service]
Type=oneshot
User=traceability
WorkingDirectory=/opt/product-traceability
ExecStart=/opt/product-traceability/.venv/bin/python manage.py backup --keep 30
```

`/etc/systemd/system/pts-backup.timer`：

```ini
[Unit]
Description=Run the traceability backup daily

[Timer]
OnCalendar=*-*-* 02:30:00
Persistent=true

[Install]
WantedBy=timers.target
```

启用：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now pts-backup.timer
systemctl list-timers pts-backup.timer
```

### 每日自动备份（Windows 计划任务）

```bat
schtasks /create /tn "PTS 每日备份" /sc daily /st 02:30 ^
  /tr "cmd /c cd /d E:\Product_Traceability_System && .venv\Scripts\python.exe manage.py backup --keep 30"
```

### 异机副本（**必做**）

```bash
# Linux：同步到受控文件服务器
rsync -av --delete /opt/product-traceability/exports/backups/ \
      backup-host:/srv/pts-backups/$(hostname)/

# Windows：映射网络盘后复制
robocopy "E:\Product_Traceability_System\exports\backups" "\\nas\pts-backups\%COMPUTERNAME%" /MIR
```

> **同一块盘上的备份不能抵御磁盘损坏。** 备份的价值取决于它有多少份副本、分布在哪里。
> 至少保留：本机一份 + 异机一份。有条件再加一份离线副本。

---

## 5. 升级流程中的备份

```bash
# 1) 升级前检查（会自动生成一份升级前备份）
python manage.py preflight

# 2) 停止服务，替换程序文件（保留 data/ 与 settings）

# 3) 升级后检查
python manage.py postflight
```

`preflight` 会在下列任一情况**中止并返回非零退出码**：

- 数据库自检未通过
- 外键校验发现问题
- **数据库结构版本高于当前程序支持**（降级会导致数据丢失）
- 备份盘空间不足

`postflight` 检查：结构版本是否达到目标、完整性、外键、必要表、是否还有账号。
失败时它会直接打印回滚命令。

---

## 6. 排查

| 症状 | 排查方向 |
| --- | --- |
| `数据库尚未创建` | 先启动一次服务初始化数据库 |
| `备份文件自检失败` | 磁盘故障或 SQLite 版本异常；检查 `dmesg` / 磁盘健康 |
| `磁盘空间不足` | 清理旧备份或扩容；`prune-backups --keep N` |
| `恢复后的数据库自检失败` | 备份已损坏；用 `恢复前副本` 回滚 |
| `verify-backup` 全部正常但服务起不来 | 检查 `settings.bat` / `settings.env` 与端口占用 |
| `db-info` 显示结构版本低于目标 | 正常：启动一次服务会执行增量迁移 |

### 查看数据库健康

```bash
python manage.py db-info
python manage.py integrity-check
```

`db-info` 中的 **空闲页（freelist_count）** 长期很大说明有大量删除未回收；
**残留 -wal 为 True** 说明服务可能正在运行，恢复前务必先停服务。
