# Requirements Document

## Introduction

本特性对正式前端页面 `templates/index_v2.html` 所使用的左侧导航（侧边栏）样式进行重构，样式源文件为 `static/styles_v2.css`。当前侧边栏样式存在同一批选择器在多处重复且相互冲突的定义，以及多组重叠的响应式媒体查询，导致间距、图标尺寸、激活态、品牌区高度等在不同规则块之间互相覆盖，呈现杂乱、不一致的观感。

本次重构的目标是：在保留项目品牌绿色令牌系统与既有「窄图标栏（rail）+ 展开面板（panel）」双区 DOM 结构的前提下，将侧边栏样式合并为唯一一套权威规则，统一品牌头部、分组标签、菜单项间距、悬停态与选中态，规范图标尺寸与对齐，并整理响应式（窄屏抽屉 + 遮罩）行为。改动范围限定于左侧导航区域及其响应式行为，主要修改 `static/styles_v2.css`，`templates/index_v2.html` 仅在必要时做最小调整。

## Glossary

- **侧边栏样式表 (Sidebar_Stylesheet)**：`static/styles_v2.css` 中负责左侧导航呈现的全部 CSS 规则集合。
- **侧边栏 (Sidebar)**：页面左侧固定定位的导航容器，对应选择器 `.sidebar`，由图标栏与展开面板组成。
- **图标栏 (Rail)**：始终可见的窄图标导航列，对应选择器 `.sidebar-rail`，其按钮为 `.rail-button`。
- **展开面板 (Panel)**：显示分组标签与带文字菜单项的区域，对应选择器 `.sidebar-panel`，主菜单为 `.main-nav`，菜单项为 `.nav-button`。
- **品牌头部 (Brand_Header)**：面板顶部展示品牌名称的区域，对应选择器 `.sidebar-panel-header`。
- **分组标签 (Group_Label)**：菜单分区的提示文字，对应选择器 `.nav-group-label`。
- **选中态 (Active_State)**：导航项处于当前选中状态时的视觉表现，对应 `.active` 状态类。
- **悬停态 (Hover_State)**：指针悬停在导航项上时的视觉表现，对应 `:hover` 伪类。
- **遮罩 (Overlay)**：窄屏抽屉展开时覆盖主内容的半透明层，对应选择器 `.sidebar-overlay`。
- **设计令牌 (Design_Token)**：定义在 `:root` 中的 CSS 自定义属性，如 `--primary`、`--primary-soft`、`--rail-width` 等。
- **窄屏抽屉 (Drawer)**：视口宽度小于响应式断点时，侧边栏以覆盖式抽屉形式展开的形态。
- **状态类 (State_Class)**：由 `static/app_v2.js` 切换、样式据此呈现的类，如 `.active`、`.open`、`.hidden`。
- **样式版本号 (Stylesheet_Version)**：`templates/index_v2.html` 中引用样式表的查询参数 `?v=`，用于突破浏览器缓存。

## Requirements

### Requirement 1: 合并为唯一权威样式规则

**User Story:** 作为前端维护者，我希望侧边栏的每个选择器只有一处权威定义，以便样式行为可预测、易于维护。

#### Acceptance Criteria

1. THE 侧边栏样式表 SHALL 使每个侧边栏顶层选择器（`.sidebar`、`.sidebar-rail`、`.sidebar-brand`、`.rail-nav`、`.rail-button`、`.nav-button`、`.nav-group-label`、`.sidebar-panel`、`.sidebar-user`、`.sidebar-footer-nav`）在非媒体查询作用域内作为独立规则选择器的出现次数恰好为 1。
2. THE 侧边栏样式表 SHALL 使 `.sidebar-overlay` 的 `display: none` 声明在整个样式表中的出现次数恰好为 1。
3. THE 侧边栏样式表 SHALL 使 `max-width: 760px` 与 `max-width: 860px` 两个响应式断点各自在样式表中最多对应一个媒体查询规则块，不出现相同阈值的重复媒体查询。
4. WHERE 侧边栏样式规则集合中出现颜色取值，THE 侧边栏样式表 SHALL 通过 `var(--...)` 引用 `:root` 设计令牌，且除 `:root` 声明块外不出现十六进制颜色字面量（`#RGB` / `#RRGGBB`）。

### Requirement 2: 统一品牌头部

**User Story:** 作为用户，我希望品牌头部整齐并与顶栏基线对齐，以便获得一致的视觉起点。

#### Acceptance Criteria

1. THE 品牌头部 SHALL 使用 `--header-height` 令牌值作为其自身高度，且其底边与页面顶栏底边对齐于同一水平基线（两者垂直偏移为 0px）。
2. THE 品牌头部 SHALL 展示品牌名称文本。
3. THE 品牌头部 SHALL 包含关闭控件 `.sidebar-close`。
4. WHILE 视口宽度不小于 860px，THE 侧边栏样式表 SHALL 将 `.sidebar-close` 控件隐藏使其不可见且不占据布局空间。
5. WHILE 视口宽度小于 860px，THE 侧边栏样式表 SHALL 将 `.sidebar-close` 控件显示为可见且可点击。

### Requirement 3: 统一分组标签

**User Story:** 作为用户，我希望分组标签有一致的层级样式，以便快速区分菜单分区。

#### Acceptance Criteria

1. THE 分组标签 SHALL 以 12px 字号并使用 `--muted` 令牌（或等价的次要文字色令牌）作为文字色呈现，作为分区提示。
2. THE 侧边栏样式表 SHALL 为所有 `.nav-group-label` 应用完全相同的字号（12px）、文字色（`--muted`）与上下外边距取值，使任意两个 `.nav-group-label` 实例的上述取值逐项完全一致。
3. THE 侧边栏样式表 SHALL 在展开面板与窄屏抽屉两种形态下为 `.nav-group-label` 复用同一套字号、颜色与间距取值，不因响应式区块产生差异。

### Requirement 4: 统一菜单项间距与图标

**User Story:** 作为用户，我希望所有菜单项高度、内边距和图标尺寸一致，以便获得整齐的列表观感。

#### Acceptance Criteria

1. THE 侧边栏样式表 SHALL 为所有 `.nav-button` 应用相同的固定高度 44px、相同的左右内边距 12px，以及图标与文字之间 12px 的固定间距，使同一面板内任意两个 `.nav-button` 的高度、左右内边距与图标文字间距取值完全相同。
2. THE 侧边栏样式表 SHALL 为所有 `.rail-button` 应用相同的 46px × 46px 方形尺寸与 `--radius`（8px）圆角，使同一图标栏内任意两个 `.rail-button` 的宽、高与圆角取值完全相同。
3. THE 侧边栏样式表 SHALL 将图标栏内 `.rail-button` 的图标统一渲染为 20px × 20px 尺寸。
4. THE 侧边栏样式表 SHALL 为 `.nav-button .nav-icon` 应用统一的 20px × 20px 图标尺寸，并在按钮内相对文字基线垂直居中对齐。
5. IF `.nav-button` 文字过长导致内容超出按钮可用宽度，THEN THE 侧边栏样式表 SHALL 通过单行省略号保持按钮高度维持在 44px 不变。

### Requirement 5: 统一悬停态与选中态

**User Story:** 作为用户，我希望悬停和选中状态在图标栏与面板中表现一致且使用品牌绿色，以便清晰识别当前所在位置。

#### Acceptance Criteria

1. WHILE 指针悬停在未选中的 `.rail-button` 或 `.nav-button` 上，THE 侧边栏样式表 SHALL 应用 `--primary-soft` 浅底悬停态，且图标栏与面板使用相同的悬停底色令牌。
2. WHILE `.rail-button` 或 `.nav-button` 处于 `.active` 状态，THE 侧边栏样式表 SHALL 使用 `--primary` 作为文字/图标颜色并使用 `--primary-soft` 作为底色。
3. WHILE `.nav-button` 同时处于 `.active` 状态与悬停态，THE 侧边栏样式表 SHALL 使选中态样式优先于悬停态样式呈现。
4. WHILE `.nav-button` 处于 `.active` 状态，THE 侧边栏样式表 SHALL 在其左侧以 `--primary` 颜色呈现 3px 的强调条（通过 inset box-shadow 实现）。
5. WHILE `.nav-button` 处于 `.active` 状态，THE 侧边栏样式表 SHALL 将 `.nav-icon` 的不透明度提高至 1 以强化选中态。

### Requirement 6: 统一页脚导航与用户区

**User Story:** 作为用户，我希望页脚导航项与主菜单项风格一致，以便整个侧边栏保持统一。

#### Acceptance Criteria

1. THE 侧边栏样式表 SHALL 使 `.sidebar-footer-nav` 通过 `margin-top: auto` 贴底显示，且不与最后一个 `.nav-group` 重叠。
2. THE 侧边栏样式表 SHALL 使 `.sidebar-footer-button` 的高度与内边距与 `.nav-button` 逐项相等（差异为 0px）。
3. THE 侧边栏样式表 SHALL 使 `.sidebar-footer-button` 的悬停底色引用与 `.nav-button` 相同的设计令牌 `--primary-soft`。
4. THE 侧边栏样式表 SHALL 使 `.sidebar-user` 在桌面态与窄屏态下的可见性、占位与尺寸相较重构前保持不变。
5. THE 侧边栏样式表 SHALL 保持 `.sidebar-user` 的 `.sr-only` 行为（对辅助技术可读但不占据可见布局空间）。

### Requirement 7: 响应式抽屉与遮罩

**User Story:** 作为窄屏设备用户，我希望侧边栏以抽屉形式展开并可通过遮罩关闭，以便在小屏上顺畅使用导航。

#### Acceptance Criteria

1. WHILE 视口宽度不小于 860px，THE 侧边栏样式表 SHALL 隐藏 `.sidebar-overlay`。
2. WHILE 视口宽度小于 860px 且 `.sidebar` 带有 `.open` 状态类，THE 侧边栏样式表 SHALL 以覆盖式（固定）定位将 `.sidebar` 抽屉显示于主内容之上。
3. WHILE 视口宽度小于 860px 且 `.sidebar` 带有 `.open` 状态类，THE 侧边栏样式表 SHALL 显示不透明度介于 0.3–0.6 的 `.sidebar-overlay` 遮罩。
4. THE 侧边栏样式表 SHALL 使 `.sidebar-overlay` 覆盖抽屉以外的整个视口以承接点击关闭操作，并由 `.hidden` 状态类控制其显隐。
5. THE 侧边栏样式表 SHALL 使桌面态与窄屏抽屉态复用同一套侧边栏权威规则，仅通过响应式区块调整布局。

### Requirement 8: 保留品牌令牌与设计令牌系统

**User Story:** 作为品牌负责人，我希望重构保留项目品牌绿色系，以便不偏离既有视觉识别。

#### Acceptance Criteria

1. WHEN 导航项进入选中态（active），THE 侧边栏样式表 SHALL 将该项的文字颜色、图标颜色与左侧强调条颜色渲染为 `--primary`（`#1f7a5a`），并将其选中态背景渲染为 `--primary-soft`（`#edf6f2`）。
2. THE 侧边栏样式表 SHALL 使用既有设计令牌的取值作为对应尺寸：`--sidebar-width`（`270px`）用于侧边栏展开宽度、`--rail-width`（`64px`）用于图标栏宽度、`--header-height`（`64px`）用于头部高度、`--radius`（`8px`）用于导航项与容器圆角。
3. THE 侧边栏样式表 SHALL 使所有主色与强调色（文字、图标、强调条、选中/悬停背景）均解析为既有品牌绿色令牌值（`--primary`、`--primary-hover`、`--primary-soft`），不得出现任何色相非绿色系的主色取值。
4. THE 侧边栏样式表 SHALL NOT 引入 fantastic-admin 默认蓝色或任何未在既有品牌绿色令牌集合中定义的主色。
5. WHEN 未选中的导航项处于悬停（hover）态，THE 侧边栏样式表 SHALL 应用一个区别于选中态且不使用非绿色系主色的悬停样式。
6. IF 所引用的设计令牌未定义或无法解析，THEN THE 侧边栏样式表 SHALL 回退至本需求各条款中列明的具体取值（如 `#1f7a5a`、`270px`、`64px`、`8px`），并保持品牌绿色系不变。

### Requirement 9: 保留 DOM 结构与状态类契约

**User Story:** 作为前端维护者，我希望重构不破坏既有结构与脚本约定，以便无需改动 `app_v2.js` 逻辑。

#### Acceptance Criteria

1. THE 侧边栏样式表 SHALL 沿用既有 rail/panel 双区 DOM 结构与既有 class 名称集合（`.sidebar`、`.sidebar-rail`、`.sidebar-panel`、`.rail-button`、`.nav-button`、`.nav-group`、`.nav-group-label` 等），不新增、删除或重命名被 `static/app_v2.js` 引用的 class 或 DOM 元素。
2. WHERE 需要新增或重命名状态类，THE 侧边栏样式表 SHALL 逐项列出类名、作用域与用途，并同步更新 `static/app_v2.js` 与 `templates/index_v2.html`，使脚本引用与样式选择器完全一致。
3. THE 侧边栏样式表 SHALL NOT 包含任何基于 `[data-role]` 的显隐或过滤规则，角色可见性仅由 `static/app_v2.js` 控制。
4. THE 侧边栏样式表 SHALL 使 `static/app_v2.js` 内容相较重构前保持不变（diff 为空），条款 2 记录的必要同步改动除外。
5. IF 检测到未经说明的结构、class 或状态类改动，THEN THE 侧边栏样式表 SHALL 判定验收失败并给出指明受影响项的失败说明。

### Requirement 10: 健壮性与边界处理

**User Story:** 作为用户，我希望侧边栏在图标缺失、长文案或减少动效偏好下依然稳定，以便在各种环境下正常使用。

#### Acceptance Criteria

1. IF `/static/icons/*.svg` 图标资源未加载，THEN THE 侧边栏样式表 SHALL 保持 20px × 20px 的图标占位、按钮可点击区域不小于 44px × 44px，且文字不发生偏移。
2. IF `.nav-button` 文字过长，THEN THE 侧边栏样式表 SHALL 通过单行省略号截断保持按钮高度不变且面板宽度不变。
3. WHILE 用户系统开启 `prefers-reduced-motion`，THE 侧边栏样式表 SHALL 使侧边栏相关过渡时长不超过 0.01 秒且无可感知动画。
4. THE 侧边栏样式表 SHALL 保留既有 `aria-*`、`title`、`sr-only` 属性对应的样式（`.sr-only` 对辅助技术可读但不可见），且使选中态文字与背景对比度不低于 4.5:1。

### Requirement 11: 缓存刷新

**User Story:** 作为部署者，我希望样式更新后浏览器加载最新样式，以便用户立即看到重构效果。

#### Acceptance Criteria

1. WHEN `static/styles_v2.css` 的侧边栏样式内容被重构更新，THE 样式版本号 SHALL 在 `templates/index_v2.html` 中更新为一个与更新前严格不同的新值，且新值在既有格式（形如 `20260719.4`）下按序递增，使更新后的版本号大于更新前的版本号。
2. WHERE `templates/index_v2.html` 中存在多处对 `static/styles_v2.css` 的引用，THE 样式版本号 SHALL 使全部引用采用同一新版本号值，不得出现新旧版本号混用。
3. IF `static/styles_v2.css` 内容已被重构更新但 `templates/index_v2.html` 中对应的 `?v=` 版本号保持不变，THEN THE 样式版本号 SHALL 判定本次缓存刷新未完成而不通过验收，并保持既有引用的文件名与路径不变。
