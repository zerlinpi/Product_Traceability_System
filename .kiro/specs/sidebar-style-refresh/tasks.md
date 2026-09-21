# Implementation Plan: 左侧导航样式优化（sidebar-style-refresh）

## Overview

本计划将设计文档转化为一系列可增量执行的 CSS 重构步骤，全部工作集中在 `static/styles_v2.css`（侧边栏样式），并在完成后对 `templates/index_v2.html` 做最小的版本号（`?v=`）调整。核心思路：先建立唯一权威的侧边栏区块骨架，再逐块合并品牌头部、分组标签、菜单项/图标、悬停与选中态、页脚与用户区，随后删除重复定义并合并重叠的响应式媒体查询，最后做令牌化收尾、版本号递增与静态校验。每一步都在前一步基础上推进，避免留下未整合的孤立规则。

本特性为 CSS/样式重构，属 UI 呈现范畴。依据设计文档的 PBT 适用性评估，**不引入属性测试**；改用可自动执行的静态检查（选择器出现次数、遮罩去重、令牌引用、无十六进制字面量等）作为验证手段，视觉/交互/无障碍表现通过检查点中的人工评审确认。

## Tasks

- [x] 1. 建立唯一权威侧边栏区块骨架
  - 在 `static/styles_v2.css` 中新增单一 `/* === Sidebar (authoritative) === */` 注释区块，作为侧边栏容器的唯一定义处
  - 定义 `.sidebar`（固定定位、`height:100vh`、桌面态 `display:grid` 列为 `[var(--rail-width)] [1fr]`、背景 `var(--surface)`、右侧 `1px var(--line)` 分隔线、z-index 高于主内容低于弹层）
  - 定义 `.sidebar-rail`（宽 `var(--rail-width)`、纵向 flex、顶部 `.sidebar-brand`、中部 `.rail-nav`、底部 `.rail-footer{margin-top:auto}`）与 `.sidebar-panel`（`flex column`、占满剩余宽、背景引用令牌）
  - 移除旧的 L68 / L490 / L873 三处 `.sidebar` / `.sidebar-rail` / `.sidebar-brand` / `.rail-nav` / `.sidebar-panel` 冲突定义，仅保留本区块
  - 保持既有 rail/panel 双区 DOM 结构与 class 名称，不新增/删除/重命名被 `app_v2.js` 引用的 class；不引入任何 `[data-role]` 显隐规则
  - _Requirements: 1.1, 8.2, 9.1, 9.3, 7.5_

- [x] 2. 合并各子组件为统一权威规则
  - [x] 2.1 统一品牌头部与关闭控件
    - 定义唯一的 `.sidebar-panel-header`，高度使用 `var(--header-height)`，使其底边与顶栏底边对齐（垂直偏移 0px），展示品牌名称文本，底部 `1px var(--line)`
    - 保留 `.sidebar-close` 规则（桌面态隐藏、窄屏态显示由响应式区块处理，见任务 3.2）
    - 合并/删除 L551、L931 等重复的 `.sidebar-panel-header` 定义
    - _Requirements: 2.1, 2.2, 2.3_

  - [x] 2.2 统一分组标签 `.nav-group-label`
    - 定义唯一的 `.nav-group-label`：字号 `12px`、文字色 `var(--muted)`、统一上下外边距；确保展开面板与窄屏抽屉两种形态复用同一套取值（不在响应式区块内重复覆盖字号/颜色/间距）
    - 删除 L566、L951 等重复/冲突的 `.nav-group-label`（及与 `.nav-section-label` 合并的历史定义）
    - _Requirements: 3.1, 3.2, 3.3_

  - [x] 2.3 统一菜单项 `.nav-button` 间距、图标与长文案处理
    - 定义唯一的 `.nav-button`：固定高度 `44px`、左右内边距 `12px`、图标与文字间距 `12px`
    - 定义 `.nav-button .nav-icon` 为 `20px × 20px` 并相对文字垂直居中；图标资源缺失时保持占位与可点击区域（≥44px）
    - 为 `.nav-button` 添加单行省略号声明（`white-space:nowrap; overflow:hidden; text-overflow:ellipsis`），使长文案不改变按钮高度与面板宽度
    - 删除 L76、L573、L952 等重复的 `.nav-button` 定义
    - _Requirements: 4.1, 4.4, 4.5, 10.1, 10.2_

  - [x] 2.4 统一图标栏按钮 `.rail-button`
    - 定义唯一的 `.rail-button`：`46px × 46px` 方形、圆角 `var(--radius)`；图标统一渲染为 `20px × 20px`
    - 删除 L523、L911 等重复的 `.rail-button` 与图标尺寸（L536/L924）定义
    - _Requirements: 4.2, 4.3_

  - [x] 2.5 统一悬停态与选中态（图标栏与面板一致）
    - 未选中 `.rail-button:hover` 与 `.nav-button:hover` 统一引用 `var(--primary-soft)` 浅底
    - `.rail-button.active` / `.nav-button.active` 使用 `var(--primary)` 作文字/图标色、`var(--primary-soft)` 作底色
    - `.nav-button.active` 通过 `inset 3px 0 0 var(--primary)` box-shadow 呈现左侧 3px 强调条，并将 `.nav-icon` 不透明度提升至 `1`
    - 确保 `.active` 规则在源顺序/特指性上优先于纯 `:hover`（选中态压过悬停态）
    - 删除 L77/L78/L534/L535/L583/L584/L964/L965 等重复的 hover/active 定义
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 8.1_

  - [x] 2.6 统一页脚导航与用户区
    - 定义唯一的 `.sidebar-footer-nav`（`margin-top:auto` 贴底、不与最后一个 `.nav-group` 重叠）
    - 使 `.sidebar-footer-button` 高度与内边距与 `.nav-button` 逐项相等，悬停底色引用同一 `var(--primary-soft)` 令牌
    - 保留 `.sidebar-user` 既有可见性策略与 `.sr-only` 行为（辅助技术可读但不占可见布局），跨桌面/窄屏态可见性、占位与尺寸不回归
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 10.4_

  - [x] 2.7 颜色取值令牌化
    - 将侧边栏规则内所有颜色改为 `var(--...)` 引用 `:root` 令牌（如 `--primary`、`--primary-hover`、`--primary-soft`、`--text`、`--muted`、`--surface`、`--line`），消除散落的十六进制字面量（如 `#a0a3ae`、`#616574`、`#f6f7fa`、`#8a9690`、`#54806f` 等）
    - 确认无 fantastic-admin 默认蓝色或任何非绿色系主色；令牌未定义时的回退值与需求列明取值一致（`#1f7a5a`/`#edf6f2` 等仅存在于 `:root`）
    - _Requirements: 1.4, 8.3, 8.4, 8.6_

- [x] 3. 删除重复定义并合并响应式区块
  - [x] 3.1 消除重复的遮罩隐藏声明
    - 保留唯一一处 `.sidebar-overlay { display: none }`（合并 L758 与 L1224 两处重复）
    - 定义唯一的 `.sidebar-overlay` 基础规则
    - _Requirements: 1.2, 7.1_

  - [x] 3.2 合并重叠的媒体查询为唯一断点块
    - 将 `@media (max-width: 760px)`（L417）与两处 `@media (max-width: 860px)`（L767、L1245）整理为：每个阈值最多一个媒体查询规则块，不出现相同阈值重复
    - 在 `< 860px` 断点内统一：`.sidebar.open`（或既有开关类）以固定定位覆盖式抽屉显示于主内容之上；`.sidebar-overlay` 以 `0.3–0.6` 不透明度显示、覆盖抽屉以外整个视口、由 `.hidden` 状态类控制显隐；`.sidebar-close` 显示为可见可点击
    - 在 `≥ 860px` 时隐藏 `.sidebar-overlay` 与 `.sidebar-close`（不可见且不占布局）；桌面态与窄屏态复用同一套权威规则，仅由响应式区块调整布局
    - _Requirements: 1.3, 2.4, 2.5, 7.1, 7.2, 7.3, 7.4, 7.5_

  - [x] 3.3 保留/补齐减少动效偏好
    - 确保存在 `@media (prefers-reduced-motion: reduce)` 规则，使侧边栏相关过渡时长 ≤ 0.01s 且无可感知动画
    - _Requirements: 10.3_

- [x] 4. 检查点 - 确认样式合并完成且无回归
  - 确认所有静态检查通过；如出现疑问请询问用户。同时进行人工评审：桌面态（品牌头部/分组标签/菜单项间距/hover/active/页脚一致）、窄屏抽屉态（展开收起、遮罩点击关闭、焦点管理）、ADMIN 与 OPERATOR 角色可见分组、以及 `aria-*`/`title`/`sr-only` 无障碍属性与选中态对比度（≥ 4.5:1）。

- [x] 5. 更新模板缓存版本号
  - [x] 5.1 递增 `styles_v2.css` 的 `?v=` 版本号
    - 在 `templates/index_v2.html` 中将 `static/styles_v2.css` 的 `?v=20260719.4` 更新为严格更大的新值（按既有格式递增，如 `20260719.5`）
    - 确认全部对 `static/styles_v2.css` 的引用采用同一新版本号，不混用新旧值；文件名与路径保持不变
    - 不改动 `static/app_v2.js` 内容（其自身版本引用与逻辑保持不变，除非任务 2 中确有新增状态类需同步——本次无）
    - _Requirements: 11.1, 11.2, 11.3, 9.2, 9.4_

- [x] 6. 静态校验
  - [x] 6.1 编写并运行侧边栏样式静态检查
    - 校验每个侧边栏顶层选择器（`.sidebar`、`.sidebar-rail`、`.sidebar-brand`、`.rail-nav`、`.rail-button`、`.nav-button`、`.nav-group-label`、`.sidebar-panel`、`.sidebar-user`、`.sidebar-footer-nav`）在非媒体查询作用域内作为独立规则选择器恰好出现 1 次
    - 校验 `.sidebar-overlay { display: none }` 全表恰好出现 1 次；`max-width: 760px` 与 `max-width: 860px` 各自最多一个媒体查询块
    - 校验 `.nav-button.active` / `.rail-button.active` 引用 `--primary` 与 `--primary-soft`；侧边栏规则内（除 `:root` 外）无 `#RGB`/`#RRGGBB` 十六进制字面量
    - 校验 `templates/index_v2.html` 中 `styles_v2.css` 的 `?v=` 新值严格大于旧值且所有引用一致
    - **对应 Property 1–19（静态检查部分）**
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 5.2, 8.1, 8.3, 11.1, 11.2_

- [x] 7. 最终检查点 - 确认全部检查通过
  - 确认所有静态检查通过、人工视觉/交互/角色/无障碍评审无遗留问题；如有疑问请询问用户。

## Notes

- 标记 `*` 的子任务为可选（静态检查脚本），可为快速交付跳过，但建议执行以保证合并质量。
- 本特性依据设计文档不引入属性测试（PBT），验证以自动化静态检查 + 检查点人工评审为主。
- 每个任务引用具体需求条款以保证可追溯性。
- 绝大多数改动集中在单一文件 `static/styles_v2.css`，因此写同一文件的任务被安排在不同波次以避免冲突；版本号改动位于独立文件 `templates/index_v2.html`。
- 检查点用于增量验证，确保合并过程不产生视觉/交互回归。

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["2.1"] },
    { "id": 2, "tasks": ["2.2"] },
    { "id": 3, "tasks": ["2.3"] },
    { "id": 4, "tasks": ["2.4"] },
    { "id": 5, "tasks": ["2.5"] },
    { "id": 6, "tasks": ["2.6"] },
    { "id": 7, "tasks": ["2.7"] },
    { "id": 8, "tasks": ["3.1", "5.1"] },
    { "id": 9, "tasks": ["3.2"] },
    { "id": 10, "tasks": ["3.3"] },
    { "id": 11, "tasks": ["6.1"] }
  ]
}
```
