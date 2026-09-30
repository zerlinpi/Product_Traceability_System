# 发布门禁清单

每次把改动发到现场服务器前逐项过一遍。**每一条都能被机器验证的，就已经在 CI 里了**——
这份清单只列机器查不了、或者机器查了但你需要亲手确认的东西。

## 这份清单的立场

自动化已经覆盖的，不要在这里重复手工做一遍（那样只会让人养成跳过清单的习惯）。
CI 每次 push 到 `main` 都会跑，红了就是没通过：

| CI 作业 | 覆盖什么 |
| --- | --- |
| Lint | `ruff check .` |
| Permission matrix is in sync | `tools/extract_routes.py --check`——权限矩阵与代码一致 |
| Docs match the code | `tools/check_docs_consistency.py`——文档描述的是当前系统 |
| Frontend | `pnpm typecheck`、`pnpm build`、**已提交 bundle 与源码指纹一致**、浏览器 smoke E2E |
| Test (ubuntu / windows) | 测试数量守卫（`tools/check_test_count.py`）+ 全量 pytest + coverage 门槛 |
| （pytest 内） | `python tools/check_frontend_build.py` 的指纹校验、审计链与权限矩阵的回归测试 |

**CI 全绿是前提，不是结论。** 下面是它查不到的。

---

## 1. 代码与 CI

- [ ] `main` 上的最新 commit 的 CI **六项全绿**（不是「大部分绿」）
- [ ] 本次改动**没有降低** `coverage --cov-fail-under` 的门槛
- [ ] 本次改动**没有降低** `tests/BASELINE_COUNT`
      （降了就说明删了测试，要么补回来，要么在提交信息里说明为什么这个测试已失效）
- [ ] 提交信息说清了「改了什么、为什么」，不是 `update` / `fix`

## 2. 前端产物（最容易漏的一项）

`static/dist` **随仓库发布**——现场服务器没有 Node，不会自己构建。

- [ ] 如果这轮改过 `frontend/`：已运行 `python tools/build_frontend.py`
      并**把 `static/dist` 一起提交**
- [ ] `python tools/check_frontend_build.py` 通过

> 忘了这一步的后果是：仓库里跑的是旧界面，而 Python 测试断言的是
> 「当前 `static/dist` 的内容」，**不会发现**。CI 的 Frontend 作业会拦住它，
> 但前提是你把 `static/dist` 也提交了——只提交源码时 CI 会红，别把它当成噪音。

## 3. 数据库与迁移

- [ ] 本次是否动了 `traceability/db.py` 的结构版本或迁移？
      - 是 → 必须走 `docs/UPGRADE.md` 的升级流程，**升级前先备份**
      - 否 → 结构版本应保持不变
- [ ] `python manage.py preflight` 通过（在准备升级的现场机器上跑）
- [ ] 有一份**升级前的备份**，并且 `python manage.py verify-backup FILE` 通过

## 4. 配置与密钥

- [ ] `settings.bat` / `settings.env` 已配置，且**不在仓库里**
- [ ] `PTS_SECRET_KEY` 至少 32 位且**不是示例值**（生产模式下不满足会直接拒绝启动）
- [ ] `PTS_BOOTSTRAP_ADMIN_PASSWORD` 已改掉默认值
- [ ] `PTS_ENV=production`
- [ ] 领星相关端点与凭据（若本次涉及）已确认

## 5. 部署

- [ ] 部署物完整：程序文件 + `static/` + `data/`（**保留**）+ `settings`（**保留**）
- [ ] 已知本轮的**业务语义变化**（如果有），并通知了使用方
- [ ] 停机窗口已通知（现场是生产线在用，不要挑白班高峰）

## 6. 部署后验证（必做，不能只看进程起来了）

- [ ] `python manage.py postflight` 通过
- [ ] `curl -fsS http://127.0.0.1:5080/api/health` 返回 `"status":"ok"`
- [ ] 浏览器打开首页，**能登录**，且「今日」指标有数
- [ ] 用管理员账号走一遍本次改动涉及的那个业务动作
      （例：改了扫码枪 → 实际扫一枪；改了领星推送 → 实际推一单）
- [ ] 换一个**非管理员角色**登录，确认权限没被意外放开或收紧
- [ ] `journalctl -u product-traceability -n 100` 无 `Unhandled traceability error`

## 7. 回滚预案

- [ ] 知道回滚要用哪一份备份（升级前那份）
- [ ] 知道回滚步骤：`docs/BACKUP_RESTORE.md` 的恢复演练流程
- [ ] 确认回滚不会丢数据：**升级后的新数据会随回滚丢失**，
      如果升级后已经有人录了数据，回滚前先备份当前库

---

## 发布记录（每次填一行）

| 日期 | commit | 谁发的 | CI | postflight | 抽查通过 | 备注 |
| --- | --- | --- | --- | --- | --- | --- |
| | | | | | | |

> 这张表的价值不在流程，而在**事后追溯**：现场报「某天开始不对」时，
> 能立刻知道那天上了什么。
