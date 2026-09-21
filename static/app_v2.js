const state = {
  user: null,
  csrfToken: "",
  view: "",
  products: [],
  suppliers: [],
  parts: [],
  inventoryBatches: [],
  users: [],
  records: [],
  traceProductId: null,
  traceBatches: [],
  selectedProductId: null,
  scanSession: null,
  passwordRequired: false,
  productDetailId: null,
  supplierDetailId: null,
  supplierDetail: null,
  generationBatchId: null,
  generationBatchPage: 1,
  recordScanTarget: null,
  recordScanReplace: true,
  scanBusy: false,
  dashboard: null,
  batches: [],
  batchDetailId: null,
  batchTrace: null,
  batchQualityRecords: [],
  batchEntryBusy: false,
  purchaseOrders: [],
  productAttributeColumns: null,
  productAttributeDerived: [],
  productImageColumns: ["主图"],
  editingPurchaseOrderId: null,
  lingxingStatus: null,
  inboundReceipts: [],
  productionOrders: [],
  inboundScanRecords: [],
  scanGunOrder: null,
};

const viewMeta = {
  dashboard: ["数据概览", "查看当前产品、二维码与录入状态"],
  products: ["产品管理", "添加产品、配置部件并生成成套二维码"],
  suppliers: ["供应商", "管理供应部件、到货批次与可用库存"],
  users: ["用户管理", "创建管理员、仓管或运营账号，并为仓管分配产品权限"],
  "batch-gen": ["批次生成", "为生产批次生成唯一批次二维码，一码代表整批走步机"],
  "batch-entry": ["现场批次登记", "扫描批次二维码一次性登记整批走步机"],
  "batch-trace": ["扫码查询", "扫描批次二维码查看只读产品信息"],
  "batch-quality": ["批次质量处理", "对整批走步机执行合格放行或暂扣"],
  trace: ["产品查询", "按产品查看生产批次与供应批次族谱"],
  "my-records": ["批次登记记录", "查看整批走步机的登记与质量状态"],
  "purchase-orders": ["采购订单", "创建采购订单、推送领星并查看工厂进度"],
  "inbound-receipts": ["供应收货", "按采购订单登记供应到货"],
  "production-orders": ["生产订单", "从采购订单生成一单一码的生产订单"],
  "scan-gun": ["扫码枪入库", "扫描生产二维码并登记成品库存"],
  "inventory-sync": ["库存同步", "将成品库存单向推送至领星"],
  settings: ["系统设置", "维护通用参数与领星凭据"],
};

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

function escapeHtml(value) {
  return String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#039;");
}

// Renders one icon metric card for the detail-page summary strips.
// `icon` is an optional filename stem under /static/icons (without extension).
function summaryCard(label, value, hint, icon) {
  const iconHtml = icon
    ? `<span class="summary-icon"><img src="/static/icons/${icon}.svg" alt="" aria-hidden="true"></span>`
    : "";
  return `<div class="summary-item">${iconHtml}<div class="summary-item-body"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong><small>${escapeHtml(hint)}</small></div></div>`;
}

// Renders a friendly list empty state: icon, title, description and an optional
// primary action button (pass ready-made button markup as `actionHtml`).
function emptyState(icon, title, hint, actionHtml = "") {
  return `<div class="panel empty-state">
    <span class="empty-state-icon"><img src="/static/icons/${icon}.svg" alt="" aria-hidden="true"></span>
    <h3>${escapeHtml(title)}</h3>
    <p>${escapeHtml(hint)}</p>
    ${actionHtml}
  </div>`;
}

// Quantities: keep integers clean, trim trailing zeros on decimals.
function formatNumber(value) {
  if (value == null || value === "") return "";
  const number = Number(value);
  if (!Number.isFinite(number)) return String(value);
  return Number.isInteger(number) ? String(number) : String(Number(number.toFixed(4)));
}

// Amounts: two decimals with thousands separators.
function formatMoney(value) {
  if (value == null || value === "") return "";
  const number = Number(value);
  if (!Number.isFinite(number)) return String(value);
  return number.toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function formatDate(value) {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return escapeHtml(value);
  const parts = new Intl.DateTimeFormat("zh-CN", {
    year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false,
  }).formatToParts(date).reduce((result, item) => ({ ...result, [item.type]: item.value }), {});
  return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}`;
}

const recordStatusMeta = {
  ASSEMBLED: ["待检", "pending"],
  PASSED: ["合格", "passed"],
  HOLD: ["暂扣", "hold"],
  VOID: ["作废", "void"],
};

function recordStatusBadge(status) {
  const meta = recordStatusMeta[status] || [status || "未知", ""];
  return `<span class="status ${meta[1]}">${meta[0]}</span>`;
}

function auditEventLabel(type) {
  const labels = {
    ASSEMBLY_COMPLETED: "产品录入完成",
    TRACE_RECORD_STATUS_CHANGED: "质量状态更新",
    TRACE_RECORD_CORRECTED: "录入记录校对",
    TRACE_RECORD_DELETED: "录入记录删除",
    PRODUCT_CODE_BATCH_GENERATED: "产品码批次生成",
    PRODUCT_CODES_GENERATED: "产品二维码生成",
    SUPPLIER_BATCH_RECEIVED: "供应批次入库",
    SUPPLIER_BATCH_UPDATED: "供应批次调整",
    SUPPLIER_PART_UPDATED: "供应部件更新",
    PRODUCT_CREATED: "产品创建",
    PRODUCT_UPDATED: "产品更新",
    USER_CREATED: "用户创建",
    USER_UPDATED: "用户更新",
    PO_CREATED: "采购订单创建",
    PO_PUSHED: "采购订单已推送",
    PO_PUSH_FAILED: "采购订单推送失败",
    INBOUND_RECEIVED: "供应收货登记",
    INBOUND_PUSHED: "供应收货已推送",
    INBOUND_PUSH_FAILED: "供应收货推送失败",
    PRODUCTION_ORDER_CREATED: "生产订单创建",
    FINISHED_GOODS_RECEIVED: "成品扫码入库",
    INVENTORY_SYNC_PUSHED: "库存同步成功",
    INVENTORY_SYNC_FAILED: "库存同步失败",
    USER_LOGIN: "账号登录",
    USER_LOGOUT: "退出登录",
    USER_PASSWORD_CHANGED: "密码已修改",
  };
  return labels[type] || "系统操作";
}

function auditEventDescription(event) {
  const objectCode = String(event.objectCode || "").trim();
  const relatedCode = String(event.relatedObjectCode || "").trim();
  const type = event.eventType;
  if (type === "USER_LOGIN") return objectCode ? `登录账号 ${objectCode}` : "账号已登录系统";
  if (type === "USER_LOGOUT") return objectCode ? `账号 ${objectCode} 已退出` : "账号已退出系统";
  if (type === "USER_PASSWORD_CHANGED") return objectCode ? `账号 ${objectCode} 更新登录密码` : "登录密码已更新";
  if (type === "PRODUCT_CODE_BATCH_GENERATED" || type === "PRODUCT_CODES_GENERATED") {
    return objectCode ? `生成产品二维码 ${objectCode}` : "生成产品二维码";
  }
  if (type === "TRACE_RECORD_STATUS_CHANGED") return objectCode ? `更新质量记录 ${objectCode}` : "更新质量记录";
  return [objectCode, relatedCode].filter(Boolean).join(" · ") || "系统数据已更新";
}

function debounce(fn, delay = 240) {
  let timer;
  return (...args) => { clearTimeout(timer); timer = setTimeout(() => fn(...args), delay); };
}

async function api(path, options = {}) {
  const headers = { Accept: "application/json", ...(options.headers || {}) };
  if (options.body !== undefined && !(options.body instanceof FormData)) {
    headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(options.body);
  }
  if (options.method && options.method !== "GET" && state.csrfToken) {
    headers["X-CSRF-Token"] = state.csrfToken;
  }
  let response;
  try {
    response = await fetch(path, { credentials: "same-origin", ...options, headers });
  } catch (networkError) {
    // A dropped connection / offline / DNS failure surfaces as a raw browser
    // error ("Failed to fetch"); replace it with a clear, actionable message.
    const error = new Error("网络连接失败，请检查网络后重试");
    error.status = 0;
    throw error;
  }
  const contentType = response.headers.get("content-type") || "";
  const payload = contentType.includes("application/json") ? await response.json() : null;
  if (!response.ok || payload?.ok === false) {
    const error = new Error(payload?.message || `请求失败（${response.status}）`);
    error.status = response.status;
    if (response.status === 401) showLogin();
    if (response.status === 428 && !state.passwordRequired) showPasswordModal(true);
    throw error;
  }
  return payload?.data;
}

function toast(title, message = "", type = "success") {
  const item = document.createElement("div");
  item.className = `toast ${type}`;
  item.innerHTML = `<strong>${escapeHtml(title)}</strong>${message ? `<span>${escapeHtml(message)}</span>` : ""}`;
  $("#toast-container").append(item);
  setTimeout(() => item.remove(), 4200);
}

function showError(error, title = "操作失败") { toast(title, error?.message || String(error), "error"); }

function isAdmin() { return state.user?.role === "ADMIN"; }
function isWarehouse() { return state.user?.role === "WAREHOUSE"; }
function isOperations() { return state.user?.role === "OPERATIONS"; }

function roleLabel(role) {
  return { ADMIN: "管理员", WAREHOUSE: "仓管", OPERATIONS: "运营" }[role] || "未登录";
}

function allowedViews() {
  if (isAdmin()) return ["dashboard", "products", "suppliers", "users", "batch-gen", "batch-entry", "batch-trace", "batch-quality", "trace", "my-records", "purchase-orders", "inbound-receipts", "production-orders", "scan-gun", "inventory-sync", "settings"];
  if (isWarehouse()) return ["batch-gen", "batch-entry", "inbound-receipts", "production-orders", "scan-gun", "batch-trace", "my-records"];
  if (isOperations()) return ["products", "purchase-orders", "batch-trace", "inventory-sync"];
  return [];
}

// Purchase-order template columns (matches PURCHASE_ORDER_EXPORT_COLUMNS on the
// backend, minus 标识号 which is the system row id). Operations fill these
// directly; the export keeps the exact template column order.
const PURCHASE_ORDER_FIELD_COLUMNS = [
  "采购单号", "供应商", "联系人", "采购方", "联系方式", "结算方式", "预付比例",
  "结算账期", "结算描述", "支付方式", "含税", "费用分配方式", "采购币种", "当前汇率",
  "运费", "运费币种", "其他费用", "其他费用币种", "采购员", "质检类型", "单据备注",
  "颜色", "材质", "内含配件", "包装要求", "特殊要求", "HS海关编码", "交货周期（天数）",
  "采购仓库", "计划编号", "SKU", "店铺", "FNSKU", "是否赠品", "单箱数量", "箱数",
  "实际采购量", "含税单价", "税率", "预计到货时间", "产品备注", "更新报价",
  "内含配件(产品)", "包装要求(产品)", "特殊要求（规避专利）", "HS海关编码(产品)",
  "交货周期（天数）(产品)",
];
const PURCHASE_ORDER_NUMERIC_COLUMNS = new Set([
  "预付比例", "结算账期", "当前汇率", "运费", "其他费用", "单箱数量", "箱数",
  "实际采购量", "含税单价", "税率",
]);
// The columns shown up front; everything else lives under a collapsible group so
// the ~47-column template isn't an overwhelming wall of inputs.
const PURCHASE_ORDER_PRIMARY_COLUMNS = [
  "采购单号", "SKU", "供应商", "联系人", "联系方式", "采购币种",
  "实际采购量", "含税单价", "税率", "含税", "预计到货时间", "采购仓库",
];
// Editable defaults pre-filled to reduce friction (operations can still change them).
const PURCHASE_ORDER_FIELD_DEFAULTS = {
  "采购币种": "CNY",
  "含税": "是",
  "费用分配方式": "按数量",
};
// Controlled inputs: option lists for select fields and date-picker columns, so
// operators pick valid values instead of free-typing.
const PURCHASE_ORDER_YES_NO = ["是", "否"];
const PURCHASE_ORDER_CURRENCIES = ["CNY", "USD", "EUR", "JPY", "GBP", "HKD"];
const PURCHASE_ORDER_FIELD_OPTIONS = {
  "含税": PURCHASE_ORDER_YES_NO,
  "是否赠品": PURCHASE_ORDER_YES_NO,
  "更新报价": PURCHASE_ORDER_YES_NO,
  "采购币种": PURCHASE_ORDER_CURRENCIES,
  "运费币种": PURCHASE_ORDER_CURRENCIES,
  "其他费用币种": PURCHASE_ORDER_CURRENCIES,
  "费用分配方式": ["按数量", "按金额", "按体积", "按重量"],
  "结算方式": ["预付", "月结", "货到付款", "分期", "其他"],
  "支付方式": ["电汇 T/T", "信用证 L/C", "支付宝", "微信", "银行转账", "其他"],
  "质检类型": ["全检", "抽检", "免检"],
};
const PURCHASE_ORDER_DATE_COLUMNS = new Set(["预计到货时间"]);

// Product profile (产品资料). The column list comes from the server so the client
// never hardcodes the template; these constants only drive presentation.
const PRODUCT_ATTRIBUTE_PRIMARY_COLUMNS = [
  "主图",
  "SPU", "款名", "型号", "单位", "品牌", "一级分类", "二级分类", "产品类型",
  "供应商名称", "采购员", "产品负责人", "采购交期", "最小采购量", "币种",
  "含税", "税率", "单价", "含税单价", "采购成本(CNY)", "产品材质",
];
const PRODUCT_ATTRIBUTE_NUMERIC_COLUMNS = new Set([
  "单位加工费", "关联单品成本", "采购成本(CNY)", "单品规格长", "单品规格宽",
  "单品规格高", "单品净重", "单品毛重", "包装规格长", "包装规格宽", "包装规格高",
  "外箱规格长", "外箱规格宽", "外箱规格高", "单箱重量", "单箱数量(pcs)", "税率",
  "最小采购量", "单价", "含税单价", "报关单价", "默认清关单价", "默认清关税率",
  "全部国家头程费用(含税)",
]);
const PRODUCT_ATTRIBUTE_TEXTAREA_COLUMNS = new Set([
  "产品描述", "产品描述(纯文本)", "加工备注", "采购备注", "报价备注",
  "其他申报要素", "配货备注", "默认清关备注",
]);
const PRODUCT_CURRENCIES = ["CNY", "USD", "EUR", "JPY", "GBP", "HKD"];
const PRODUCT_ATTRIBUTE_OPTIONS = {
  "含税": ["是", "否"],
  "币种": PRODUCT_CURRENCIES,
  "报关单价币种": PRODUCT_CURRENCIES,
  "默认清关单价币种": PRODUCT_CURRENCIES,
  "全部国家头程费用币种": PRODUCT_CURRENCIES,
  "默认质检方式": ["全检", "抽检", "免检"],
  "品牌类型": ["自主品牌", "他人品牌", "无品牌"],
  "单品规格单位": ["cm", "mm", "m", "inch"],
  "包装规格单位": ["cm", "mm", "m", "inch"],
  "外箱规格单位": ["cm", "mm", "m", "inch"],
  "单品净重单位": ["g", "kg", "lb"],
  "单品毛重单位": ["g", "kg", "lb"],
  "单箱重量单位": ["g", "kg", "lb"],
};
const PRODUCT_ATTRIBUTE_DATE_COLUMNS = new Set(["交货期", "交期"]);

function setSidebarOpen(open) {
  const shell = $("#app-shell");
  const toggle = $("#sidebar-toggle");
  const overlay = $("#sidebar-overlay");
  const shouldOpen = Boolean(open && shell && !shell.classList.contains("hidden"));
  shell?.classList.toggle("sidebar-open", shouldOpen);
  document.body.classList.toggle("sidebar-open", shouldOpen);
  toggle?.setAttribute("aria-expanded", String(shouldOpen));
  overlay?.classList.toggle("hidden", !shouldOpen);
  overlay?.setAttribute("aria-hidden", String(!shouldOpen));
}

function isNarrowViewport() { return window.matchMedia("(max-width: 860px)").matches; }

// Desktop-only collapse/expand: collapsed (default) shows the icon rail;
// expanded shows the text panel. The choice is remembered across sessions.
function setNavExpanded(expanded) {
  const shell = $("#app-shell");
  const shouldExpand = Boolean(expanded);
  shell?.classList.toggle("nav-expanded", shouldExpand);
  $("#sidebar-toggle")?.setAttribute("aria-expanded", String(shouldExpand));
  try { localStorage.setItem("navExpanded", shouldExpand ? "1" : "0"); } catch (error) { /* storage optional */ }
}

function restoreNavExpanded() {
  let stored = "0";
  try { stored = localStorage.getItem("navExpanded") || "0"; } catch (error) { /* storage optional */ }
  setNavExpanded(stored === "1");
}

// The topbar toggle opens the drawer on narrow screens, and collapses/expands
// the sidebar on desktop.
function toggleSidebar() {
  const shell = $("#app-shell");
  if (isNarrowViewport()) {
    setSidebarOpen(!shell?.classList.contains("sidebar-open"));
  } else {
    setNavExpanded(!shell?.classList.contains("nav-expanded"));
  }
}

function setAccountMenuOpen(open) {
  const popover = $("#account-popover");
  const button = $("#account-button");
  const shouldOpen = Boolean(open && popover);
  popover?.classList.toggle("hidden", !shouldOpen);
  button?.setAttribute("aria-expanded", String(shouldOpen));
}

function setUser(user) {
  state.user = user;
  state.csrfToken = user?.csrfToken || state.csrfToken || "";
  const role = roleLabel(user?.role);
  const shell = $("#app-shell");
  const roleClass = user?.role ? `role-${user.role.toLowerCase()}` : "";
  const displayName = user?.displayName || user?.username || "未登录";
  [document.body, shell].filter(Boolean).forEach((element) => {
    element.classList.toggle("role-admin", roleClass === "role-admin");
    element.classList.toggle("role-warehouse", roleClass === "role-warehouse");
    element.classList.toggle("role-operations", roleClass === "role-operations");
  });
  if (shell) shell.dataset.userRole = user?.role || "";
  $("#sidebar-user-name").textContent = displayName;
  $("#sidebar-user-role").textContent = user ? `${role} · ${user.username || ""}` : role;
  $("#topbar-user-name").textContent = displayName;
  const roleBadge = $("#topbar-user-role");
  if (roleBadge) {
    roleBadge.textContent = role;
    roleBadge.className = `account-role-badge${roleClass ? ` ${roleClass}` : ""}`;
  }
  const avatar = $("#account-avatar");
  if (avatar) avatar.textContent = Array.from(displayName.trim())[0] || "用";
  $$('[data-role]').forEach((item) => {
    const roles = item.dataset.role.split(",").map((roleName) => roleName.trim());
    item.classList.toggle("hidden", !roles.includes(user?.role));
  });
}

function showLogin(message = "") {
  state.user = null;
  state.csrfToken = "";
  state.scanBusy = false;
  setSidebarOpen(false);
  setAccountMenuOpen(false);
  [document.body, $("#app-shell")].filter(Boolean).forEach((element) => element.classList.remove("role-admin", "role-warehouse", "role-operations"));
  $("#app-shell").classList.add("hidden");
  $("#login-screen").classList.remove("hidden");
  if (message) {
    $("#login-error").textContent = message;
    $("#login-error").classList.remove("hidden");
  }
}

function showPasswordModal(required = false) {
  state.passwordRequired = required;
  $("#password-modal").classList.remove("hidden");
  $("#password-close").classList.toggle("hidden", required);
  $("#password-help").textContent = required ? "首次登录必须修改初始密码" : "请输入当前密码和新密码";
  setTimeout(() => $("#password-form").elements.currentPassword.focus(), 30);
}

function closeModal(id) {
  if (id === "password-modal" && state.passwordRequired) return;
  $(`#${id}`)?.classList.add("hidden");
  if (id === "record-edit-modal") state.recordScanTarget = null;
}

function openModal(id) { $(`#${id}`)?.classList.remove("hidden"); }
function anyModalOpen() { return $$(".modal-backdrop").some((item) => !item.classList.contains("hidden")); }

async function submitLogin(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const button = $("button[type='submit']", form);
  button.disabled = true;
  $("#login-error").classList.add("hidden");
  try {
    const user = await api("/api/auth/login", { method: "POST", body: Object.fromEntries(new FormData(form)) });
    setUser(user);
    if (user.mustChangePassword) showPasswordModal(true);
    else await enterApplication();
  } catch (error) {
    $("#login-error").textContent = error.message;
    $("#login-error").classList.remove("hidden");
    form.elements.password.select();
  } finally { button.disabled = false; }
}

async function submitPassword(event) {
  event.preventDefault();
  const form = event.currentTarget;
  try {
    const user = await api("/api/auth/change-password", { method: "POST", body: Object.fromEntries(new FormData(form)) });
    state.passwordRequired = false;
    setUser(user);
    form.reset();
    closeModal("password-modal");
    toast("密码已更新");
    if ($("#app-shell").classList.contains("hidden")) await enterApplication();
  } catch (error) { showError(error, "密码修改失败"); }
}

async function logout() {
  try { await api("/api/auth/logout", { method: "POST", body: {} }); } catch (_) { /* local cleanup */ }
  location.hash = "";
  showLogin();
}

async function enterApplication() {
  $("#login-screen").classList.add("hidden");
  $("#app-shell").classList.remove("hidden");
  restoreNavExpanded();
  await loadProducts();
  const allowed = allowedViews();
  const requested = location.hash.slice(1);
  await switchView(allowed.includes(requested) ? requested : allowed[0]);
}

async function switchView(view) {
  const allowed = allowedViews();
  if (!allowed.includes(view)) view = allowed[0];
  state.view = view;
  $$(".view").forEach((item) => item.classList.toggle("active", item.id === `view-${view}`));
  $$(".nav-button").forEach((item) => {
    const active = item.dataset.view === view && !item.classList.contains("hidden");
    item.classList.toggle("active", active);
    if (active) item.setAttribute("aria-current", "page");
    else item.removeAttribute("aria-current");
  });
  $$(".rail-button").forEach((item) => {
    const active = item.dataset.go === view && !item.classList.contains("hidden");
    item.classList.toggle("active", active);
    if (active) item.setAttribute("aria-current", "page");
    else item.removeAttribute("aria-current");
  });
  $("#page-title").textContent = viewMeta[view][0];
  $("#page-subtitle").textContent = view === "my-records" && isAdmin()
    ? "查看全部整批走步机的登记与质量状态"
    : viewMeta[view][1];
  setSidebarOpen(false);
  setAccountMenuOpen(false);
  history.replaceState(null, "", `#${view}`);
  try {
    if (view === "dashboard") await loadDashboard();
    if (view === "products") showProductList();
    if (view === "suppliers") await loadSuppliers();
    if (view === "users") await loadUsers();
    if (view === "trace") await loadTrace();
    if (view === "batch-gen") await loadBatchGen();
    if (view === "batch-entry") loadBatchEntry();
    if (view === "batch-trace") loadBatchTrace();
    if (view === "batch-quality") await loadBatchQuality();
    if (view === "my-records") await loadMyRecords();
    if (view === "purchase-orders") await loadPurchaseOrders();
    if (view === "inbound-receipts") await loadInboundReceipts();
    if (view === "production-orders") await loadProductionOrders();
    if (view === "scan-gun") loadScanGun();
    if (view === "inventory-sync") loadInventorySync();
    if (view === "settings") await loadSettings();
  } catch (error) { showError(error, "页面加载失败"); }
}

async function loadProducts() {
  state.products = await api("/api/products");
  // The product profile template is fetched once and reused by the form.
  if (!state.productAttributeColumns) {
    try {
      const meta = await api("/api/product-attribute-columns");
      state.productAttributeColumns = meta.columns || [];
      state.productAttributeDerived = meta.derived || [];
      state.productImageColumns = meta.imageColumns || ["主图"];
    } catch (_error) {
      state.productAttributeColumns = [];
      state.productAttributeDerived = [];
    }
  }
  // Only admins build BOMs, so only they need the supplier / part / batch
  // catalogs. Operations register simple products (name + SKU) and cannot read
  // the admin-only inventory batches endpoint.
  if (isAdmin()) {
    [state.suppliers, state.parts, state.inventoryBatches] = await Promise.all([
      api("/api/suppliers"),
      api("/api/part-types"),
      api("/api/supplier-inventory-batches"),
    ]);
  }
}

function componentsByName(product) {
  const grouped = [];
  product.components.forEach((item) => {
    const last = grouped[grouped.length - 1];
    if (last && last.partTypeId === item.partTypeId && last.inventoryBatchId === item.inventoryBatchId) last.quantity += 1;
    else grouped.push({ ...item, quantity: 1 });
  });
  return grouped;
}

// Product card 主图 thumbnail; omitted entirely when no picture was uploaded so
// the card keeps its current layout.
function productThumbHtml(product) {
  const url = (product.attributes || {})["主图"];
  if (!url) return "";
  return `<span class="product-thumb"><img src="${escapeHtml(url)}" alt="" loading="lazy"></span>`;
}

function renderProducts() {
  const container = $("#product-grid");
  const query = ($("#product-search")?.value || "").trim().toLowerCase();
  const filter = $("#product-stock-filter")?.value || "ALL";
  const stockState = (product) => {
    if (product.availableUnits == null || product.availableUnits <= 0) return "OUT";
    const low = product.components.some((item) => item.minimumStock > 0
      && item.inventoryQuantityAvailable > 0
      && item.inventoryQuantityAvailable <= item.minimumStock);
    return low ? "LOW" : "NORMAL";
  };
  const products = state.products.filter((product) => {
    const matchesSearch = !query || `${product.name} ${product.productCode}`.toLowerCase().includes(query);
    if (!matchesSearch) return false;
    if (filter === "ACTIVE") return product.active;
    if (filter === "LOW" || filter === "OUT") return stockState(product) === filter;
    return true;
  });
  if ($("#product-result-count")) $("#product-result-count").textContent = `显示 ${products.length} / ${state.products.length}`;
  const hint = $("#product-list-hint");
  if (hint) hint.textContent = isOperations()
    ? "添加并管理你自己的采购产品，仅你自己可见"
    : "配置供应批次、查看可生成数量和二维码批次";
  if (!state.products.length) {
    const emptyHint = isOperations()
      ? "还没有产品，登记第一个产品后即可生成二维码。"
      : "尚未添加产品，添加后可配置部件、批次与二维码。";
    container.innerHTML = emptyState("package", "暂无产品", emptyHint,
      '<button class="button primary" data-action="open-product-form">添加新产品</button>');
    return;
  }
  if (!products.length) {
    container.innerHTML = emptyState("package", "没有符合条件的产品", "试试调整搜索关键字或筛选条件。");
    return;
  }
  container.innerHTML = products.map((product) => {
    const creatorLine = isAdmin() && product.createdBy
      ? `<div class="product-creator">添加人：${escapeHtml(product.createdBy)}</div>`
      : "";
    // Admins can edit straight from the list; the whole card still opens detail.
    const cardActions = isAdmin()
      ? `<div class="product-card-actions"><button class="button text small" type="button" data-action="edit-product-inline" data-id="${product.id}">编辑</button><span class="product-card-hint">点击卡片查看详情与二维码</span></div>`
      : "";
    // BOM-less products (operations' simple products) show a compact card
    // without the manufacturing inventory / batch footer.
    if (!product.componentCount) {
      return `<div class="product-card" role="button" tabindex="0" data-action="open-product" data-id="${product.id}" aria-label="查看产品 ${escapeHtml(product.name)}">
        <div class="product-card-header">${productThumbHtml(product)}<div><h3>${escapeHtml(product.name)}</h3><div class="product-code">${escapeHtml(product.productCode)}</div></div><div class="status-stack"><span class="status ${product.active ? "active" : ""}">${product.active ? "启用" : "停用"}</span></div></div>
        ${creatorLine}
        <div class="component-summary"><div class="empty-block">采购产品（未配置部件）</div></div>
        ${cardActions}
      </div>`;
    }
    const components = componentsByName(product);
    const available = product.availableUnits == null ? "未绑定" : product.availableUnits;
    const inventoryStatus = stockState(product);
    const inventoryLabel = inventoryStatus === "OUT" ? "不可生成" : inventoryStatus === "LOW" ? "库存紧张" : "库存正常";
    return `<div class="product-card" role="button" tabindex="0" data-action="open-product" data-id="${product.id}" aria-label="查看产品 ${escapeHtml(product.name)} 详情">
      <div class="product-card-header">${productThumbHtml(product)}<div><h3>${escapeHtml(product.name)}</h3><div class="product-code">${escapeHtml(product.productCode)}</div></div><div class="status-stack"><span class="status ${product.active ? "active" : ""}">${product.active ? "启用" : "停用"}</span><span class="status stock-${inventoryStatus.toLowerCase()}">${inventoryLabel}</span></div></div>
      ${creatorLine}
      <div class="component-summary">${components.slice(0, 3).map((item) => `<div class="component-summary-row"><span>${escapeHtml(item.partName || item.slotName.replace(/ \d+$/, ""))}${item.quantity > 1 ? ` × ${item.quantity}` : ""}</span><small>${escapeHtml(item.supplierName)}<br>${escapeHtml(item.inventoryBatchNo || "未绑定批次")}</small></div>`).join("") || '<div class="empty-block">未配置部件</div>'}</div>
      <div class="product-card-footer"><span>${product.generationBatchCount} 个生成批次 · ${product.generatedCount} 套</span><div class="stock-number">${escapeHtml(available)}<small>${product.availableUnits == null ? "" : "套可生成"}</small></div></div>
      ${cardActions}
    </div>`;
  }).join("");
}

function showProductList() {
  state.productDetailId = null;
  state.generationBatchId = null;
  $("#product-list-step").classList.remove("hidden");
  $("#product-detail").classList.add("hidden");
  $("#product-batch-detail").classList.add("hidden");
  renderProducts();
}

function scrollContentToTop(target) {
  const content = target?.closest(".content") || $(".content");
  if (content) content.scrollTop = 0;
  if (document.scrollingElement) document.scrollingElement.scrollTop = 0;
  requestAnimationFrame(() => {
    if (content) content.scrollTop = 0;
    if (document.scrollingElement) document.scrollingElement.scrollTop = 0;
  });
}

async function openProductDetail(productId) {
  const product = state.products.find((item) => item.id === productId);
  if (!product) return;
  // Operations don't have the manufacturing detail (components / QR batches are
  // admin-only endpoints); tapping a card opens the simple edit form instead.
  if (isOperations()) {
    resetProductForm(product);
    openModal("product-modal");
    setTimeout(() => $("#product-form").elements.name.focus(), 30);
    return;
  }
  state.productDetailId = productId;
  state.generationBatchId = null;
  $("#product-list-step").classList.add("hidden");
  $("#product-batch-detail").classList.add("hidden");
  $("#product-detail").classList.remove("hidden");
  scrollContentToTop($("#product-detail"));
  $("#product-detail-name").textContent = product.name;
  $("#product-detail-code").textContent = product.productCode;
  $("#download-product-codes").href = `/api/products/${product.id}/qrcodes.zip`;
  const components = componentsByName(product);
  $("#product-summary-strip").innerHTML = [
    ["产品部件", product.componentCount, "每套需要扫描的实物部件", "package"],
    ["生成批次", product.generationBatchCount, "按每次生成任务归档", "clipboard-list"],
    ["已生成套数", product.generatedCount, "产品主码总数", "scan"],
    ["当前可生成", product.availableUnits == null ? "未绑定" : product.availableUnits, "由最少可用部件批次决定", "building-factory-2"],
  ].map((item) => summaryCard(item[0], item[1], item[2], item[3])).join("");
  renderProductAttributeView(product);
  $("#product-component-table").innerHTML = components.map((item) => `<tr><td><span class="cell-main">${escapeHtml(item.partName || item.slotName)}</span><span class="cell-sub">${escapeHtml(item.specification || item.partCode || "")}</span></td><td>${escapeHtml(item.supplierName)}</td><td><span class="cell-main">${escapeHtml(item.inventoryBatchNo || "未绑定")}</span><span class="cell-sub">${item.inventoryProductionDate || "-"}</span></td><td>${item.quantity}</td><td><span class="inventory-chip ${Number(item.inventoryQuantityAvailable || 0) > 0 ? "available" : "empty"}">${item.inventoryBatchId ? Number(item.inventoryQuantityAvailable || 0) : "需编辑"}</span></td></tr>`).join("") || '<tr><td colspan="5" class="empty-row">未配置部件</td></tr>';
  const batches = await api(`/api/product-code-batches?productModelId=${productId}`);
  $("#product-batch-table").innerHTML = batches.map((batch) => `<tr><td><span class="cell-main">${escapeHtml(batch.batchCode)}</span><span class="cell-sub">${escapeHtml(batch.prefix)}</span></td><td>${String(batch.startSequence).padStart(4, "0")}–${String(batch.endSequence).padStart(4, "0")}</td><td>${batch.quantity}</td><td>${escapeHtml(batch.generatedBy || "-")}</td><td>${formatDate(batch.generatedAt)}</td><td><div class="table-actions"><a class="button secondary small" href="${batch.downloadUrl}">下载</a><button class="button primary small" data-action="open-product-batch" data-id="${batch.id}">查看二维码</button></div></td></tr>`).join("") || '<tr><td colspan="6" class="empty-row">尚未生成二维码批次</td></tr>';
}

async function openProductBatch(batchId, page = 1) {
  const data = await api(`/api/product-code-batches/${batchId}?page=${page}&pageSize=40`);
  state.generationBatchId = batchId;
  state.generationBatchPage = data.pagination.page;
  $("#product-detail").classList.add("hidden");
  $("#product-list-step").classList.add("hidden");
  $("#product-batch-detail").classList.remove("hidden");
  scrollContentToTop($("#product-batch-detail"));
  $("#batch-detail-code").textContent = data.batch.batchCode;
  $("#batch-detail-meta").textContent = `${data.batch.productName} · ${data.batch.quantity} 套 · ${formatDate(data.batch.generatedAt)}`;
  $("#download-batch-codes").href = data.batch.downloadUrl;
  $("#batch-pagination-summary").textContent = `第 ${data.pagination.page} / ${data.pagination.totalPages} 页，共 ${data.pagination.total} 套`;
  $("#batch-prev").disabled = data.pagination.page <= 1;
  $("#batch-next").disabled = data.pagination.page >= data.pagination.totalPages;
  renderGeneratedCodeSets(data.sets, "#batch-code-sets");
}

function openProductEditFromList(productId) {
  const product = state.products.find((item) => item.id === productId);
  if (!product) return;
  resetProductForm(product);
  openModal("product-modal");
  setTimeout(() => $("#product-form").elements.name.focus(), 30);
}

function isProductImageColumn(column) {
  return (state.productImageColumns || ["主图"]).includes(column);
}

// 主图 is a picture: the hidden `.product-attr` input keeps the stored URL (so it
// submits like any other column) while the file input uploads and previews it.
function productImageFieldHtml(column) {
  const label = escapeHtml(column);
  const key = escapeHtml(column);
  return `<div class="field wide image-field" data-image-column="${key}">
    <span class="image-field-label">${label}</span>
    <input class="product-attr" data-column="${key}" type="hidden">
    <div class="image-field-body">
      <div class="image-preview" data-image-preview><span class="image-preview-empty">未上传</span></div>
      <div class="image-field-actions">
        <input class="image-input" type="file" accept="image/png,image/jpeg,image/gif,image/webp" data-image-input aria-label="${label}">
        <button class="button text small" type="button" data-image-clear hidden>移除图片</button>
        <span class="field-help">支持 PNG / JPG / GIF / WEBP，最大 5 MB</span>
      </div>
    </div>
  </div>`;
}

function productAttributeFieldHtml(column) {
  const label = escapeHtml(column);
  const key = escapeHtml(column);
  if (isProductImageColumn(column)) return productImageFieldHtml(column);
  const options = PRODUCT_ATTRIBUTE_OPTIONS[column];
  if (options) {
    const opts = ['<option value="">（不选）</option>']
      .concat(options.map((opt) => `<option value="${escapeHtml(opt)}">${escapeHtml(opt)}</option>`))
      .join("");
    return `<label class="field">${label}<select class="product-attr" data-column="${key}">${opts}</select></label>`;
  }
  if (PRODUCT_ATTRIBUTE_TEXTAREA_COLUMNS.has(column)) {
    return `<label class="field wide">${label}<textarea class="product-attr" data-column="${key}" rows="2" maxlength="500"></textarea></label>`;
  }
  if (PRODUCT_ATTRIBUTE_DATE_COLUMNS.has(column)) {
    return `<label class="field">${label}<input class="product-attr" data-column="${key}" type="date"></label>`;
  }
  const numeric = PRODUCT_ATTRIBUTE_NUMERIC_COLUMNS.has(column);
  return `<label class="field">${label}<input class="product-attr" data-column="${key}" ${numeric ? 'type="number" step="any"' : 'type="text" maxlength="500"'}></label>`;
}

// Builds the 产品资料 inputs once: the columns operators fill most often stay
// visible, the long tail (报关 / 清关 / 物流) collapses into a details block.
// System-managed columns (SKU / 品名 / 创建人 / 时间 / 状态) are never inputs.
function renderProductAttributeFields() {
  const container = $("#product-attribute-fields");
  if (!container || container.dataset.rendered) return;
  const columns = state.productAttributeColumns || [];
  const derived = new Set(state.productAttributeDerived || []);
  const editable = columns.filter((column) => !derived.has(column));
  if (!editable.length) return;
  const primary = PRODUCT_ATTRIBUTE_PRIMARY_COLUMNS.filter((column) => editable.includes(column));
  const more = editable.filter((column) => !primary.includes(column));
  container.innerHTML = `
    <div class="po-fields-grid">${primary.map(productAttributeFieldHtml).join("")}</div>
    ${more.length ? `<details class="po-more"><summary>更多产品资料（选填，共 ${more.length} 项）</summary>
      <div class="po-fields-grid">${more.map(productAttributeFieldHtml).join("")}</div>
    </details>` : ""}`;
  container.dataset.rendered = "1";
  $$("[data-image-column]", container).forEach(bindProductImageField);
}

// Shows the current picture (or the empty placeholder) for one image field.
function renderProductImagePreview(field) {
  const value = $(".product-attr", field)?.value || "";
  const preview = $("[data-image-preview]", field);
  const clear = $("[data-image-clear]", field);
  if (preview) {
    preview.innerHTML = value
      ? `<img src="${escapeHtml(value)}" alt="${escapeHtml(field.dataset.imageColumn || "主图")}">`
      : '<span class="image-preview-empty">未上传</span>';
  }
  if (clear) clear.hidden = !value;
}

// Uploads the chosen file immediately and stores the returned URL, so saving the
// product only persists a reference.
function bindProductImageField(field) {
  const hidden = $(".product-attr", field);
  const input = $("[data-image-input]", field);
  const clear = $("[data-image-clear]", field);
  if (!hidden || !input) return;
  input.addEventListener("change", async () => {
    const file = input.files && input.files[0];
    if (!file) return;
    if (file.size > 5 * 1024 * 1024) {
      showError(new Error("图片不能超过 5 MB"), "图片上传失败");
      input.value = "";
      return;
    }
    const body = new FormData();
    body.append("file", file);
    input.disabled = true;
    try {
      const uploaded = await api("/api/product-images", { method: "POST", body });
      hidden.value = uploaded.url;
      renderProductImagePreview(field);
      toast("图片已上传", "保存产品后生效");
    } catch (error) {
      showError(error, "图片上传失败");
    } finally {
      input.disabled = false;
      input.value = "";
    }
  });
  clear?.addEventListener("click", () => {
    hidden.value = "";
    input.value = "";
    renderProductImagePreview(field);
  });
}

// The read-only columns the system maintains, shown so operators know they are
// filled automatically rather than missing.
function renderProductAttributeAuto(product) {
  const target = $("#product-attribute-auto");
  if (!target) return;
  const attributes = product?.attributes || {};
  const rows = (state.productAttributeDerived || []).map((column) => [column, attributes[column]]);
  target.innerHTML = rows.length
    ? rows.map(([column, value]) => `<span class="auto-field"><small>${escapeHtml(column)}</small><b>${escapeHtml(value || (product ? "-" : "保存后自动填写"))}</b></span>`).join("")
    : "";
}

// Product detail: list only the profile columns that actually carry a value, in
// template order, so the panel stays readable instead of showing ~98 blanks.
function renderProductAttributeView(product) {
  const target = $("#product-attribute-view");
  if (!target) return;
  const attributes = product?.attributes || {};
  const columns = state.productAttributeColumns && state.productAttributeColumns.length
    ? state.productAttributeColumns
    : Object.keys(attributes);
  const filled = columns
    .filter((column) => attributes[column] != null && String(attributes[column]).trim() !== "")
    .map((column) => [column, String(attributes[column])]);
  target.innerHTML = filled.length
    ? `<div class="order-summary-grid">${filled.map(([column, value]) => {
        // Picture columns render the image itself rather than its URL.
        const body = isProductImageColumn(column)
          ? `<a class="attr-image" href="${escapeHtml(value)}" target="_blank" rel="noopener"><img src="${escapeHtml(value)}" alt="${escapeHtml(column)}"></a>`
          : `<strong>${escapeHtml(value)}</strong>`;
        return `<div class="order-summary-item"><span>${escapeHtml(column)}</span>${body}</div>`;
      }).join("")}</div>`
    : '<div class="empty-block compact-empty">尚未填写产品资料，点击“编辑”补充</div>';
}

function resetProductForm(product = null) {
  const form = $("#product-form");
  form.reset();
  renderProductAttributeFields();
  renderProductAttributeAuto(product);
  // Load stored values into the profile inputs (blank for a new product).
  const attributes = product?.attributes || {};
  $$(".product-attr", form).forEach((input) => {
    const value = attributes[input.dataset.column];
    input.value = value == null ? "" : String(value);
  });
  // Reset the file inputs and mirror the stored picture into each preview.
  $$("[data-image-column]", form).forEach((field) => {
    const input = $("[data-image-input]", field);
    if (input) input.value = "";
    renderProductImagePreview(field);
  });
  form.elements.productId.value = product?.id || "";
  form.elements.name.value = product?.name || "";
  const operations = isOperations();
  const componentsSection = $("#product-components-section");
  const skuField = $("#product-sku-field");
  // Operations register simple products: hide the BOM editor, show the optional
  // SKU field (locked once created since the code can't be changed on edit).
  if (componentsSection) componentsSection.classList.toggle("hidden", operations);
  if (skuField) skuField.classList.toggle("hidden", !operations);
  if (form.elements.modelCode) {
    form.elements.modelCode.value = product?.productCode || "";
    form.elements.modelCode.readOnly = Boolean(product);
  }
  $("#component-editor").innerHTML = "";
  if (!operations) {
    const components = product ? componentsByName(product) : [{}];
    components.forEach((component) => addComponentRow(component));
  }
  $("#product-modal-title").textContent = product ? "编辑产品" : "添加新产品";
}

function addComponentRow(data = {}) {
  const row = document.createElement("div");
  row.className = "component-row";
  row.innerHTML = `<label>供应商<select name="supplierId" required><option value="">请选择</option>${state.suppliers.filter((item) => item.active).map((supplier) => `<option value="${supplier.id}" ${Number(data.supplierId) === supplier.id ? "selected" : ""}>${escapeHtml(supplier.name)}</option>`).join("")}</select></label>
    <label>部件<select name="partTypeId" required></select></label>
    <label>供应批次<select name="inventoryBatchId" required></select></label>
    <label>每套数量<input name="quantity" type="number" min="1" max="20" value="${Number(data.quantity || 1)}" required></label>
    <button class="remove-component" type="button">删除此行</button>`;
  const supplierSelect = $("[name='supplierId']", row);
  const partSelect = $("[name='partTypeId']", row);
  const batchSelect = $("[name='inventoryBatchId']", row);
  const fillBatches = (selectedBatchId = null) => {
    const partId = Number(partSelect.value || 0);
    const batches = state.inventoryBatches.filter((item) => item.partTypeId === partId && (item.active || item.id === Number(selectedBatchId)));
    batchSelect.innerHTML = '<option value="">请选择批次</option>' + batches.map((batch) => `<option value="${batch.id}" ${batch.id === Number(selectedBatchId) ? "selected" : ""} ${batch.quantityAvailable <= 0 && batch.id !== Number(selectedBatchId) ? "disabled" : ""}>${escapeHtml(batch.batchNo)} · 可用 ${batch.quantityAvailable}</option>`).join("");
  };
  const fillParts = (selectedPartId = null, selectedBatchId = null) => {
    const supplierId = Number(supplierSelect.value || 0);
    const parts = state.parts.filter((item) => item.supplierId === supplierId && (item.active || item.id === Number(selectedPartId)));
    partSelect.innerHTML = '<option value="">请选择部件</option>' + parts.map((part) => `<option value="${part.id}" ${part.id === Number(selectedPartId) ? "selected" : ""}>${escapeHtml(part.name)}${part.specification ? ` · ${escapeHtml(part.specification)}` : ""}</option>`).join("");
    fillBatches(selectedBatchId);
  };
  supplierSelect.addEventListener("change", () => fillParts());
  partSelect.addEventListener("change", () => fillBatches());
  fillParts(data.partTypeId, data.inventoryBatchId);
  $(".remove-component", row).addEventListener("click", () => {
    // A product may have no components at all (complete, indivisible item), so
    // every row can be removed. With zero rows the product is saved as a
    // component-less finished good.
    row.remove();
  });
  $("#component-editor").append(row);
}

async function submitProduct(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const productId = Number(form.elements.productId.value || 0);
  const operations = isOperations();
  // Extended product profile; blanks are omitted so a partial save keeps
  // whatever was stored before.
  const attributes = {};
  $$(".product-attr", form).forEach((input) => {
    const value = input.value.trim();
    if (value !== "") attributes[input.dataset.column] = value;
  });
  let body;
  if (operations) {
    body = { name: form.elements.name.value.trim(), attributes };
    const sku = form.elements.modelCode ? form.elements.modelCode.value.trim() : "";
    if (!productId && sku) body.modelCode = sku;
  } else {
    body = {
      name: form.elements.name.value.trim(),
      attributes,
      components: $$(".component-row", form).map((row) => ({
        inventoryBatchId: Number($("[name='inventoryBatchId']", row).value),
        quantity: Number($("[name='quantity']", row).value),
      })),
    };
  }
  try {
    const saved = await api(productId ? `/api/products/${productId}` : "/api/products", { method: productId ? "PUT" : "POST", body });
    closeModal("product-modal");
    await loadProducts();
    if (!operations && productId) await openProductDetail(saved.id);
    else renderProducts();
    toast(
      productId ? "产品已更新" : "产品已添加",
      operations ? "已保存，可在采购订单中选择该产品" : "供应批次已绑定，可按库存生成产品二维码",
    );
  } catch (error) { showError(error, "产品保存失败"); }
}

function todayCode() {
  const now = new Date();
  return `${now.getFullYear()}${String(now.getMonth() + 1).padStart(2, "0")}${String(now.getDate()).padStart(2, "0")}`;
}

function openCodeModal(productId) {
  const product = state.products.find((item) => item.id === productId);
  if (!product) return;
  const form = $("#code-form");
  form.reset();
  form.elements.productModelId.value = product.id;
  form.elements.quantity.value = 1;
  $("#code-product-name").textContent = `${product.name} · ${product.componentCount} 个部件`;
  $("#generated-code-sets").innerHTML = "";
  updateCodePreview();
  openModal("code-modal");
  setTimeout(() => form.elements.prefix.focus(), 30);
}

function updateCodePreview() {
  const prefix = $("#code-form").elements.prefix.value.trim().toUpperCase() || "前缀";
  $("#code-preview").textContent = `${prefix}-${todayCode()}-0001`;
}

async function submitCodeGeneration(event) {
  event.preventDefault();
  const form = event.currentTarget;
  try {
    const sets = await api(`/api/products/${Number(form.elements.productModelId.value)}/code-sets`, {
      method: "POST",
      body: { prefix: form.elements.prefix.value, quantity: Number(form.elements.quantity.value) },
    });
    closeModal("code-modal");
    await loadProducts();
    if (sets[0]?.generationBatchId) await openProductBatch(sets[0].generationBatchId, 1);
    toast("二维码已生成", `共生成 ${sets.length} 套，每套主码和部件码一一对应`);
  } catch (error) { showError(error, "二维码生成失败"); }
}

function renderGeneratedCodeSets(sets, target = "#generated-code-sets") {
  $(target).innerHTML = sets.map((set) => `<article class="code-set-card" data-code-set="${set.id}">
    <div class="code-set-header"><div><strong>${escapeHtml(set.setCode)}</strong><span>1 个产品主码 · ${set.parts.length} 个专属部件码</span></div><div><button class="button secondary small" data-action="print-code-set" data-id="${set.id}">打印</button> <a class="button secondary small" href="${set.downloadUrl}">下载全部</a></div></div>
    <div class="qr-grid"><div class="qr-item main"><img src="${set.machine.qrUrl}" alt="产品主码"><strong>产品主码</strong><span>${escapeHtml(set.productName)}</span><code>${escapeHtml(set.machine.identificationCode)}</code></div>
    ${set.parts.map((part) => `<div class="qr-item"><img src="${part.qrUrl}" alt="${escapeHtml(part.partName)}二维码"><strong>${part.position}. ${escapeHtml(part.partName)}</strong><span>${escapeHtml(part.supplierName)}</span><code>${escapeHtml(part.identificationCode)}</code></div>`).join("")}</div>
  </article>`).join("");
}

function printCodeSet(id) {
  const card = $(`[data-code-set="${id}"]`);
  if (!card) return;
  card.classList.add("printing");
  window.print();
  card.classList.remove("printing");
}

async function loadSuppliers() {
  await loadProducts();
  showSupplierList();
}

function showSupplierList() {
  state.supplierDetailId = null;
  state.supplierDetail = null;
  $("#supplier-detail").removeAttribute("aria-busy");
  [$("#edit-supplier"), $("#add-supplier-part"), $("#add-supplier-batch")].forEach((button) => { if (button) button.disabled = false; });
  $("#supplier-list-step").classList.remove("hidden");
  $("#supplier-detail").classList.add("hidden");
  const query = ($("#supplier-search")?.value || "").trim().toLowerCase();
  const filter = $("#supplier-stock-filter")?.value || "ALL";
  const suppliers = state.suppliers.filter((supplier) => {
    const matchesSearch = !query || `${supplier.name} ${supplier.supplierCode} ${supplier.contact || ""}`.toLowerCase().includes(query);
    if (!matchesSearch) return false;
    if (filter === "AVAILABLE") return supplier.quantityAvailable > 0;
    if (filter === "OUT") return supplier.quantityAvailable <= 0;
    return true;
  });
  if ($("#supplier-result-count")) $("#supplier-result-count").textContent = `显示 ${suppliers.length} / ${state.suppliers.length}`;
  $("#supplier-grid").innerHTML = suppliers.map((supplier) => `<button class="supplier-card" type="button" data-action="open-supplier" data-id="${supplier.id}" aria-label="查看供应商 ${escapeHtml(supplier.name)} 详情">
    <div class="product-card-header"><div><h3>${escapeHtml(supplier.name)}</h3><div class="product-code">${escapeHtml(supplier.supplierCode)}</div></div><span class="status ${supplier.active ? "active" : ""}">${supplier.active ? "启用" : "停用"}</span></div>
    <div class="supplier-card-meta"><div><span>供应部件</span><strong>${supplier.partCount}</strong></div><div><span>到货批次</span><strong>${supplier.batchCount}</strong></div><div><span>可用库存</span><strong>${supplier.quantityAvailable}</strong></div></div>
    <div class="product-card-footer"><span>${escapeHtml(supplier.contact || "未填写联系人")}${supplier.phone ? ` · ${escapeHtml(supplier.phone)}` : ""}</span><strong>查看详情</strong></div>
  </button>`).join("") || (state.suppliers.length
    ? emptyState("users", "没有符合条件的供应商", "试试调整搜索关键字或筛选条件。")
    : emptyState("users", "暂无供应商", "添加供应商后即可登记部件、批次与到货数量。",
        '<button class="button primary" data-action="open-supplier-form">添加供应商</button>'));
}

async function openSupplierDetail(supplierId) {
  const supplier = state.suppliers.find((item) => item.id === supplierId);
  state.supplierDetailId = supplierId;
  state.supplierDetail = null;
  $("#supplier-list-step").classList.add("hidden");
  $("#supplier-detail").classList.remove("hidden");
  $("#supplier-detail").setAttribute("aria-busy", "true");
  $("#supplier-detail-name").textContent = supplier?.name || "供应商详情";
  $("#supplier-detail-code").textContent = "正在加载部件、批次与关联产品…";
  $("#supplier-summary-strip").innerHTML = '<div class="detail-loading" role="status">正在加载供应商详情</div>';
  $("#supplier-product-usage-table").innerHTML = '<tr><td colspan="5" class="empty-row compact-empty">正在加载关联产品…</td></tr>';
  $("#supplier-part-table").innerHTML = '<tr><td colspan="6" class="empty-row compact-empty">正在加载供应部件…</td></tr>';
  $("#supplier-batch-table").innerHTML = '<tr><td colspan="7" class="empty-row compact-empty">正在加载到货批次…</td></tr>';
  [$("#edit-supplier"), $("#add-supplier-part"), $("#add-supplier-batch")].forEach((button) => { button.disabled = true; });
  window.scrollTo({ top: 0, behavior: "smooth" });
  let detail;
  try {
    detail = await api(`/api/suppliers/${supplierId}`);
  } catch (error) {
    showSupplierList();
    throw error;
  }
  if (state.supplierDetailId !== supplierId) return;
  state.supplierDetail = detail;
  $("#supplier-detail").removeAttribute("aria-busy");
  [$("#edit-supplier"), $("#add-supplier-part"), $("#add-supplier-batch")].forEach((button) => { button.disabled = false; });
  $("#supplier-detail-name").textContent = detail.supplier.name;
  $("#supplier-detail-code").textContent = `${detail.supplier.supplierCode}${detail.supplier.contact ? ` · ${detail.supplier.contact}` : ""}${detail.supplier.phone ? ` · ${detail.supplier.phone}` : ""}`;
  $("#supplier-summary-strip").innerHTML = [
    ["供应部件", detail.supplier.partCount, "该供应商维护的部件型号", "package"],
    ["到货批次", detail.supplier.batchCount, "所有部件的供应批次", "clipboard-list"],
    ["可用库存", detail.supplier.quantityAvailable, "生成产品码时自动扣减", "layout-dashboard"],
    ["供应商状态", detail.supplier.active ? "启用" : "停用", "停用后不可用于新产品", "user-circle"],
  ].map((item) => summaryCard(item[0], item[1], item[2], item[3])).join("");
  $("#supplier-product-usage-table").innerHTML = (detail.usedInProducts || []).map((product) => `<tr><td><span class="cell-main">${escapeHtml(product.name)}</span><span class="cell-sub code-text">${escapeHtml(product.productCode)}</span></td><td>${product.parts.map((part) => `<span class="cell-sub">${escapeHtml(part.name)} × ${part.requiredQuantity}</span>`).join("")}</td><td>${product.requiredQuantity}</td><td>${product.generatedCount}</td><td><div class="table-actions"><button class="button secondary small" data-action="open-related-product" data-id="${product.id}">查看产品</button></div></td></tr>`).join("") || '<tr><td colspan="5" class="empty-row">当前没有产品使用该供应商的部件</td></tr>';
  $("#supplier-part-table").innerHTML = detail.parts.map((part) => `<tr><td><span class="cell-main">${escapeHtml(part.name)}</span>${part.stockStatus !== "NORMAL" ? `<span class="cell-sub ${part.stockStatus === "OUT" ? "danger-text" : "warning-text"}">${part.stockStatus === "OUT" ? "库存已用尽" : "低于安全库存"}</span>` : ""}</td><td class="code-text">${escapeHtml(part.partCode)}</td><td>${escapeHtml(part.specification || "-")}</td><td>${part.batchCount}</td><td><span class="inventory-chip ${part.stockStatus === "OUT" ? "empty" : part.stockStatus === "LOW" ? "low" : "available"}">${part.quantityAvailable}</span><span class="cell-sub">安全库存 ${part.minimumStock}</span></td><td><div class="table-actions"><button class="button secondary small" data-action="edit-part" data-id="${part.id}">编辑</button><button class="button primary small" data-action="add-part-batch" data-id="${part.id}">登记批次</button></div></td></tr>`).join("") || '<tr><td colspan="6" class="empty-row">尚未添加供应部件</td></tr>';
  $("#supplier-batch-table").innerHTML = detail.batches.map((batch) => `<tr><td><span class="cell-main">${escapeHtml(batch.partName)}</span><span class="cell-sub code-text">${escapeHtml(batch.batchNo)}</span></td><td>${batch.productionDate || "-"}</td><td>${batch.receivedDate || "-"}</td><td>${batch.quantityReceived}</td><td>${batch.quantityConsumed}</td><td><span class="inventory-chip ${batch.quantityAvailable > 0 ? "available" : "empty"}">${batch.quantityAvailable}</span></td><td><div class="table-actions"><button class="button secondary small" data-action="open-inventory-movements" data-id="${batch.id}">查看流水</button><button class="button secondary small" data-action="edit-inventory-batch" data-id="${batch.id}">编辑批次</button></div></td></tr>`).join("") || '<tr><td colspan="7" class="empty-row">尚未登记到货批次</td></tr>';
}

function openSupplierModal(supplierId = null) {
  const form = $("#supplier-form");
  form.reset();
  const supplier = state.suppliers.find((item) => item.id === supplierId) || state.supplierDetail?.supplier;
  form.elements.supplierId.value = supplierId || "";
  form.elements.supplierCode.value = supplierId ? supplier?.supplierCode || "" : "";
  form.elements.name.value = supplierId ? supplier?.name || "" : "";
  form.elements.contact.value = supplierId ? supplier?.contact || "" : "";
  form.elements.phone.value = supplierId ? supplier?.phone || "" : "";
  $("#supplier-modal-title").textContent = supplierId ? "编辑供应商" : "添加供应商";
  openModal("supplier-modal");
  setTimeout(() => form.elements.supplierCode.focus(), 30);
}

async function submitSupplier(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const supplierId = Number(form.elements.supplierId.value || 0);
  try {
    const saved = await api(supplierId ? `/api/suppliers/${supplierId}` : "/api/suppliers", {
      method: supplierId ? "PUT" : "POST",
      body: { supplierCode: form.elements.supplierCode.value, name: form.elements.name.value, contact: form.elements.contact.value, phone: form.elements.phone.value },
    });
    closeModal("supplier-modal");
    await loadProducts();
    if (supplierId) await openSupplierDetail(saved.id);
    else showSupplierList();
    toast(supplierId ? "供应商已更新" : "供应商已添加");
  } catch (error) { showError(error, "供应商保存失败"); }
}

function openPartModal(partId = null) {
  if (!state.supplierDetailId) return;
  const form = $("#part-form");
  form.reset();
  const part = state.supplierDetail?.parts.find((item) => item.id === partId);
  form.elements.partTypeId.value = part?.id || "";
  form.elements.supplierId.value = state.supplierDetailId;
  form.elements.name.value = part?.name || "";
  form.elements.partCode.value = part?.partCode || "";
  form.elements.specification.value = part?.specification || "";
  form.elements.minimumStock.value = part?.minimumStock ?? 0;
  $("#part-modal-title").textContent = part ? "编辑供应部件" : "添加供应部件";
  $("#part-modal-help").textContent = state.supplierDetail?.supplier.name || "";
  openModal("part-modal");
  setTimeout(() => form.elements.name.focus(), 30);
}

async function submitPart(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const partId = Number(form.elements.partTypeId.value || 0);
  const supplierId = Number(form.elements.supplierId.value);
  try {
    await api(partId ? `/api/part-types/${partId}` : "/api/part-types", {
      method: partId ? "PUT" : "POST",
      body: { partCode: form.elements.partCode.value, name: form.elements.name.value, specification: form.elements.specification.value, minimumStock: Number(form.elements.minimumStock.value), supplierId },
    });
    closeModal("part-modal");
    await loadProducts();
    await openSupplierDetail(supplierId);
    toast(partId ? "供应部件已更新" : "供应部件已添加");
  } catch (error) { showError(error, "部件保存失败"); }
}

function todayInputDate() {
  const now = new Date();
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}`;
}

function openInventoryBatchModal(batchId = null, preferredPartId = null) {
  if (!state.supplierDetailId) return;
  if (!state.supplierDetail?.parts.length) {
    toast("请先添加供应部件", "登记批次前需要先维护部件型号", "error");
    return;
  }
  const form = $("#inventory-batch-form");
  form.reset();
  const batch = state.supplierDetail?.batches.find((item) => item.id === batchId);
  form.elements.inventoryBatchId.value = batch?.id || "";
  form.elements.partTypeId.innerHTML = state.supplierDetail.parts.filter((item) => item.active || item.id === batch?.partTypeId).map((part) => `<option value="${part.id}">${escapeHtml(part.name)}${part.specification ? ` · ${escapeHtml(part.specification)}` : ""}</option>`).join("");
  form.elements.partTypeId.value = String(batch?.partTypeId || preferredPartId || state.supplierDetail.parts[0]?.id || "");
  form.elements.partTypeId.disabled = Boolean(batch);
  form.elements.batchNo.value = batch?.batchNo || "";
  form.elements.quantity.value = batch?.quantityReceived || "";
  form.elements.productionDate.value = batch?.productionDate || "";
  form.elements.receivedDate.value = batch?.receivedDate || todayInputDate();
  form.elements.remarks.value = batch?.remarks || "";
  $("#inventory-batch-title").textContent = batch ? "编辑到货批次" : "登记到货批次";
  $("#inventory-batch-help").textContent = batch ? `已领用 ${batch.quantityConsumed}，到货数量不可低于该值` : "记录供应商部件的批次、数量和日期";
  openModal("inventory-batch-modal");
  setTimeout(() => form.elements.batchNo.focus(), 30);
}

async function submitInventoryBatch(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const batchId = Number(form.elements.inventoryBatchId.value || 0);
  const existing = state.supplierDetail?.batches.find((item) => item.id === batchId);
  try {
    await api(batchId ? `/api/supplier-inventory-batches/${batchId}` : "/api/supplier-inventory-batches", {
      method: batchId ? "PUT" : "POST",
      body: { partTypeId: existing?.partTypeId || Number(form.elements.partTypeId.value), batchNo: form.elements.batchNo.value, quantity: Number(form.elements.quantity.value), productionDate: form.elements.productionDate.value, receivedDate: form.elements.receivedDate.value, remarks: form.elements.remarks.value },
    });
    closeModal("inventory-batch-modal");
    const supplierId = state.supplierDetailId;
    await loadProducts();
    await openSupplierDetail(supplierId);
    toast(batchId ? "供应批次已更新" : "到货批次已登记");
  } catch (error) { showError(error, "批次保存失败"); }
}

async function openInventoryMovements(batchId) {
  const batch = state.supplierDetail?.batches.find((item) => item.id === batchId);
  if (!batch) return;
  try {
    const movements = await api(`/api/supplier-inventory-batches/${batchId}/movements`);
    $("#inventory-movement-title").textContent = `${batch.partName} · 库存流水`;
    $("#inventory-movement-subtitle").textContent = `${state.supplierDetail.supplier.name} · 批次 ${batch.batchNo}`;
    $("#inventory-movement-summary").innerHTML = [
      ["累计入库", batch.quantityReceived],
      ["累计领用", batch.quantityConsumed],
      ["当前结存", batch.quantityAvailable],
      ["流水笔数", movements.length],
    ].map((item) => `<div><span>${item[0]}</span><strong>${item[1]}</strong></div>`).join("");
    const movementLabels = { RECEIPT: "到货入库", ISSUE: "生产领用", ADJUSTMENT: "库存调整" };
    $("#inventory-movement-table").innerHTML = movements.map((movement) => `<tr><td>${formatDate(movement.occurredAt)}</td><td><span class="movement-type ${movement.movementType.toLowerCase()}">${movementLabels[movement.movementType] || movement.movementType}</span><span class="cell-sub">${escapeHtml(movement.reason || "-")}</span></td><td><strong class="quantity-change ${movement.quantityChange < 0 ? "negative" : "positive"}">${movement.quantityChange > 0 ? "+" : ""}${movement.quantityChange}</strong></td><td>${movement.balanceAfter}</td><td>${escapeHtml(movement.productName || "-")}<span class="cell-sub code-text">${escapeHtml(movement.productCodeBatch || "")}</span></td><td>${escapeHtml(movement.actorName || "系统")}</td></tr>`).join("") || '<tr><td colspan="6" class="empty-row">暂无库存流水</td></tr>';
    openModal("inventory-movement-modal");
  } catch (error) { showError(error, "库存流水加载失败"); }
}

async function loadUsers() {
  state.users = await api("/api/users");
  renderUsers();
}

function renderUsers() {
  $("#user-table").innerHTML = state.users.map((user) => {
    const isCurrent = user.id === state.user?.id;
    // Permission scope by role. Warehouse operates every product (no per-product
    // assignment), so it shows a fixed 仓储作业 scope rather than a product list
    // or a misleading "未分配产品".
    const permissions = user.role === "ADMIN"
      ? '<span class="cell-main">全系统权限</span><span class="cell-sub">系统配置与全业务访问</span>'
      : user.role === "OPERATIONS"
        ? '<span class="cell-main">运营业务</span><span class="cell-sub">采购、领星同步与工厂进度</span>'
        : '<span class="cell-main">仓储作业</span><span class="cell-sub">全部产品入库、批次与生产订单</span>';
    return `<tr><td><span class="cell-main">${escapeHtml(user.displayName)}</span>${isCurrent ? '<span class="cell-sub">当前登录账号</span>' : ""}</td><td>${escapeHtml(user.username)}</td><td><span class="status ${user.role === "ADMIN" ? "role-admin" : ""}">${roleLabel(user.role)}</span></td><td>${permissions}</td><td><span class="status ${user.active ? "active" : ""}">${user.active ? "启用" : "停用"}</span></td><td><div class="table-actions"><button class="button secondary small" data-action="edit-user" data-id="${user.id}">编辑</button><button class="button text small ${user.active ? "danger-text" : ""}" data-action="toggle-user" data-id="${user.id}" ${isCurrent ? "disabled" : ""}>${isCurrent ? "当前账号" : user.active ? "停用" : "启用"}</button></div></td></tr>`;
  }).join("") || '<tr><td colspan="6" class="empty-row">尚未添加账号</td></tr>';
}

function openUserModal(userId = null) {
  const form = $("#user-form");
  form.reset();
  const user = state.users.find((item) => item.id === userId);
  form.elements.userId.value = user?.id || "";
  form.elements.displayName.value = user?.displayName || "";
  form.elements.username.value = user?.username || "";
  form.elements.username.disabled = Boolean(user);
  form.elements.role.value = user?.role || "WAREHOUSE";
  form.elements.role.disabled = user?.id === state.user?.id;
  form.elements.password.required = !user;
  $("#user-password-label").textContent = user ? "重置密码（可不填）" : "初始密码";
  $("#user-modal-title").textContent = user ? "编辑账号" : "添加账号";
  openModal("user-modal");
  setTimeout(() => form.elements.displayName.focus(), 30);
}

async function submitUser(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const userId = Number(form.elements.userId.value || 0);
  const body = {
    displayName: form.elements.displayName.value,
    username: form.elements.username.value,
    password: form.elements.password.value,
    role: form.elements.role.value,
  };
  if (userId && !body.password) delete body.password;
  try {
    await api(userId ? `/api/users/${userId}` : "/api/users", { method: userId ? "PUT" : "POST", body });
    closeModal("user-modal");
    await loadUsers();
    toast(userId ? "账号已更新" : "账号已添加");
  } catch (error) { showError(error, "账号保存失败"); }
}

async function toggleUser(userId) {
  const user = state.users.find((item) => item.id === userId);
  if (!user) return;
  try {
    await api(`/api/users/${userId}`, { method: "PUT", body: { active: !user.active } });
    await loadUsers();
    toast("账号状态已更新");
  } catch (error) { showError(error, "账号状态更新失败"); }
}

function qualityCheckbox(recordId, scope) {
  return `<input class="quality-record-checkbox" type="checkbox" value="${recordId}" data-quality-scope="${scope}" aria-label="选择质量记录 ${recordId}">`;
}

function selectedQualityIds(scope) {
  return $$(`.quality-record-checkbox[data-quality-scope="${scope}"]:checked`).map((item) => Number(item.value));
}

function updateQualitySelection(scope) {
  const ids = selectedQualityIds(scope);
  const prefix = scope === "dashboard" ? "dashboard" : "trace";
  const total = $$(`.quality-record-checkbox[data-quality-scope="${scope}"]`).length;
  const selectAll = $(`#${prefix}-quality-select-all`);
  const selectedLabel = $(`#${prefix}-quality-selected`);
  const bulkButton = $(`#${prefix}-quality-bulk-button`);
  if (selectAll) {
    selectAll.checked = total > 0 && ids.length === total;
    selectAll.indeterminate = ids.length > 0 && ids.length < total;
  }
  if (selectedLabel) selectedLabel.textContent = `已选择 ${ids.length} 条`;
  if (bulkButton) bulkButton.disabled = ids.length === 0;
}

function setQualitySelection(scope, checked) {
  $$(`.quality-record-checkbox[data-quality-scope="${scope}"]`).forEach((item) => { item.checked = checked; });
  updateQualitySelection(scope);
}

function formatDay(value) {
  if (!value) return "日期未记录";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value).slice(0, 10);
  return new Intl.DateTimeFormat("zh-CN", { year: "numeric", month: "2-digit", day: "2-digit", weekday: "short" }).format(date);
}

function generationBatchLabel(record) {
  const batch = record.generationBatch;
  return batch ? batch.batchCode : "历史录入 / 无生成批次";
}

// The board reflects the current batch flow (采购单 → 生产订单/溯源码 → 批次登记 →
// 质量放行 → 成品入库 → 库存同步) rather than the retired per-unit records, so the
// numbers and the 待办 list match what the team actually works on.
async function loadDashboard() {
  const [dashboard, batches] = await Promise.all([api("/api/dashboard"), api("/api/production-batches")]);
  state.dashboard = dashboard;
  const counts = dashboard.counts;
  const passRate = counts.registeredBatches
    ? `${((counts.batchPassed / counts.registeredBatches) * 100).toFixed(1)}%`
    : "--";
  const stockAlertCount = counts.lowStockParts + counts.outOfStockParts;
  const metrics = [
    ["今日生成批次", counts.todayBatches, "今日生成的生产批次", "neutral"],
    ["批次合格率", passRate, `已登记 ${counts.registeredBatches} 批`, "success"],
    ["待处理暂扣", counts.batchHold, "暂扣批次，不可入库", counts.batchHold ? "danger" : "neutral"],
    ["待检批次", counts.batchAssembled, "等待质量放行", counts.batchAssembled ? "warning" : "neutral"],
    ["成品库存", counts.finishedGoodsOnHand, `${counts.pendingStockIn} 个生产订单待入库`, "neutral"],
    ["库存预警（项）", stockAlertCount, `${counts.outOfStockParts} 项缺货`, counts.outOfStockParts ? "danger" : counts.lowStockParts ? "warning" : "neutral"],
  ];
  $("#dashboard-metrics").innerHTML = metrics.map((item) => `<article class="metric ${item[3]}"><span>${item[0]}</span><strong>${item[1]}</strong><small>${item[2]}</small></article>`).join("");
  // 待办 follows the flow order so the backlog reads as "which step is stuck".
  const tasks = [];
  if (counts.batchHold) tasks.push({ icon: "route.svg", tone: "danger", title: "暂扣批次待处理", detail: "暂扣批次不可入库，需复核后闭环", source: "批次质量处理", count: counts.batchHold, urgency: "高", view: "batch-quality" });
  if (counts.outOfStockParts || counts.lowStockParts) tasks.push({ icon: "building-factory-2.svg", tone: counts.outOfStockParts ? "danger" : "warning", title: "库存预警", detail: counts.outOfStockParts ? "存在缺货部件，请及时补充库存" : "部件库存低于安全库存", source: "供应商库存", count: stockAlertCount, urgency: counts.outOfStockParts ? "高" : "中", view: "suppliers" });
  if (counts.pendingProductionOrders) tasks.push({ icon: "clipboard-list.svg", tone: "info", title: "待生成生产订单", detail: "运营已下采购单，等待生成溯源码（第 1 步）", source: "生产订单", count: counts.pendingProductionOrders, urgency: "中", view: "production-orders" });
  if (counts.unregisteredBatches) tasks.push({ icon: "scan.svg", tone: "info", title: "待批次登记", detail: "已生成溯源码但尚未扫码登记（第 2 步）", source: "现场批次登记", count: counts.unregisteredBatches, urgency: "中", view: "batch-entry" });
  if (counts.batchAssembled) tasks.push({ icon: "clipboard-list.svg", tone: "warning", title: "待质量放行", detail: "已登记批次等待合格放行（第 3 步）", source: "批次质量处理", count: counts.batchAssembled, urgency: "中", view: "batch-quality" });
  if (counts.pendingStockIn) tasks.push({ icon: "scan.svg", tone: "info", title: "待成品入库", detail: "生产订单未达计划入库量（第 4 步）", source: "扫码枪入库", count: counts.pendingStockIn, urgency: "中", view: "scan-gun" });
  if (counts.inventorySyncPending) tasks.push({ icon: "route.svg", tone: "info", title: "待库存同步", detail: "成品库存尚未同步至领星", source: "库存同步", count: counts.inventorySyncPending, urgency: "低", view: "inventory-sync" });
  $("#dashboard-task-list").innerHTML = tasks.slice(0, 5).map((task) => `<tr><td><span class="task-type ${task.tone}"><img src="/static/icons/${task.icon}" alt=""></span></td><td><span class="cell-main">${escapeHtml(task.title)}</span><span class="cell-sub">${escapeHtml(task.detail)}</span></td><td>${escapeHtml(task.source)}</td><td>${task.count}</td><td><span class="task-urgency ${task.tone}">${task.urgency}</span></td><td class="right"><button class="button secondary small" data-action="go-view" data-view="${task.view}">去处理</button></td></tr>`).join("") || '<tr><td colspan="6" class="empty-row compact-empty">今日暂无待办任务</td></tr>';
  if ($("#dashboard-refreshed-at")) $("#dashboard-refreshed-at").textContent = `数据截至：${formatDate(new Date())}`;
  // Batch registrations awaiting quality; 处理 opens the same modal as 批次质量处理.
  $("#dashboard-quality-queue").innerHTML = (dashboard.batchQualityQueue || []).map((record) => `<tr><td><span class="cell-main code-text">${escapeHtml(record.batchCode || "-")}</span><span class="cell-sub">${escapeHtml(record.productName || "-")}</span></td><td>${escapeHtml(record.registeredQuantity ?? "-")}</td><td>${recordStatusBadge(record.qualityStatus || "ASSEMBLED")}</td><td>${escapeHtml(record.operatorName || "-")}</td><td><div class="table-actions">${(record.qualityStatus || "ASSEMBLED") === "ASSEMBLED" ? `<button class="button secondary small" data-action="open-batch-quality" data-id="${record.id}">处理</button>` : '<span class="cell-sub">已暂扣</span>'}</div></td></tr>`).join("") || '<tr><td colspan="5" class="empty-row compact-empty">当前没有待处理批次</td></tr>';
  $("#dashboard-stock-alerts").innerHTML = dashboard.stockAlerts.map((part) => `<button class="alert-item" data-action="open-supplier" data-id="${part.supplierId}"><span class="alert-severity ${part.stockStatus.toLowerCase()}">${part.stockStatus === "OUT" ? "缺货" : "低库存"}</span><div><strong>${escapeHtml(part.name)}</strong><span>${escapeHtml(part.supplierName)} · ${escapeHtml(part.partCode)}</span></div><div class="alert-balance"><b>${part.quantityAvailable}</b><small>安全库存 ${part.minimumStock}</small></div></button>`).join("") || '<div class="empty-block compact-empty">所有启用部件库存正常</div>';
  $("#dashboard-activity").innerHTML = dashboard.recentActivity.map((event) => `<div class="activity-item"><span class="activity-marker"></span><div class="activity-copy"><div class="activity-line"><time>${formatDate(event.occurredAt).slice(11)}</time><span>${escapeHtml(event.actorDisplayName || event.operatorName || event.actorUsername || "系统")}</span></div><p>${escapeHtml(auditEventDescription(event))}</p></div><button class="button secondary small" data-action="show-activity-detail" data-event-label="${escapeHtml(auditEventLabel(event.eventType))}" data-event-description="${escapeHtml(auditEventDescription(event))}">查看</button></div>`).join("") || '<div class="empty-block compact-empty">暂无操作记录</div>';
  $("#dashboard-code-sets").innerHTML = (batches || []).slice(0, 8).map((batch) => `<tr><td><span class="cell-main">${escapeHtml(batch.productName || "-")}</span><span class="cell-sub">${escapeHtml(batch.productModelCode || "")}</span></td><td class="code-text">${escapeHtml(batch.batchCode || "-")}</td><td>${escapeHtml(batch.plannedQuantity ?? "-")}</td><td>${formatDate(batch.generatedAt)}</td></tr>`).join("") || '<tr><td colspan="4" class="empty-row">尚未生成生产批次</td></tr>';
}

// 产品查询 now browses the current batch data model (生产批次) instead of the
// retired per-unit trace_records, so it stays consistent with 批次生成 /
// 批次登记 and no longer shows an empty legacy list.
async function loadTrace() {
  state.batches = await api("/api/production-batches");
  renderTraceProducts();
}

function traceBatchCount(productId) {
  return (state.batches || []).filter((batch) => batch.productModelId === productId).length;
}

function renderTraceProducts() {
  state.traceProductId = null;
  $("#trace-product-step").classList.remove("hidden");
  $("#trace-detail").classList.add("hidden");
  $("#trace-product-grid").innerHTML = state.products.map((product) => `<button class="trace-product-card" data-action="open-trace-product" data-id="${product.id}"><h3>${escapeHtml(product.name)}</h3><p>${product.componentCount} 个部件 · ${traceBatchCount(product.id)} 个生产批次</p><strong>查看生产批次</strong></button>`).join("") || '<div class="panel empty-block">尚未添加产品</div>';
}

function openTraceProduct(productId) {
  const product = state.products.find((item) => item.id === productId);
  if (!product) return;
  state.traceProductId = productId;
  $("#trace-product-step").classList.add("hidden");
  $("#trace-detail").classList.remove("hidden");
  $("#trace-product-name").textContent = product.name;
  state.traceBatches = (state.batches || []).filter((batch) => batch.productModelId === productId);
  $("#trace-date-from").value = "";
  $("#trace-date-to").value = "";
  $("#trace-status-filter").value = "";
  $("#trace-record-search").value = "";
  renderTraceBatches();
}

function renderTraceBatches() {
  if (!state.traceProductId) return;
  const search = ($("#trace-record-search").value || "").trim().toLowerCase();
  const status = $("#trace-status-filter").value;
  const dateFrom = $("#trace-date-from").value;
  const dateTo = $("#trace-date-to").value;
  if (dateFrom && dateTo && dateFrom > dateTo) {
    toast("筛选日期无效", "开始日期不能晚于结束日期", "error");
    return;
  }
  const batches = (state.traceBatches || []).filter((batch) => {
    if (search && !`${batch.batchCode || ""} ${batch.productName || ""} ${batch.prefix || ""}`.toLowerCase().includes(search)) return false;
    if (status && (batch.qualityStatus || "") !== status) return false;
    const day = (batch.generatedAt || "").slice(0, 10);
    if (dateFrom && day && day < dateFrom) return false;
    if (dateTo && day && day > dateTo) return false;
    return true;
  });
  const groups = new Map();
  batches.forEach((batch) => {
    const key = batch.generatedAt ? batch.generatedAt.slice(0, 10) : "";
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(batch);
  });
  $("#trace-record-table").innerHTML = Array.from(groups.entries()).map(([day, items]) => `
    <tr class="date-group-row"><td colspan="7"><strong>${escapeHtml(formatDay(day))}</strong><span>${items.length} 个批次</span></td></tr>
    ${items.map((batch) => {
      const registered = batchRegistered(batch);
      const statusLabel = registered ? recordStatusBadge(batch.qualityStatus || "ASSEMBLED") : '<span class="status">未登记</span>';
      const downloadUrl = batch.downloadUrl || `/api/production-batches/${batch.id}/qr`;
      return `<tr><td><span class="cell-main code-text">${escapeHtml(batch.batchCode || "-")}</span></td><td><span class="cell-main">${escapeHtml(batch.productName || "-")}</span><span class="cell-sub code-text">${escapeHtml(batch.productModelCode || "")}</span></td><td>${escapeHtml(batch.plannedQuantity ?? "-")}</td><td>${registered ? escapeHtml(batch.registeredQuantity ?? "-") : "未登记"}</td><td>${statusLabel}</td><td>${formatDate(batch.generatedAt)}</td><td><div class="table-actions"><a class="button secondary small" href="${downloadUrl}" target="_blank" rel="noopener">下载二维码</a><button class="button secondary small" data-action="open-batch-genealogy" data-id="${batch.id}">查看族谱</button></div></td></tr>`;
    }).join("")}
  `).join("") || '<tr><td colspan="7" class="empty-row">当前筛选条件下没有生产批次</td></tr>';
  const summaryParts = [`${batches.length} 个生产批次`];
  if (dateFrom || dateTo) summaryParts.push(`${dateFrom || "最早"} 至 ${dateTo || "今天"}`);
  if (status) summaryParts.push(recordStatusMeta[status]?.[0] || status);
  $("#trace-filter-summary").textContent = summaryParts.join(" · ");
}

// Batch-level 族谱: the batch summary plus the supplier batches it consumed
// (reverse trace), replacing the retired per-unit machine/part genealogy.
async function openBatchGenealogy(batchId) {
  try {
    const data = await api(`/api/production-batches/${batchId}`);
    const registration = data.registration || {};
    const registered = Boolean(registration.registered);
    const consumption = data.reverseTrace || [];
    const qrUrl = data.downloadUrl || `/api/production-batches/${batchId}/qr`;
    $("#genealogy-subtitle").textContent = "整批走步机的供应批次族谱";
    $("#genealogy-content").innerHTML = `<div class="genealogy-overview">
      <div class="genealogy-main-qr"><img src="${escapeHtml(qrUrl)}" alt="批次二维码"><div><span>批次码</span><strong>${escapeHtml(data.identificationCode || data.batchCode || "-")}</strong><small>扫码可查看整批只读信息</small></div></div>
      <div class="genealogy-summary-grid">
        <div class="genealogy-summary"><span>产品</span><strong>${escapeHtml(data.productName || "-")}</strong></div>
        <div class="genealogy-summary"><span>计划台数</span><strong>${escapeHtml(data.plannedQuantity ?? "-")}</strong></div>
        <div class="genealogy-summary"><span>登记台数</span><strong>${registered ? escapeHtml(registration.registeredQuantity ?? "-") : "未登记"}</strong></div>
        <div class="genealogy-summary"><span>质量状态</span><strong>${registered ? recordStatusBadge(registration.qualityStatus || "ASSEMBLED") : "未登记"}</strong></div>
      </div>
    </div><div class="genealogy-section-heading"><div><span>供应来源</span><strong>${consumption.length} 条供应批次消耗</strong></div><small>本批次消耗的供应批次、供应部件与数量</small></div><div class="genealogy-parts">${consumption.map((item, index) => `<div class="genealogy-part"><div class="genealogy-part-index">${index + 1}</div><div class="genealogy-part-copy"><strong>${escapeHtml(item.partName || item.partCode || "-")}</strong><span>消耗 ${escapeHtml(item.quantityConsumed ?? "-")}</span><code>${escapeHtml(item.supplierBatchNo || item.supplierInventoryBatchId || "未关联供应批次")}</code></div></div>`).join("") || '<div class="empty-block">暂无供应批次消耗记录</div>'}</div>`;
    openModal("genealogy-modal");
  } catch (error) { showError(error, "族谱查询失败"); }
}

function findQualityRecord(recordId) {
  return state.records.find((item) => item.id === recordId)
    || state.dashboard?.qualityQueue?.find((item) => item.id === recordId);
}

function updateQualityNote() {
  const form = $("#record-quality-form");
  const status = form.elements.status.value;
  const note = $("#record-quality-note");
  note.className = `quality-status-note ${status.toLowerCase()}`;
  note.textContent = status === "HOLD"
    ? "暂扣会进入管理看板的待处理队列，必须填写原因。"
    : status === "PASSED"
      ? "合格后记录进入已放行状态，完整保留录入和处理审计。"
      : "退回待检后，记录会重新进入待处理队列。";
}

function openRecordQualitySelection(recordIds) {
  const records = recordIds.map(findQualityRecord).filter(Boolean);
  if (!records.length) return;
  const record = records[0];
  const form = $("#record-quality-form");
  form.reset();
  form.elements.recordId.value = record.id;
  form.elements.recordIds.value = records.map((item) => item.id).join(",");
  form.elements.status.value = records.every((item) => item.status === records[0].status) && record.status !== "ASSEMBLED" ? record.status : "PASSED";
  form.elements.reason.value = records.length === 1 ? record.statusReason || "" : "";
  $("#record-quality-title").textContent = records.length > 1 ? "批量处理质量状态" : "处理质量状态";
  $("#record-quality-machine").textContent = records.length > 1
    ? `已选择 ${records.length} 条记录；处理结果将同时应用`
    : `${record.machine.productModelName || record.machine.model} · ${record.machine.sn}`;
  updateQualityNote();
  openModal("record-quality-modal");
  setTimeout(() => form.elements.status.focus(), 30);
}

function openRecordQuality(recordId) {
  openRecordQualitySelection([recordId]);
}

function openBulkQuality(scope) {
  openRecordQualitySelection(selectedQualityIds(scope));
}

async function submitRecordQuality(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const recordIds = form.elements.recordIds.value.split(",").map(Number).filter(Boolean);
  const body = { status: form.elements.status.value, reason: form.elements.reason.value };
  try {
    if (recordIds.length > 1) {
      await api("/api/records/status/bulk", { method: "PUT", body: { ...body, recordIds } });
    } else {
      await api(`/api/records/${recordIds[0] || Number(form.elements.recordId.value)}/status`, { method: "PUT", body });
    }
    closeModal("record-quality-modal");
    if (state.view === "dashboard") await loadDashboard();
    toast(recordIds.length > 1 ? `已批量更新 ${recordIds.length} 条质量记录` : "质量状态已更新");
  } catch (error) { showError(error, "质量状态更新失败"); }
}

function resetTraceFilters() {
  $("#trace-date-from").value = "";
  $("#trace-date-to").value = "";
  $("#trace-status-filter").value = "";
  $("#trace-record-search").value = "";
  renderTraceBatches();
}

// The 产品族谱 lookup resolves a scanned/typed batch code (raw or PTS:B:… payload)
// to a production batch, then shows its supplier-batch genealogy.
async function submitGenealogy(event) {
  event.preventDefault();
  const raw = String(new FormData(event.currentTarget).get("code") || "").trim();
  const code = raw.replace(/^PTS:B:/i, "").trim();
  const batch = (state.batches || []).find((item) => (item.batchCode || "").toLowerCase() === code.toLowerCase());
  if (!batch) {
    toast("未找到该批次", "请检查批次码或扫描批次二维码", "error");
    return;
  }
  await openBatchGenealogy(batch.id);
}

async function openGenealogy(code) {
  try {
    const data = await api(`/api/genealogy?code=${encodeURIComponent(code)}`);
    const record = data.records?.[0];
    const machine = data.machine || record?.machine;
    const parts = record?.parts || (data.partLabel ? [data.partLabel] : []);
    const machineQrUrl = machine?.qrUrl || (machine?.id ? `/api/machines/${machine.id}/qr` : "");
    $("#genealogy-subtitle").textContent = data.queryType === "MACHINE" ? "从产品主码向下查看全部部件" : "从部件码向上查看所属产品";
    $("#genealogy-content").innerHTML = `<div class="genealogy-overview">
      <div class="genealogy-main-qr">${machineQrUrl ? `<img src="${escapeHtml(machineQrUrl)}" alt="产品主码二维码">` : ""}<div><span>产品主码</span><strong>${escapeHtml(machine?.identificationCode || "尚未录入")}</strong><small>扫码可再次进入该产品完整族谱</small></div></div>
      <div class="genealogy-summary-grid">
        <div class="genealogy-summary"><span>产品</span><strong>${escapeHtml(machine?.productModelName || machine?.model || "-")}</strong></div>
        <div class="genealogy-summary"><span>生成批次</span><strong>${escapeHtml(generationBatchLabel(record || {}))}</strong></div>
        <div class="genealogy-summary"><span>完成日期</span><strong>${record ? escapeHtml(formatDay(record.completedAt)) : "未完成"}</strong></div>
        <div class="genealogy-summary"><span>质量状态</span><strong>${record ? recordStatusBadge(record.status) : "未完成"}</strong></div>
      </div>
    </div><div class="genealogy-section-heading"><div><span>组成部件</span><strong>${parts.length} 个已绑定部件</strong></div><small>每个部件的编码与二维码一一对应</small></div><div class="genealogy-parts">${parts.map((part, index) => {
      const qrUrl = part.qrUrl || (part.id ? `/api/part-labels/${part.id}/qr` : "");
      return `<div class="genealogy-part"><div class="genealogy-part-index">${part.position || index + 1}</div>${qrUrl ? `<img src="${escapeHtml(qrUrl)}" alt="${escapeHtml(part.partName)}二维码">` : ""}<div class="genealogy-part-copy"><strong>${escapeHtml(part.partName)}</strong><span>${escapeHtml(part.supplierName || "")}</span><code>${escapeHtml(part.identificationCode)}</code><small>${escapeHtml(part.batchCode || part.supplierBatchNo || "未关联部件批次")}</small></div></div>`;
    }).join("") || '<div class="empty-block">暂无部件记录</div>'}</div>`;
    openModal("genealogy-modal");
  } catch (error) { showError(error, "族谱查询失败"); }
}

function getStationId() {
  let id = localStorage.getItem("pts_station_id_v2");
  if (!id) {
    id = `station-${crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(16).slice(2)}`}`;
    localStorage.setItem("pts_station_id_v2", id);
  }
  return id;
}

function stationBody(extra = {}) {
  return { stationId: getStationId(), stationName: "自动录入终端", operatorName: state.user?.displayName || "", ...extra };
}

// ---- 批次生成（管理员或仓管） ----

function batchRegistered(batch) {
  return Boolean(batch.registered ?? batch.registeredQuantity ?? batch.entry);
}

async function loadBatchGen() {
  showBatchGenList();
  state.batches = await api("/api/production-batches");
  renderBatchGenList();
}

function showBatchGenList() {
  state.batchDetailId = null;
  $("#batch-gen-list-step").classList.remove("hidden");
  $("#batch-gen-detail").classList.add("hidden");
}

function renderBatchGenList() {
  const query = ($("#batch-gen-search")?.value || "").trim().toLowerCase();
  const batches = (state.batches || []).filter((batch) => {
    if (!query) return true;
    return `${batch.batchCode || ""} ${batch.productName || ""} ${batch.prefix || ""}`.toLowerCase().includes(query);
  });
  if ($("#batch-gen-result-count")) $("#batch-gen-result-count").textContent = `显示 ${batches.length} / ${(state.batches || []).length}`;
  $("#batch-gen-table").innerHTML = batches.map((batch) => {
    const registered = batchRegistered(batch);
    const statusLabel = registered ? recordStatusBadge(batch.qualityStatus || "ASSEMBLED") : '<span class="status">未登记</span>';
    const downloadUrl = batch.downloadUrl || `/api/production-batches/${batch.id}/qr`;
    return `<tr><td><span class="cell-main code-text">${escapeHtml(batch.batchCode)}</span></td><td>${escapeHtml(batch.productName || "-")}</td><td>${escapeHtml(batch.plannedQuantity ?? "-")}</td><td>${escapeHtml(batch.prefix || "-")}</td><td>${statusLabel}</td><td>${escapeHtml(batch.generatedBy || "-")}</td><td>${formatDate(batch.generatedAt)}</td><td><div class="table-actions"><a class="button secondary small" href="${downloadUrl}" target="_blank" rel="noopener">下载二维码</a><button class="button primary small" data-action="open-batch-gen-detail" data-id="${batch.id}">查看详情</button></div></td></tr>`;
  }).join("") || '<tr><td colspan="8" class="empty-row">尚未生成生产批次</td></tr>';
}

function openBatchGenerateModal() {
  const form = $("#batch-generate-form");
  form.reset();
  const products = (state.products || []).filter((item) => item.active);
  form.elements.productModelId.innerHTML = '<option value="">请选择产品</option>' + products.map((product) => `<option value="${product.id}">${escapeHtml(product.name)}</option>`).join("");
  form.elements.quantity.value = 1;
  openModal("batch-generate-modal");
  setTimeout(() => form.elements.productModelId.focus(), 30);
}

async function submitBatchGenerate(event) {
  event.preventDefault();
  const form = event.currentTarget;
  try {
    await api("/api/production-batches", {
      method: "POST",
      body: {
        productModelId: Number(form.elements.productModelId.value),
        prefix: form.elements.prefix.value,
        quantity: Number(form.elements.quantity.value),
      },
    });
    closeModal("batch-generate-modal");
    await loadBatchGen();
    toast("生产批次已生成", "已生成唯一批次二维码，并按整批用量扣减库存");
  } catch (error) { showError(error, "批次生成失败"); }
}

async function openBatchGenDetail(batchId) {
  const data = await api(`/api/production-batches/${batchId}`);
  // The detail response is the flat production_batch dict; registration status
  // lives under `registration` and the reverse trace under `reverseTrace`.
  const registration = data.registration || {};
  const registered = Boolean(registration.registered);
  const consumption = data.reverseTrace || [];
  state.batchDetailId = batchId;
  $("#batch-gen-list-step").classList.add("hidden");
  $("#batch-gen-detail").classList.remove("hidden");
  scrollContentToTop($("#batch-gen-detail"));
  $("#batch-gen-detail-code").textContent = data.batchCode || "";
  $("#batch-gen-detail-meta").textContent = `${data.productName || "-"} · 计划 ${data.plannedQuantity ?? "-"} 台 · ${formatDate(data.generatedAt)}`;
  $("#batch-gen-download-qr").href = data.downloadUrl || `/api/production-batches/${batchId}/qr`;
  $("#batch-gen-summary-strip").innerHTML = [
    ["批次码", data.batchCode || "-", "整批唯一二维码码值", "scan"],
    ["计划台数", data.plannedQuantity ?? "-", "本批走步机计划数量", "clipboard-list"],
    ["登记台数", registered ? (registration.registeredQuantity ?? "-") : "未登记", "扫码登记的实际台数", "building-factory-2"],
    ["质量状态", registered ? (recordStatusMeta[registration.qualityStatus]?.[0] || registration.qualityStatus || "-") : "未登记", "批次级质量状态", "layout-dashboard"],
  ].map((item) => summaryCard(item[0], item[1], item[2], item[3])).join("");
  $("#batch-gen-qr-preview").innerHTML = `<img src="${data.downloadUrl || `/api/production-batches/${batchId}/qr`}" alt="批次二维码" class="batch-qr-image">`;
  $("#batch-gen-consumption-table").innerHTML = consumption.map((item) => `<tr><td class="code-text">${escapeHtml(item.supplierBatchNo || item.supplierInventoryBatchId || "-")}</td><td>${escapeHtml(item.partTypeName || item.partName || "-")}</td><td class="right">${escapeHtml(item.quantityConsumed ?? "-")}</td></tr>`).join("") || '<tr><td colspan="3" class="empty-row">暂无供应批次消耗记录</td></tr>';
}

// ---- 现场批次登记（仓管） ----

function loadBatchEntry() {
  $("#batch-entry-code").value = "";
  $("#batch-entry-quantity").value = "";
  setBatchEntryFeedback("neutral", "尚未开始", "请扫描批次二维码");
  focusBatchEntry();
}

function setBatchEntryFeedback(type, title, message) {
  const feedback = $("#batch-entry-feedback");
  if (!feedback) return;
  feedback.className = `scan-feedback ${type}`;
  feedback.innerHTML = `<strong>${escapeHtml(title)}</strong><span>${escapeHtml(message)}</span>`;
}

async function submitBatchEntry(event) {
  event.preventDefault();
  if (state.batchEntryBusy) return;
  const input = $("#batch-entry-code");
  const code = input.value.trim();
  if (!code) { focusBatchEntry(); return; }
  const quantityRaw = $("#batch-entry-quantity").value.trim();
  const body = stationBody({ code });
  if (quantityRaw) body.quantity = Number(quantityRaw);
  const submit = $("button[type='submit']", event.currentTarget);
  state.batchEntryBusy = true;
  submit.disabled = true;
  try {
    const result = await api("/api/batch-entry/scan", { method: "POST", body });
    input.value = "";
    $("#batch-entry-quantity").value = "";
    renderBatchEntryResult(result);
    setBatchEntryFeedback("success", "整批登记成功", `${result.batchCode || ""} 已登记 ${result.registeredQuantity ?? ""} 台`);
  } catch (error) {
    setBatchEntryFeedback("error", "登记未通过", error.message);
    if (navigator.vibrate) navigator.vibrate([120, 60, 120]);
  } finally {
    state.batchEntryBusy = false;
    submit.disabled = false;
    focusBatchEntry();
  }
}

function renderBatchEntryResult(result) {
  $("#batch-entry-result").innerHTML = [
    ["批次码", result.batchCode || "-"],
    ["产品型号", result.productModelName || "-"],
    ["计划台数", result.plannedQuantity ?? "-"],
    ["登记台数", result.registeredQuantity ?? "-"],
    ["质量状态", recordStatusMeta[result.qualityStatus]?.[0] || result.qualityStatus || "待检"],
    ["登记时间", formatDate(result.registeredAt)],
    ["操作人", result.operatorName || "-"],
  ].map((item) => `<div class="batch-entry-result-row"><span>${item[0]}</span><strong>${escapeHtml(item[1])}</strong></div>`).join("");
}

function resetBatchEntry() {
  $("#batch-entry-code").value = "";
  $("#batch-entry-quantity").value = "";
  $("#batch-entry-result").innerHTML = '<div class="empty-block">扫码登记成功后在此显示批次信息</div>';
  setBatchEntryFeedback("neutral", "尚未开始", "请扫描批次二维码");
  focusBatchEntry();
}

// ---- 扫码查询（已认证用户） ----

function loadBatchTrace() {
  $("#batch-trace-result").classList.add("hidden");
  const input = $("#batch-trace-form")?.elements.code;
  if (input) { input.value = ""; setTimeout(() => input.focus(), 30); }
}

async function submitBatchTrace(event) {
  event.preventDefault();
  const code = new FormData(event.currentTarget).get("code").trim();
  if (!code) return;
  try {
    const data = await api("/api/batch-trace/query", { method: "POST", body: { code } });
    state.batchTrace = data;
    const consumption = data.reverseTrace || [];
    $("#batch-trace-summary-strip").innerHTML = [
      ["批次码", data.batchCode || "-", "整批唯一二维码码值"],
      ["产品型号", data.productName || "-", "本批走步机型号", "package"],
      ["计划台数", data.plannedQuantity ?? "-", "计划生产数量", "clipboard-list"],
      ["登记台数", data.registered ? (data.registeredQuantity ?? "-") : "未登记", "已扫码登记数量", "building-factory-2"],
      ["生成时间", formatDate(data.generatedAt), "批次生成时间", "route"],
      ["质量状态", data.registered ? (recordStatusMeta[data.qualityStatus]?.[0] || data.qualityStatus || "-") : "未登记", "批次级质量状态", "layout-dashboard"],
    ].map((item) => summaryCard(item[0], item[1], item[2], item[3])).join("");
    $("#batch-trace-consumption-table").innerHTML = consumption.map((item) => `<tr><td class="code-text">${escapeHtml(item.supplierBatchNo || item.supplierInventoryBatchId || "-")}</td><td>${escapeHtml(item.partTypeName || item.partName || "-")}</td><td class="right">${escapeHtml(item.quantityConsumed ?? "-")}</td></tr>`).join("") || '<tr><td colspan="3" class="empty-row">暂无供应批次消耗记录</td></tr>';
    $("#batch-trace-result").classList.remove("hidden");
  } catch (error) { showError(error, "查询失败"); }
}

// ---- 批次质量处理（管理员） ----

async function loadBatchQuality() {
  const query = new URLSearchParams();
  const code = $("#batch-quality-code-filter").value.trim();
  const from = $("#batch-quality-from").value;
  const to = $("#batch-quality-to").value;
  if (from && to && from > to) {
    toast("筛选时间无效", "开始时间不能晚于结束时间", "error");
    return;
  }
  if (code) query.set("batchCode", code);
  if (from) query.set("from", from);
  if (to) query.set("to", to);
  state.batchQualityRecords = await api(`/api/batch-trace-records${query.toString() ? `?${query}` : ""}`);
  renderBatchQuality();
  const summary = [`${state.batchQualityRecords.length} 条记录`];
  if (code) summary.push(`批次码 ${code}`);
  if (from || to) summary.push(`${from || "最早"} 至 ${to || "今天"}`);
  $("#batch-quality-summary").textContent = summary.join(" · ");
}

function renderBatchQuality() {
  $("#batch-quality-table").innerHTML = (state.batchQualityRecords || []).map((record) => {
    const actionable = (record.qualityStatus || "ASSEMBLED") === "ASSEMBLED";
    const actions = actionable
      ? `<button class="button secondary small" data-action="open-batch-quality" data-id="${record.id}">质量处理</button>`
      : '<span class="cell-sub">已处理</span>';
    return `<tr><td><span class="cell-main code-text">${escapeHtml(record.batchCode || "-")}</span></td><td>${escapeHtml(record.productName || "-")}</td><td>${escapeHtml(record.registeredQuantity ?? "-")}</td><td>${recordStatusBadge(record.qualityStatus || "ASSEMBLED")}${record.statusReason ? `<span class="cell-sub">${escapeHtml(record.statusReason)}</span>` : ""}</td><td>${escapeHtml(record.operatorName || "-")}</td><td>${formatDate(record.generatedAt)}</td><td><div class="table-actions">${actions}</div></td></tr>`;
  }).join("") || '<tr><td colspan="7" class="empty-row">当前筛选条件下没有批次登记记录</td></tr>';
}

function updateBatchQualityNote() {
  const form = $("#batch-quality-form");
  const isHold = form.elements.status.value === "HOLD";
  $("#batch-quality-reason-field").classList.toggle("hidden", !isHold);
  $("#batch-quality-note").textContent = isHold ? "暂扣时必须填写 1 至 500 个字符的原因" : "合格放行将整批走步机标记为合格";
}

function openBatchQualityModal(recordId) {
  // Also resolvable from the dashboard queue so quality can be handled straight
  // from the board without first opening 批次质量处理.
  const record = (state.batchQualityRecords || []).find((item) => item.id === recordId)
    || (state.dashboard?.batchQualityQueue || []).find((item) => item.id === recordId);
  if (!record) return;
  const form = $("#batch-quality-form");
  form.reset();
  form.elements.recordId.value = record.id;
  form.elements.status.value = "PASSED";
  $("#batch-quality-machine").textContent = `${record.batchCode || ""} · ${record.productName || ""} · ${record.registeredQuantity ?? ""} 台`;
  updateBatchQualityNote();
  openModal("batch-quality-modal");
  setTimeout(() => form.elements.status.focus(), 30);
}

async function submitBatchQuality(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const recordId = Number(form.elements.recordId.value);
  const status = form.elements.status.value;
  try {
    if (status === "HOLD") {
      await api(`/api/batch-trace-records/${recordId}/hold`, { method: "POST", body: { reason: form.elements.reason.value } });
    } else {
      await api(`/api/batch-trace-records/${recordId}/pass`, { method: "POST", body: {} });
    }
    closeModal("batch-quality-modal");
    // Refresh whichever page invoked the modal.
    if (state.view === "dashboard") await loadDashboard();
    else await loadBatchQuality();
    toast("批次质量状态已更新");
  } catch (error) { showError(error, "质量状态更新失败"); }
}

function resetBatchQualityFilters() {
  $("#batch-quality-code-filter").value = "";
  $("#batch-quality-from").value = "";
  $("#batch-quality-to").value = "";
  loadBatchQuality().catch((error) => showError(error, "批次登记记录加载失败"));
}

function focusBatchEntry() {
  const shell = $("#app-shell");
  if (!shell || shell.classList.contains("hidden") || state.view !== "batch-entry" || anyModalOpen()) return;
  const input = $("#batch-entry-code");
  if (!input) return;
  const apply = () => {
    if (!shell.classList.contains("hidden") && state.view === "batch-entry" && !anyModalOpen()) {
      input.focus({ preventScroll: true });
    }
  };
  apply();
  requestAnimationFrame(apply);
  setTimeout(apply, 60);
  setTimeout(apply, 180);
}

function handleBatchEntryKeydown(event) {
  if (state.view !== "batch-entry" || anyModalOpen()) return;
  const input = $("#batch-entry-code");
  if (!input || state.batchEntryBusy || event.ctrlKey || event.altKey || event.metaKey) return;
  const target = event.target;
  const editable = target instanceof HTMLInputElement || target instanceof HTMLTextAreaElement || target instanceof HTMLSelectElement;
  if (editable && target !== input) return;
  if (event.key === "Enter") {
    event.preventDefault();
    if (input.value.trim()) $("#batch-entry-form").requestSubmit();
    else focusBatchEntry();
    return;
  }
  if (event.key.length === 1 && target !== input) {
    event.preventDefault();
    input.value += event.key;
    focusBatchEntry();
  }
}

function setRecordScanTarget(input, select = false) {
  $$(".record-scan-input").forEach((item) => item.classList.toggle("scan-target-active", item === input));
  state.recordScanTarget = input;
  state.recordScanReplace = true;
  if (select) input.select();
}

function handleRecordScannerKeydown(event) {
  const modal = $("#record-edit-modal");
  if (!modal || modal.classList.contains("hidden")) return false;
  if (event.ctrlKey || event.altKey || event.metaKey) return false;
  const inputs = $$(".record-scan-input", modal);
  const eventTarget = event.target instanceof HTMLInputElement && event.target.classList.contains("record-scan-input") ? event.target : null;
  const target = eventTarget || state.recordScanTarget;
  if (!target || !inputs.includes(target)) return false;
  const editable = event.target instanceof HTMLInputElement
    || event.target instanceof HTMLTextAreaElement
    || event.target instanceof HTMLSelectElement
    || event.target.isContentEditable;
  const interactive = editable || event.target.closest?.("button, a[href], [role='button']");
  if (interactive && event.target !== target) return false;
  if (event.key === "Enter") {
    event.preventDefault();
    target.value = target.value.trim();
    const next = inputs[inputs.indexOf(target) + 1] || target;
    next.focus({ preventScroll: true });
    setRecordScanTarget(next, true);
    return true;
  }
  if (event.key.length === 1 && event.target !== target) {
    event.preventDefault();
    target.value = state.recordScanReplace ? event.key : `${target.value}${event.key}`;
    target.focus({ preventScroll: true });
    target.setSelectionRange(target.value.length, target.value.length);
    state.recordScanReplace = false;
  } else if (event.key.length === 1) {
    state.recordScanReplace = false;
  }
  return true;
}

// 批次登记记录: the current batch registration history (batch_trace_records),
// shared with 批次生成 / 批次质量, replacing the retired per-unit record list so
// the page no longer reads the empty legacy /api/records endpoint.
async function loadMyRecords() {
  $("#my-record-title").textContent = isAdmin() ? "全部批次登记记录" : "批次登记记录";
  $("#my-record-help").textContent = isAdmin()
    ? "查看全部整批走步机的登记台数与质量状态"
    : "查看已授权产品的整批登记与质量状态";
  state.batchRecords = await api("/api/batch-trace-records");
  renderMyRecords();
}

function renderMyRecords() {
  const search = ($("#my-record-search").value || "").trim().toLowerCase();
  const records = (state.batchRecords || []).filter((record) => {
    if (!search) return true;
    return `${record.batchCode || ""} ${record.productName || ""}`.toLowerCase().includes(search);
  });
  $("#my-record-table").innerHTML = records.map((record) => `<tr><td><span class="cell-main code-text">${escapeHtml(record.batchCode || "-")}</span></td><td><span class="cell-main">${escapeHtml(record.productName || "-")}</span><span class="cell-sub code-text">${escapeHtml(record.productModelCode || "")}</span></td><td>${escapeHtml(record.registeredQuantity ?? "-")}</td><td>${recordStatusBadge(record.qualityStatus || "ASSEMBLED")}</td><td>${escapeHtml(record.operatorName || "-")}</td><td>${formatDate(record.generatedAt)}</td></tr>`).join("") || '<tr><td colspan="6" class="empty-row">暂无批次登记记录</td></tr>';
}

function openRecordEdit(recordId) {
  const record = state.records.find((item) => item.id === recordId);
  if (!record) return;
  const form = $("#record-edit-form");
  form.elements.recordId.value = record.id;
  $("#record-edit-machine").textContent = `${record.machine.productModelName || record.machine.model} · ${record.machine.sn}`;
  $("#record-part-editor").innerHTML = record.parts.map((part) => `<label class="field">${part.position}. ${escapeHtml(part.partName)}<input class="record-scan-input" name="partCode" value="${escapeHtml(part.identificationCode)}" required maxlength="120" autocomplete="off"></label>`).join("");
  $("#record-part-editor").insertAdjacentHTML("beforeend", `<label class="field">校对备注<textarea name="remarks" maxlength="200" rows="3" placeholder="可填写本次修改说明">${escapeHtml(record.remarks || "")}</textarea></label>`);
  const inputs = $$(".record-scan-input", form);
  inputs.forEach((input) => {
    input.addEventListener("focus", () => setRecordScanTarget(input, true));
    input.addEventListener("click", () => setRecordScanTarget(input, true));
  });
  openModal("record-edit-modal");
  if (inputs[0]) {
    setRecordScanTarget(inputs[0], true);
    setTimeout(() => { inputs[0].focus(); setRecordScanTarget(inputs[0], true); }, 30);
  }
}

async function submitRecordEdit(event) {
  event.preventDefault();
  const form = event.currentTarget;
  try {
    await api(`/api/records/${Number(form.elements.recordId.value)}`, { method: "PUT", body: { partCodes: $$("[name='partCode']", form).map((input) => input.value), remarks: form.elements.remarks.value } });
    closeModal("record-edit-modal");
    await loadMyRecords();
    toast("录入记录已修改");
  } catch (error) { showError(error, "记录修改失败"); }
}

function openRecordDelete(recordId) {
  const record = state.records.find((item) => item.id === recordId);
  if (!record) return;
  const form = $("#record-delete-form");
  form.reset();
  form.elements.recordId.value = record.id;
  $("#record-delete-machine").textContent = `${record.machine.productModelName || record.machine.model} · ${record.machine.sn}`;
  openModal("record-delete-modal");
}

async function submitRecordDelete(event) {
  event.preventDefault();
  const form = event.currentTarget;
  try {
    await api(`/api/records/${Number(form.elements.recordId.value)}`, { method: "DELETE", body: { reason: form.elements.reason.value } });
    closeModal("record-delete-modal");
    await loadMyRecords();
    toast("录入记录已删除", "该套二维码可以重新录入");
  } catch (error) { showError(error, "记录删除失败"); }
}

async function openRelatedProduct(productId) {
  await switchView("products");
  await openProductDetail(productId);
}

function syncStatusBadge(status) {
  const labels = { PENDING: "待推送", PUSHED: "已推送", FAILED: "推送失败" };
  const classes = { PENDING: "pending", PUSHED: "passed", FAILED: "hold" };
  return `<span class="status ${classes[status] || ""}">${labels[status] || escapeHtml(status || "-")}</span>`;
}

function purchaseOrderFieldHtml(column) {
  const def = PURCHASE_ORDER_FIELD_DEFAULTS[column] || "";
  const options = PURCHASE_ORDER_FIELD_OPTIONS[column];
  if (options) {
    const opts = ['<option value="">（不选）</option>']
      .concat(options.map((opt) => `<option value="${escapeHtml(opt)}" ${opt === def ? "selected" : ""}>${escapeHtml(opt)}</option>`))
      .join("");
    return `<label class="field">${escapeHtml(column)}<select class="po-field" data-column="${escapeHtml(column)}">${opts}</select></label>`;
  }
  if (PURCHASE_ORDER_DATE_COLUMNS.has(column)) {
    return `<label class="field">${escapeHtml(column)}<input class="po-field" data-column="${escapeHtml(column)}" type="date"></label>`;
  }
  const numeric = PURCHASE_ORDER_NUMERIC_COLUMNS.has(column);
  const placeholder = column === "采购单号" ? ' placeholder="留空自动生成"' : "";
  const value = def ? ` value="${escapeHtml(def)}"` : "";
  return `<label class="field">${escapeHtml(column)}<input class="po-field" data-column="${escapeHtml(column)}" ${numeric ? 'type="number" step="any"' : 'type="text" maxlength="500"'}${value}${placeholder}></label>`;
}

function renderPurchaseOrderFormOptions() {
  const form = $("#purchase-order-form");
  if (!form) return;
  const productSelect = form.elements.productModelId;
  if (productSelect) {
    const selected = Number(productSelect.value || 0);
    const products = (state.products || []).filter((item) => item.active !== false);
    const placeholder = products.length ? "请选择关联产品" : "请先在“我的产品”中添加产品";
    productSelect.innerHTML = `<option value="">${placeholder}</option>` + products.map((item) => `<option value="${item.id}" ${item.id === selected ? "selected" : ""}>${escapeHtml(item.name)} · ${escapeHtml(item.productCode)}</option>`).join("");
  }
  const container = $("#purchase-order-fields");
  if (container && !container.dataset.rendered) {
    const primary = PURCHASE_ORDER_PRIMARY_COLUMNS.filter((column) => PURCHASE_ORDER_FIELD_COLUMNS.includes(column));
    const more = PURCHASE_ORDER_FIELD_COLUMNS.filter((column) => !primary.includes(column));
    container.innerHTML = `
      <div class="po-fields-grid">${primary.map(purchaseOrderFieldHtml).join("")}</div>
      <details class="po-more"><summary>更多字段（选填，共 ${more.length} 项）</summary>
        <div class="po-fields-grid">${more.map(purchaseOrderFieldHtml).join("")}</div>
      </details>`;
    container.dataset.rendered = "1";
  }
}

async function loadPurchaseOrders() {
  state.products = await api("/api/products");
  renderPurchaseOrderFormOptions();
  // Push availability (endpoints configured) so we can gate the 推送领星 button.
  try { state.lingxingStatus = await api("/api/lingxing/status"); }
  catch (_) { state.lingxingStatus = null; }
  // 采购单下单 only needs its own endpoint, not the inbound / inventory ones.
  const pushReady = Boolean(
    state.lingxingStatus?.endpointsConfigured?.purchaseOrder
      ?? state.lingxingStatus?.writeEndpointsConfigured,
  );
  state.purchaseOrders = await api("/api/purchase-orders");
  // Only admins act on factory progress; skip the per-order lookups for
  // operations (procurement view) to avoid noise and needless requests.
  const withProgress = await Promise.all(state.purchaseOrders.map(async (order) => {
    if (!isAdmin()) return { ...order, progress: null };
    try { return { ...order, progress: await api(`/api/purchase-orders/${order.id}/factory-progress`) }; }
    catch (_) { return { ...order, progress: null }; }
  }));
  $("#purchase-order-table").innerHTML = withProgress.map((order) => {
    const fields = order.fields || {};
    const productLabel = order.productModelName
      ? `${order.productModelName}${order.productModelCode ? ` · ${order.productModelCode}` : ""}`
      : (order.partName ? `${order.partName}${order.partCode ? ` · ${order.partCode}` : ""}` : (fields["SKU"] || "-"));
    const supplierLabel = fields["供应商"] || order.supplierName || "";
    const rawQuantity = (fields["实际采购量"] != null && fields["实际采购量"] !== "") ? fields["实际采购量"] : (order.quantity != null ? order.quantity : "-");
    const progressCell = order.progress
      ? `<span class="cell-main">${order.progress.productionOrderGenerated ? "已生成生产订单" : "未生成"}</span><span class="cell-sub">累计入库 ${order.progress.latestQuantity ?? 0}</span>`
      : '<span class="cell-sub">—</span>';
    const notPushed = order.syncStatus !== "PUSHED";
    const pushButton = notPushed
      ? (pushReady
          ? `<button class="button secondary small" data-action="push-purchase-order" data-id="${order.id}">推送领星</button>`
          : '<button class="button secondary small" type="button" disabled title="请先在系统设置中配置领星采购订单接口">推送领星</button>')
      : "";
    const editButton = notPushed ? `<button class="button text small" data-action="edit-purchase-order" data-id="${order.id}">编辑</button>` : "";
    const actions = `${pushButton}${editButton}<button class="button text small" data-action="copy-purchase-order" data-id="${order.id}">复制</button><a class="button text small" href="/api/purchase-orders/${order.id}/export">导出</a><button class="button text small danger" data-action="delete-purchase-order" data-id="${order.id}">删除</button>`;
    return `<tr><td><span class="cell-main">${escapeHtml(order.poNo)}</span><span class="cell-sub">采购人：${escapeHtml(order.createdBy || "-")}</span></td><td><span class="cell-main">${escapeHtml(productLabel)}</span><span class="cell-sub">${escapeHtml(supplierLabel)}</span></td><td>${escapeHtml(String(rawQuantity))}</td><td>${syncStatusBadge(order.syncStatus)}${order.pushError ? `<span class="cell-sub warning-text">${escapeHtml(order.pushError)}</span>` : ""}</td><td>${progressCell}</td><td>${formatDate(order.createdAt)}</td><td><div class="table-actions">${actions}</div></td></tr>`;
  }).join("") || '<tr><td colspan="7" class="empty-row">暂无采购订单</td></tr>';
  await loadOperationsInbound();
}

function setPurchaseOrderFormMode(mode, order = null) {
  const submit = $("#po-submit");
  const cancel = $("#po-cancel-edit");
  const label = $("#po-form-mode");
  if (!submit) return;
  if (mode === "edit" && order) {
    submit.textContent = "保存修改";
    cancel?.classList.remove("hidden");
    if (label) label.textContent = `正在编辑 ${order.poNo}`;
  } else if (mode === "copy" && order) {
    submit.textContent = "创建采购订单";
    cancel?.classList.remove("hidden");
    if (label) label.textContent = `按 ${order.poNo} 复制新建`;
  } else {
    submit.textContent = "创建采购订单";
    cancel?.classList.add("hidden");
    if (label) label.textContent = "";
  }
}

function fillPurchaseOrderForm(order, { editing }) {
  const form = $("#purchase-order-form");
  if (!form) return;
  const container = $("#purchase-order-fields");
  if (container && !container.dataset.rendered) renderPurchaseOrderFormOptions();
  form.elements.productModelId.value = order.productModelId || "";
  const fields = order.fields || {};
  let hasMoreValue = false;
  $$(".po-field", form).forEach((input) => {
    const column = input.dataset.column;
    // A copied order gets a fresh document number, so leave 采购单号 blank.
    if (!editing && column === "采购单号") { input.value = ""; return; }
    const value = fields[column] != null ? String(fields[column]) : "";
    input.value = value;
    if (value && !PURCHASE_ORDER_PRIMARY_COLUMNS.includes(column)) hasMoreValue = true;
  });
  const details = $(".po-more", form);
  if (details) details.open = hasMoreValue;
  state.editingPurchaseOrderId = editing ? order.id : null;
  setPurchaseOrderFormMode(editing ? "edit" : "copy", order);
  form.scrollIntoView({ behavior: "smooth", block: "start" });
}

function editPurchaseOrder(orderId) {
  const order = state.purchaseOrders.find((item) => item.id === orderId);
  if (!order) return;
  fillPurchaseOrderForm(order, { editing: true });
}

function copyPurchaseOrder(orderId) {
  const order = state.purchaseOrders.find((item) => item.id === orderId);
  if (!order) return;
  fillPurchaseOrderForm(order, { editing: false });
  toast("已复制采购单内容", "已按所选订单预填，可修改后创建新单");
}

function cancelPurchaseOrderEdit() {
  state.editingPurchaseOrderId = null;
  const form = $("#purchase-order-form");
  if (form) form.elements.productModelId.value = "";
  const container = $("#purchase-order-fields");
  if (container) container.dataset.rendered = "";
  renderPurchaseOrderFormOptions();
  setPurchaseOrderFormMode("create");
}

async function deletePurchaseOrder(orderId) {
  const order = state.purchaseOrders.find((item) => item.id === orderId);
  if (!order) return;
  if (!window.confirm(`确定删除采购订单「${order.poNo}」吗？此操作不可恢复。`)) return;
  try {
    await api(`/api/purchase-orders/${orderId}`, { method: "DELETE" });
    if (state.editingPurchaseOrderId === orderId) cancelPurchaseOrderEdit();
    await loadPurchaseOrders();
    toast("采购订单已删除");
  } catch (error) { showError(error, "采购订单删除失败"); }
}

async function loadOperationsInbound() {
  const target = $("#operations-inbound-table");
  if (!target) return;
  try {
    state.inboundReceipts = await api("/api/inbound-receipts");
    target.innerHTML = state.inboundReceipts.map((item) => `<tr><td>#${item.id}</td><td>${escapeHtml(item.poNo)}</td><td>${escapeHtml(item.partName)}</td><td>${item.quantity}</td><td>${syncStatusBadge(item.syncStatus)}</td><td>${formatDate(item.receivedAt)}</td><td><div class="table-actions">${item.syncStatus !== "PUSHED" ? `<button class="button secondary small" data-action="push-inbound-receipt" data-id="${item.id}">推送入库</button>` : ""}</div></td></tr>`).join("") || '<tr><td colspan="7" class="empty-row">暂无供应收货记录</td></tr>';
  } catch (error) {
    target.innerHTML = `<tr><td colspan="7" class="empty-row">${escapeHtml(error.message)}</td></tr>`;
  }
}

async function submitPurchaseOrder(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const productModelId = Number(form.elements.productModelId.value || 0);
  if (!productModelId) { showError(new Error("请选择关联产品"), "采购订单创建失败"); return; }
  const fields = {};
  $$(".po-field", form).forEach((input) => {
    const value = input.value.trim();
    if (value !== "") fields[input.dataset.column] = value;
  });
  const editingId = state.editingPurchaseOrderId;
  try {
    if (editingId) {
      await api(`/api/purchase-orders/${editingId}`, { method: "PUT", body: { productModelId, fields } });
    } else {
      await api("/api/purchase-orders", { method: "POST", body: { productModelId, fields } });
    }
    // Rebuild the field inputs so they reset to their editable defaults.
    state.editingPurchaseOrderId = null;
    const container = $("#purchase-order-fields");
    if (container) container.dataset.rendered = "";
    form.elements.productModelId.value = "";
    renderPurchaseOrderFormOptions();
    setPurchaseOrderFormMode("create");
    await loadPurchaseOrders();
    toast(editingId ? "采购订单已更新" : "采购订单已创建");
  } catch (error) { showError(error, editingId ? "采购订单更新失败" : "采购订单创建失败"); }
}

async function pushPurchaseOrder(orderId) {
  const result = await api(`/api/purchase-orders/${orderId}/push`, { method: "POST", body: {} });
  await loadPurchaseOrders();
  toast("采购单已下单", result.lingxingPoId ? `领星采购单号 ${result.lingxingPoId} 已转为待到货` : "领星同步完成");
}

// Label for a purchase order in the warehouse selects. Free-form orders have no
// linked 商品(part), so fall back to the product / SKU instead of printing null.
function purchaseOrderOptionLabel(order, { withQuantity = false } = {}) {
  const summary = order.summary || {};
  const name = summary.productName || order.productModelName || order.partName || "";
  const sku = summary.sku || order.productModelCode || order.partCode || "";
  const parts = [order.poNo];
  if (name) parts.push(name);
  else if (sku) parts.push(sku);
  if (withQuantity) {
    const quantity = summary.orderedQuantity != null ? summary.orderedQuantity : order.quantity;
    if (quantity != null && quantity !== "") parts.push(`× ${formatNumber(quantity)}`);
  }
  return parts.join(" · ");
}

// Renders the selected order's key data so the warehouse can confirm what it is
// receiving / producing before submitting.
function renderPurchaseOrderSummary(target, order) {
  const container = typeof target === "string" ? $(target) : target;
  if (!container) return;
  if (!order) {
    container.innerHTML = '<div class="empty-block compact-empty">选择采购订单后显示订单明细</div>';
    return;
  }
  const summary = order.summary || {};
  const rows = [
    ["SKU", summary.sku],
    ["品名", summary.productName],
    ["店铺", summary.shop],
    ["FNSKU", summary.fnsku],
    ["负责人", summary.owner],
    ["供应商", summary.supplierName],
    ["下单时间", formatDate(summary.orderedAt)],
    ["原计划到货时间", summary.plannedArrivalAt],
    ["实际到货时间", summary.actualArrivalAt ? formatDate(summary.actualArrivalAt) : ""],
    ["单价", formatMoney(summary.unitPrice)],
    ["原采购单数量", formatNumber(summary.orderedQuantity)],
    ["实际到货数量", formatNumber(summary.receivedQuantity)],
    ["采购单金额", formatMoney(summary.orderAmount)],
    ["实际到货金额", formatMoney(summary.receivedAmount)],
  ];
  container.innerHTML = `<div class="order-summary-grid">${rows.map(([label, value]) => `<div class="order-summary-item"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value === "" || value == null ? "-" : value)}</strong></div>`).join("")}</div>`;
}

// Keeps a form's summary panel in sync with its purchase-order select.
function bindPurchaseOrderSummary(formSelector, panelSelector) {
  const form = $(formSelector);
  if (!form) return;
  const select = form.elements.purchaseOrderId;
  const update = () => {
    const id = Number(select.value || 0);
    renderPurchaseOrderSummary(panelSelector, (state.purchaseOrders || []).find((item) => item.id === id));
  };
  select.addEventListener("change", update);
  update();
}

async function loadInboundReceipts() {
  [state.purchaseOrders, state.inboundReceipts] = await Promise.all([api("/api/purchase-orders"), api("/api/inbound-receipts")]);
  const select = $("#inbound-receipt-form").elements.purchaseOrderId;
  const previous = select.value;
  select.innerHTML = '<option value="">请选择采购订单</option>' + state.purchaseOrders.map((item) => `<option value="${item.id}">${escapeHtml(purchaseOrderOptionLabel(item))}</option>`).join("");
  if (previous && state.purchaseOrders.some((item) => String(item.id) === previous)) select.value = previous;
  renderPurchaseOrderSummary("#inbound-receipt-summary", state.purchaseOrders.find((item) => String(item.id) === select.value));
  $("#inbound-receipt-table").innerHTML = state.inboundReceipts.map((item) => `<tr><td>#${item.id}</td><td>${escapeHtml(item.poNo)}</td><td><span class="cell-main">${escapeHtml(item.supplierName)}</span><span class="cell-sub">${escapeHtml(item.partName)} · ${escapeHtml(item.partCode)}</span></td><td>${item.quantity}</td><td>${escapeHtml(item.receiver || "-")}</td><td>${syncStatusBadge(item.syncStatus)}</td><td>${formatDate(item.receivedAt)}</td></tr>`).join("") || '<tr><td colspan="7" class="empty-row">暂无供应收货记录</td></tr>';
}

async function submitInboundReceipt(event) {
  event.preventDefault();
  const form = event.currentTarget;
  try {
    await api("/api/inbound-receipts", { method: "POST", body: { purchaseOrderId: Number(form.elements.purchaseOrderId.value), quantity: Number(form.elements.quantity.value) } });
    form.elements.quantity.value = "";
    await loadInboundReceipts();
    toast("供应收货已登记");
  } catch (error) { showError(error, "供应收货登记失败"); }
}

async function pushInboundReceipt(receiptId) {
  const result = await api(`/api/inbound-receipts/${receiptId}/push`, { method: "POST", body: {} });
  await loadPurchaseOrders();
  toast("供应收货已推送", result.lingxingInboundId || "领星入库同步完成");
}

function productionTypeBadge(isExternal) {
  return isExternal
    ? '<span class="status hold">外采</span>'
    : '<span class="status passed">自产</span>';
}

// Where a production order sits in the warehouse flow: 批次登记 → 质量放行 → 入库.
function productionFlowCell(order) {
  const progress = order.progress || {};
  const received = progress.receivedQuantity ?? 0;
  const planned = order.quantity ?? 0;
  const stage = progress.fullyReceived
    ? '<span class="status passed">入库完成</span>'
    : received > 0
      ? '<span class="status pending">部分入库</span>'
      : progress.registered
        ? '<span class="status pending">待入库</span>'
        : '<span class="status">待登记</span>';
  const registration = progress.registered
    ? recordStatusMeta[progress.qualityStatus]?.[0] || progress.qualityStatus || "待检"
    : "未登记";
  const blocked = progress.canStockIn === false
    ? `<span class="cell-sub warning-text">${escapeHtml(progress.stockInBlockedReason || "")}</span>`
    : "";
  return `${stage}<span class="cell-sub">入库 ${received} / ${planned} · ${escapeHtml(registration)}</span>${blocked}`;
}

async function loadProductionOrders() {
  [state.purchaseOrders, state.productionOrders] = await Promise.all([api("/api/purchase-orders"), api("/api/production-orders")]);
  const used = new Set(state.productionOrders.map((item) => item.purchaseOrderId));
  const available = state.purchaseOrders.filter((item) => !used.has(item.id));
  state.pendingPurchaseOrders = available;
  const checkAll = $("#pending-po-check-all");
  if (checkAll) checkAll.checked = false;
  $("#pending-purchase-order-table").innerHTML = available.map((item) => {
    const summary = item.summary || {};
    const name = summary.productName || item.productModelName || item.partName || "-";
    const sku = summary.sku || item.productModelCode || item.partCode || "";
    const planned = summary.orderedQuantity != null ? summary.orderedQuantity : item.quantity;
    const received = summary.receivedQuantity;
    return `<tr><td class="select-column"><input type="checkbox" class="pending-po-checkbox" value="${item.id}" aria-label="选择采购单 ${escapeHtml(item.poNo)}"></td><td><span class="cell-main">${escapeHtml(item.poNo)}</span><span class="cell-sub">采购人：${escapeHtml(item.createdBy || "-")}</span></td><td><span class="cell-main">${escapeHtml(name)}</span><span class="cell-sub">${escapeHtml(sku)}</span></td><td>${planned == null || planned === "" ? "-" : formatNumber(planned)}</td><td>${received == null ? "-" : formatNumber(received)}</td></tr>`;
  }).join("") || '<tr><td colspan="5" class="empty-row">暂无待生成的采购订单</td></tr>';
  if ($("#pending-po-summary")) $("#pending-po-summary").textContent = `${available.length} 张采购单待生成生产订单`;
  $("#production-order-table").innerHTML = state.productionOrders.map((item) => `<tr><td>#${item.id}</td><td>${escapeHtml(item.poNo)}</td><td><span class="cell-main">${escapeHtml(item.productName)}</span><span class="cell-sub">${escapeHtml(item.productModelCode)}</span></td><td>${productionTypeBadge(item.isExternal)}</td><td>${item.quantity}</td><td>${productionFlowCell(item)}</td><td><button class="qr-thumb-button" type="button" data-action="view-production-qr" data-id="${item.id}" title="查看 / 打印生产二维码"><img class="qr-thumb" src="${item.downloadUrl}" alt="生产二维码" loading="lazy"></button><span class="cell-sub code-text">${escapeHtml(item.productionQrCode)}</span></td><td>${formatDate(item.createdAt)}</td><td><button class="button secondary small" data-action="view-production-qr" data-id="${item.id}">查看 / 打印</button></td></tr>`).join("") || '<tr><td colspan="9" class="empty-row">暂无生产订单</td></tr>';
}

function selectedPendingPurchaseOrderIds() {
  return $$(".pending-po-checkbox").filter((box) => box.checked).map((box) => Number(box.value));
}

async function generateProductionOrders(external) {
  const ids = selectedPendingPurchaseOrderIds();
  if (!ids.length) {
    toast("请先勾选采购单", "请至少选择一张待生成的采购订单", "error");
    return;
  }
  const result = await api("/api/production-orders/batch", {
    method: "POST",
    body: { purchaseOrderIds: ids, external: Boolean(external) },
  });
  await loadProductionOrders();
  const created = (result.created || []).length;
  const skipped = (result.skipped || []).length;
  const failed = result.failed || [];
  const parts = [`生成 ${created} 单`];
  if (skipped) parts.push(`跳过 ${skipped} 单（已生成）`);
  if (failed.length) parts.push(`失败 ${failed.length} 单`);
  const tone = failed.length ? "error" : "success";
  toast(external ? "外采生产订单已生成" : "生产订单已生成", parts.join(" · "), tone);
  if (failed.length) {
    const detail = failed.map((item) => `${item.poNo || item.purchaseOrderId}：${item.message}`).join("；");
    showError(new Error(detail), "部分采购单生成失败");
  }
}

function openProductionQr(orderId) {
  const order = state.productionOrders.find((item) => item.id === orderId);
  if (!order) return;
  $("#production-qr-sub").textContent = `${order.poNo} · 计划 ${order.quantity} 件${order.isExternal ? " · 外采" : ""}`;
  $("#production-qr-image").src = order.downloadUrl;
  $("#production-qr-product").textContent = `${order.productName} · ${order.productModelCode}${order.isExternal ? " · 外采" : ""}`;
  $("#production-qr-code").textContent = order.productionQrCode;
  const download = $("#production-qr-download");
  download.href = order.downloadUrl;
  download.setAttribute("download", `${order.productionQrCode}.svg`);
  openModal("production-qr-modal");
}

function printProductionQr() {
  const area = $("#production-qr-print");
  if (!area) return;
  area.classList.add("printing");
  window.print();
  area.classList.remove("printing");
}

function focusScanGun() {
  if (state.view !== "scan-gun" || anyModalOpen()) return;
  setTimeout(() => $("#scan-gun-code")?.focus({ preventScroll: true }), 20);
}

// Recent 成品扫码入库 history, so the operator sees the record right after
// confirming an inbound instead of only a toast.
async function loadScanGunRecords() {
  const target = $("#scan-gun-record-table");
  if (!target) return;
  try {
    state.inboundScanRecords = await api("/api/inbound-scan-records?limit=50");
    target.innerHTML = state.inboundScanRecords.map((item) => `<tr><td>#${item.id}</td><td><span class="cell-main">${escapeHtml(item.productName || "-")}</span><span class="cell-sub">${escapeHtml(item.productModelCode || "")}</span></td><td><span class="cell-main code-text">${escapeHtml(item.productionQrCode || "-")}</span><span class="cell-sub">${escapeHtml(item.poNo || "-")}</span></td><td>${formatNumber(item.quantity)}</td><td>${item.onHand == null ? "-" : formatNumber(item.onHand)}</td><td>${escapeHtml(item.operatorName || "-")}</td><td>${formatDate(item.receivedAt)}</td></tr>`).join("")
      || '<tr><td colspan="7" class="empty-row">暂无成品入库记录，扫描生产二维码后在此显示</td></tr>';
  } catch (error) {
    target.innerHTML = `<tr><td colspan="7" class="empty-row">${escapeHtml(error.message)}</td></tr>`;
  }
}

function loadScanGun() {
  state.scanGunOrder = null;
  $("#scan-gun-lookup-form")?.reset();
  $("#scan-gun-inbound-form")?.classList.add("hidden");
  if ($("#scan-gun-order")) $("#scan-gun-order").innerHTML = "扫码后显示订单与产品";
  loadScanGunRecords();
  focusScanGun();
}

async function submitScanGunLookup(event) {
  event.preventDefault();
  const form = event.currentTarget;
  try {
    state.scanGunOrder = await api("/api/scan-gun/lookup", { method: "POST", body: { code: form.elements.code.value } });
    const order = state.scanGunOrder;
    const progress = order.progress || {};
    const blocked = progress.canStockIn === false;
    // Surface the batch's place in the flow (登记 → 质量 → 入库) before the operator
    // confirms, and refuse up front when the quality gate blocks this batch.
    $("#scan-gun-feedback").className = `scan-feedback ${blocked ? "error" : "success"}`;
    $("#scan-gun-feedback").innerHTML = blocked
      ? `<strong>该批次暂不可入库</strong><span>${escapeHtml(progress.stockInBlockedReason || "请先完成批次登记与质量放行")}</span>`
      : `<strong>已找到生产订单</strong><span>${escapeHtml(order.poNo)} · ${escapeHtml(order.productName)}</span>`;
    const registrationLabel = progress.registered
      ? `已登记 ${progress.registeredQuantity ?? "-"} 台 · ${recordStatusMeta[progress.qualityStatus]?.[0] || progress.qualityStatus || "待检"}`
      : "尚未批次登记";
    const receivedLabel = `已入库 ${progress.receivedQuantity ?? 0} / 计划 ${order.quantity ?? "-"}`;
    $("#scan-gun-order").innerHTML = `<div class="batch-entry-card"><strong>${escapeHtml(order.productName)}</strong><span>${escapeHtml(order.productModelCode)} · 计划 ${order.quantity} 件${order.isExternal ? " · 外采" : ""}</span><code>${escapeHtml(order.productionQrCode)}</code>
      <span class="cell-sub">${escapeHtml(registrationLabel)}</span>
      <span class="cell-sub">${escapeHtml(receivedLabel)}</span>
      ${progress.fullyReceived ? '<span class="cell-sub warning-text">该生产订单已按计划入库完毕，请确认是否重复扫码</span>' : ""}
      ${blocked ? `<span class="cell-sub warning-text">${escapeHtml(progress.stockInBlockedReason)}</span>` : ""}</div>`;
    const inboundForm = $("#scan-gun-inbound-form");
    if (blocked) {
      // Keep the form hidden so a blocked batch cannot be submitted at all.
      inboundForm.classList.add("hidden");
      form.elements.code.select();
      return;
    }
    inboundForm.elements.productionOrderId.value = order.id;
    // Default to what is still outstanding so the common case is one keystroke.
    if (progress.remainingQuantity) inboundForm.elements.quantity.value = progress.remainingQuantity;
    inboundForm.classList.remove("hidden");
    inboundForm.elements.quantity.focus();
    inboundForm.elements.quantity.select();
  } catch (error) {
    $("#scan-gun-feedback").className = "scan-feedback error";
    $("#scan-gun-feedback").innerHTML = `<strong>二维码无效</strong><span>${escapeHtml(error.message)}</span>`;
    form.elements.code.select();
  }
}

async function submitScanGunInbound(event) {
  event.preventDefault();
  const form = event.currentTarget;
  try {
    const result = await api("/api/scan-gun/inbound", { method: "POST", body: { productionOrderId: Number(form.elements.productionOrderId.value), quantity: Number(form.elements.quantity.value) } });
    toast("成品入库成功", `最新库存 ${result.onHand}`);
    loadScanGun();
  } catch (error) { showError(error, "成品入库失败"); }
}

async function loadInventorySync() {
  try {
    state.inventorySync = await api("/api/inventory-sync");
    renderInventorySync();
  } catch (error) {
    $("#inventory-sync-status").innerHTML = `<div class="empty-block">${escapeHtml(error.message)}</div>`;
  }
}

function renderInventorySync() {
  const data = state.inventorySync || { overall: {}, items: [] };
  const overall = data.overall || {};
  const items = data.items || [];
  const statusLabels = { PENDING: "待推送", PUSHED: "已推送", FAILED: "推送失败", PARTIAL: "部分成功" };
  $("#inventory-sync-status").innerHTML = `<div class="summary-strip">${[
    ["同步状态", statusLabels[overall.syncStatus] || "尚未同步", "最近一次整体同步结果", "route"],
    ["产品总数", overall.total ?? items.length, "参与同步的产品数量", "package"],
    ["已推送 / 待推送", `${overall.pushed ?? 0} / ${overall.pending ?? 0}`, "各产品同步进度", "clipboard-list"],
    ["最近同步时间", overall.syncedAt ? formatDate(overall.syncedAt) : "-", "最近一次同步时间", "building-factory-2"],
  ].map((item) => summaryCard(item[0], item[1], item[2], item[3])).join("")}${overall.error ? `<div class="notice-panel warning-text">上次同步失败：${escapeHtml(overall.error)}</div>` : ""}</div>`;
  const checkAll = $("#inventory-sync-check-all");
  if (checkAll) checkAll.checked = false;
  $("#inventory-sync-table").innerHTML = items.map((item) => `<tr><td class="select-column"><input type="checkbox" class="inventory-sync-checkbox" value="${item.productModelId}" aria-label="选择产品 ${escapeHtml(item.productName || item.sku || "")}"></td><td><span class="cell-main">${escapeHtml(item.productName || "-")}</span><span class="cell-sub code-text">${escapeHtml(item.sku || "")}</span></td><td>${formatNumber(item.quantity ?? 0)}</td><td>${formatNumber(item.syncedQuantity ?? 0)}</td><td>${syncStatusBadge(item.syncStatus)}${item.pushError ? `<span class="cell-sub warning-text">${escapeHtml(item.pushError)}</span>` : ""}</td><td class="code-text">${escapeHtml(item.lingxingId || "-")}</td><td>${item.syncedAt ? formatDate(item.syncedAt) : "-"}</td></tr>`).join("") || '<tr><td colspan="7" class="empty-row">暂无成品库存，扫码入库后在此显示</td></tr>';
  if ($("#inventory-sync-summary")) $("#inventory-sync-summary").textContent = `${items.length} 个产品 · 已推送 ${overall.pushed ?? 0} · 失败 ${overall.failed ?? 0}`;
}

function selectedInventorySyncIds() {
  return $$(".inventory-sync-checkbox").filter((box) => box.checked).map((box) => Number(box.value));
}

async function syncInventory(selectedOnly) {
  let body = {};
  if (selectedOnly) {
    const ids = selectedInventorySyncIds();
    if (!ids.length) {
      toast("请先勾选产品", "请至少选择一个要同步的产品", "error");
      return;
    }
    body = { productModelIds: ids };
  }
  try {
    const result = await api("/api/inventory-sync", { method: "POST", body });
    state.inventorySync = { overall: result.overall, items: result.items };
    renderInventorySync();
    const failed = (result.results || []).filter((item) => item.status === "FAILED").length;
    if (result.syncStatus === "PARTIAL" || failed) {
      toast("库存同步部分完成", `成功 ${result.itemCount} 个 · 失败 ${failed} 个`, "warning");
    } else {
      toast("库存同步完成", `本次快捷入库 ${result.itemCount} 个产品`);
    }
  } catch (error) { showError(error, "库存同步失败"); }
}

async function loadSettings() {
  const data = await api("/api/settings");
  const form = $("#settings-form");
  form.elements.requireQualityRelease.checked = Boolean(data.requireQualityRelease);
  form.elements.appId.value = "";
  form.elements.appSecret.value = "";
  const endpoints = data.lingxing?.endpoints || {};
  form.elements.endpointPurchaseOrder.value = endpoints.purchaseOrder || "";
  form.elements.endpointInboundReceipt.value = endpoints.inboundReceipt || "";
  form.elements.endpointInventorySync.value = endpoints.inventorySync || "";
  const credentialState = data.lingxing?.configured ? "凭据已配置" : "尚未配置";
  const ready = data.lingxing?.endpointsConfigured || {};
  const endpointState = data.lingxing?.writeEndpointsConfigured
    ? "写入接口已配置"
    : (ready.purchaseOrder ? "采购下单接口已配置，其余待配置" : "写入接口待配置");
  $("#settings-lingxing-state").textContent = `${credentialState}；${endpointState}`;
  $("#settings-lingxing-masked").textContent = data.lingxing?.configured ? `当前值：${data.lingxing.appId} / ${data.lingxing.appSecret}` : "保存后只显示脱敏值；两个凭据需同时填写。";
}

async function submitSettings(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const body = { requireQualityRelease: form.elements.requireQualityRelease.checked };
  const values = [form.elements.appId.value.trim(), form.elements.appSecret.value.trim()];
  if (values.some(Boolean)) {
    if (!values.every(Boolean)) return showError(new Error("appId、appSecret 需同时填写"), "设置保存失败");
    [body.appId, body.appSecret] = values;
  }
  body.lingxingEndpoints = {
    purchaseOrder: form.elements.endpointPurchaseOrder.value.trim(),
    inboundReceipt: form.elements.endpointInboundReceipt.value.trim(),
    inventorySync: form.elements.endpointInventorySync.value.trim(),
  };
  try {
    await api("/api/settings", { method: "PUT", body });
    await loadSettings();
    toast("系统设置已保存");
  } catch (error) { showError(error, "设置保存失败"); }
}

const dataActionHandlers = {
  "go-view": { run: (action) => switchView(action.dataset.view), error: "页面打开失败" },
  "show-help": { run: () => toast("帮助中心", "请联系系统管理员处理账号、权限或业务数据问题。", "success"), error: "帮助信息打开失败" },
  "show-notifications": { run: () => toast("暂无新通知", "待办事项会直接显示在数据概览中。", "success"), error: "通知打开失败" },
  "show-activity-help": { run: () => toast("最近操作", "这里展示最新 8 条关键操作，便于快速核对责任人与时间。", "success"), error: "操作说明打开失败" },
  "show-activity-detail": { run: (action) => toast(action.dataset.eventLabel || "操作详情", action.dataset.eventDescription || "系统数据已更新", "success"), error: "操作详情打开失败" },
  "open-product-form": { run: () => $("#open-product-modal")?.click(), error: "打开产品表单失败" },
  "open-supplier-form": { run: () => $("#open-supplier-modal")?.click(), error: "打开供应商表单失败" },
  "open-product": { run: (action) => openProductDetail(Number(action.dataset.id)), error: "产品详情加载失败" },
  "edit-product-inline": { run: (action) => openProductEditFromList(Number(action.dataset.id)), error: "产品编辑打开失败" },
  "open-related-product": { run: (action) => openRelatedProduct(Number(action.dataset.id)), error: "关联产品加载失败" },
  "open-product-batch": { run: (action) => openProductBatch(Number(action.dataset.id)), error: "批次二维码加载失败" },
  "generate-codes": { run: (action) => openCodeModal(Number(action.dataset.id)), error: "二维码生成页面打开失败" },
  "print-code-set": { run: (action) => printCodeSet(Number(action.dataset.id)), error: "打印页面打开失败" },
  "open-supplier": { run: async (action) => { if (state.view !== "suppliers") await switchView("suppliers"); await openSupplierDetail(Number(action.dataset.id)); }, error: "供应商详情加载失败" },
  "edit-part": { run: (action) => openPartModal(Number(action.dataset.id)), error: "部件编辑页面打开失败" },
  "add-part-batch": { run: (action) => openInventoryBatchModal(null, Number(action.dataset.id)), error: "批次登记页面打开失败" },
  "edit-inventory-batch": { run: (action) => openInventoryBatchModal(Number(action.dataset.id)), error: "批次编辑页面打开失败" },
  "open-inventory-movements": { run: (action) => openInventoryMovements(Number(action.dataset.id)), error: "库存流水加载失败" },
  "edit-user": { run: (action) => openUserModal(Number(action.dataset.id)), error: "账号编辑页面打开失败" },
  "toggle-user": { run: (action) => toggleUser(Number(action.dataset.id)), error: "账号状态更新失败" },
  "open-trace-product": { run: (action) => openTraceProduct(Number(action.dataset.id)), error: "生产记录加载失败" },
  "open-batch-gen-detail": { run: (action) => openBatchGenDetail(Number(action.dataset.id)), error: "批次详情加载失败" },
  "open-batch-quality": { run: (action) => openBatchQualityModal(Number(action.dataset.id)), error: "质量处理页面打开失败" },
  "open-genealogy": { run: (action) => openGenealogy(action.dataset.code), error: "产品族谱加载失败" },
  "open-batch-genealogy": { run: (action) => openBatchGenealogy(Number(action.dataset.id)), error: "批次族谱加载失败" },
  "open-record-quality": { run: (action) => openRecordQuality(Number(action.dataset.id)), error: "质量处理页面打开失败" },
  "edit-record": { run: (action) => openRecordEdit(Number(action.dataset.id)), error: "记录修改页面打开失败" },
  "delete-record": { run: (action) => openRecordDelete(Number(action.dataset.id)), error: "记录删除页面打开失败" },
  "push-purchase-order": { run: (action) => pushPurchaseOrder(Number(action.dataset.id)), error: "采购订单推送失败" },
  "edit-purchase-order": { run: (action) => editPurchaseOrder(Number(action.dataset.id)), error: "采购订单编辑打开失败" },
  "copy-purchase-order": { run: (action) => copyPurchaseOrder(Number(action.dataset.id)), error: "采购订单复制失败" },
  "delete-purchase-order": { run: (action) => deletePurchaseOrder(Number(action.dataset.id)), error: "采购订单删除失败" },
  "cancel-po-edit": { run: () => cancelPurchaseOrderEdit(), error: "取消编辑失败" },
  "push-inbound-receipt": { run: (action) => pushInboundReceipt(Number(action.dataset.id)), error: "供应收货推送失败" },
  "view-production-qr": { run: (action) => openProductionQr(Number(action.dataset.id)), error: "生产二维码打开失败" },
  "generate-production-orders": { run: () => generateProductionOrders(false), error: "生产订单生成失败" },
  "generate-production-orders-external": { run: () => generateProductionOrders(true), error: "外采生产订单生成失败" },
};

async function dispatchDataAction(action) {
  const handler = dataActionHandlers[action.dataset.action];
  if (!handler) {
    toast("该操作暂不可用", `未找到 ${action.dataset.action} 的处理逻辑`, "error");
    return;
  }
  if (action.disabled || action.getAttribute("aria-busy") === "true") return;
  const canDisable = action instanceof HTMLButtonElement;
  if (canDisable) action.disabled = true;
  action.setAttribute("aria-busy", "true");
  try {
    await handler.run(action);
  } catch (error) {
    showError(error, handler.error);
  } finally {
    action.removeAttribute("aria-busy");
    if (canDisable && document.contains(action)) action.disabled = false;
  }
}

function bindEvents() {
  $("#login-form").addEventListener("submit", submitLogin);
  $("#password-form").addEventListener("submit", submitPassword);
  $("#password-close").addEventListener("click", () => closeModal("password-modal"));
  $("#change-password-button").addEventListener("click", () => { setAccountMenuOpen(false); showPasswordModal(false); });
  $("#logout-button").addEventListener("click", logout);
  $("#account-button").addEventListener("click", () => setAccountMenuOpen($("#account-popover").classList.contains("hidden")));
  $("#sidebar-toggle")?.addEventListener("click", toggleSidebar);
  $("#sidebar-close")?.addEventListener("click", () => { if (isNarrowViewport()) setSidebarOpen(false); else setNavExpanded(false); });
  $("#sidebar-overlay")?.addEventListener("click", () => setSidebarOpen(false));
  $$(".nav-button").forEach((button) => button.addEventListener("click", () => switchView(button.dataset.view)));
  $$('[data-go]').forEach((button) => button.addEventListener("click", () => switchView(button.dataset.go)));
  $$('[data-close]').forEach((button) => button.addEventListener("click", () => closeModal(button.dataset.close)));
  // Clicking the blank backdrop area no longer closes modals; use the modal's own close/cancel button instead.
  $("#open-product-modal").addEventListener("click", () => { resetProductForm(); openModal("product-modal"); setTimeout(() => $("#product-form").elements.name.focus(), 30); });
  $("#production-qr-print-btn")?.addEventListener("click", printProductionQr);
  $("#product-search").addEventListener("input", debounce(renderProducts));
  $("#product-stock-filter").addEventListener("change", renderProducts);
  $("#product-back").addEventListener("click", showProductList);
  $("#edit-product").addEventListener("click", () => { const product = state.products.find((item) => item.id === state.productDetailId); resetProductForm(product); openModal("product-modal"); });
  $("#delete-product").addEventListener("click", async () => {
    const product = state.products.find((item) => item.id === state.productDetailId);
    if (!product) return;
    if (!window.confirm(`确定删除产品「${product.name}」吗？此操作不可恢复。\n仅未生成二维码、无生产记录的产品可删除。`)) return;
    try {
      await api(`/api/product-models/${state.productDetailId}`, { method: "DELETE" });
      toast("已删除", `产品「${product.name}」已删除`, "success");
      await loadProducts();
      showProductList();
    } catch (error) {
      showError(error, "产品删除失败");
    }
  });
  $("#generate-product-codes").addEventListener("click", () => openCodeModal(state.productDetailId));
  $("#batch-back").addEventListener("click", () => openProductDetail(state.productDetailId).catch((error) => showError(error, "产品详情加载失败")));
  $("#batch-prev").addEventListener("click", () => openProductBatch(state.generationBatchId, state.generationBatchPage - 1).catch((error) => showError(error, "批次加载失败")));
  $("#batch-next").addEventListener("click", () => openProductBatch(state.generationBatchId, state.generationBatchPage + 1).catch((error) => showError(error, "批次加载失败")));
  $("#add-component").addEventListener("click", () => addComponentRow());
  $("#product-form").addEventListener("submit", submitProduct);
  $("#code-form").addEventListener("submit", submitCodeGeneration);
  $("#code-form").elements.prefix.addEventListener("input", updateCodePreview);
  $("#open-supplier-modal").addEventListener("click", () => openSupplierModal());
  $("#supplier-search").addEventListener("input", debounce(showSupplierList));
  $("#supplier-stock-filter").addEventListener("change", showSupplierList);
  $("#supplier-back").addEventListener("click", showSupplierList);
  $("#edit-supplier").addEventListener("click", () => openSupplierModal(state.supplierDetailId));
  $("#add-supplier-part").addEventListener("click", () => openPartModal());
  $("#add-supplier-batch").addEventListener("click", () => openInventoryBatchModal());
  $("#supplier-form").addEventListener("submit", submitSupplier);
  $("#part-form").addEventListener("submit", submitPart);
  $("#inventory-batch-form").addEventListener("submit", submitInventoryBatch);
  $("#open-user-modal").addEventListener("click", () => openUserModal());
  $("#user-form").addEventListener("submit", submitUser);
  $("#trace-back").addEventListener("click", renderTraceProducts);
  $("#trace-record-search").addEventListener("input", debounce(renderTraceBatches));
  $("#trace-status-filter").addEventListener("change", renderTraceBatches);
  $("#trace-date-from").addEventListener("change", renderTraceBatches);
  $("#trace-date-to").addEventListener("change", renderTraceBatches);
  $("#trace-filter-reset").addEventListener("click", resetTraceFilters);
  $("#dashboard-quality-select-all").addEventListener("change", (event) => setQualitySelection("dashboard", event.currentTarget.checked));
  $("#dashboard-quality-bulk-button").addEventListener("click", () => openBulkQuality("dashboard"));
  $("#record-quality-form").addEventListener("submit", submitRecordQuality);
  $("#record-quality-form").elements.status.addEventListener("change", updateQualityNote);
  $("#genealogy-form").addEventListener("submit", submitGenealogy);
  $("#open-batch-generate-modal").addEventListener("click", openBatchGenerateModal);
  $("#batch-generate-form").addEventListener("submit", submitBatchGenerate);
  $("#batch-gen-search").addEventListener("input", debounce(renderBatchGenList));
  $("#batch-gen-back").addEventListener("click", showBatchGenList);
  $("#batch-entry-form").addEventListener("submit", submitBatchEntry);
  $("#batch-entry-reset").addEventListener("click", resetBatchEntry);
  $("#batch-trace-form").addEventListener("submit", submitBatchTrace);
  $("#batch-quality-form").addEventListener("submit", submitBatchQuality);
  $("#batch-quality-form").elements.status.addEventListener("change", updateBatchQualityNote);
  $("#batch-quality-code-filter").addEventListener("input", debounce(loadBatchQuality));
  $("#batch-quality-from").addEventListener("change", loadBatchQuality);
  $("#batch-quality-to").addEventListener("change", loadBatchQuality);
  $("#batch-quality-reset").addEventListener("click", resetBatchQualityFilters);
  $("#my-record-search").addEventListener("input", debounce(renderMyRecords));
  $("#record-edit-form").addEventListener("submit", submitRecordEdit);
  $("#record-delete-form").addEventListener("submit", submitRecordDelete);
  $("#purchase-order-form").addEventListener("submit", submitPurchaseOrder);
  $("#inbound-receipt-form").addEventListener("submit", submitInboundReceipt);
  // Selecting an order reveals its SKU / 店铺 / 数量 / 金额 detail on the inbound page.
  bindPurchaseOrderSummary("#inbound-receipt-form", "#inbound-receipt-summary");
  $("#pending-po-check-all")?.addEventListener("change", (event) => {
    $$(".pending-po-checkbox").forEach((box) => { box.checked = event.target.checked; });
  });
  $("#scan-gun-lookup-form").addEventListener("submit", submitScanGunLookup);
  $("#scan-gun-inbound-form").addEventListener("submit", submitScanGunInbound);
  $("#inventory-sync-button").addEventListener("click", () => syncInventory(false));
  $("#inventory-sync-selected-button")?.addEventListener("click", () => syncInventory(true));
  $("#inventory-sync-check-all")?.addEventListener("change", (event) => {
    $$(".inventory-sync-checkbox").forEach((box) => { box.checked = event.target.checked; });
  });
  $("#settings-form").addEventListener("submit", submitSettings);
  document.addEventListener("keydown", (event) => {
    if (handleRecordScannerKeydown(event)) return;
    handleBatchEntryKeydown(event);
  }, true);
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      $$(".modal-backdrop:not(.hidden)").forEach((modal) => closeModal(modal.id));
      setSidebarOpen(false);
      setAccountMenuOpen(false);
    }
    const card = event.target.closest?.('[role="button"][data-action]');
    // Don't hijack Enter/Space when focus is on an inner control (e.g. the
    // card's own 编辑 button) — let that control handle its own activation.
    const onInnerControl = event.target !== card
      && event.target.closest?.("button, a[href], input, select, textarea");
    if (card && !onInnerControl && (event.key === "Enter" || event.key === " ")) {
      event.preventDefault();
      card.click();
    }
  });
  document.addEventListener("click", (event) => {
    if (!event.target.closest(".account-menu")) setAccountMenuOpen(false);
    const action = event.target.closest("[data-action]");
    if (!action) return;
    event.preventDefault();
    dispatchDataAction(action);
  });
  document.addEventListener("change", (event) => {
    const checkbox = event.target.closest?.(".quality-record-checkbox");
    if (checkbox) updateQualitySelection(checkbox.dataset.qualityScope);
  });
  window.addEventListener("focus", () => { focusBatchEntry(); focusScanGun(); });
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") { focusBatchEntry(); focusScanGun(); }
  });
}

async function initialize() {
  bindEvents();
  try {
    await api("/api/health");
    const user = await api("/api/auth/me");
    setUser(user);
    if (user.mustChangePassword) showPasswordModal(true);
    else await enterApplication();
  } catch (error) {
    if (error.status !== 401) showLogin("系统服务暂时不可用，请稍后重试");
  }
}

document.addEventListener("DOMContentLoaded", initialize);
