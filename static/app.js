const state = {
  view: "dashboard",
  user: null,
  csrfToken: "",
  settings: { requireQualityRelease: false },
  passwordChangeRequired: false,
  scanBusy: false,
  scanSoundEnabled: localStorage.getItem("pts_scan_sound") !== "0",
  scanSession: null,
  machines: [], suppliers: [], partTypes: [], partLabelBatches: [], partLabels: [], activeBatchLabels: [], tracePlans: [], records: [],
  productFamilies: [], productModels: [], users: [], bluetoothModels: [],
  audit: { items: [], eventTypes: [], total: 0, today: 0, operators: 0, filteredTotal: 0 },
  bomDraft: [],
};

const viewMeta = {
  dashboard: ["数据概览", "生产数据与归档状态"],
  scanner: ["装配扫码台", "先扫描成品码，再扫描页面提示的部件码"],
  machines: ["成品建档", "按产品型号录入唯一 SN 并生成成品二维码"],
  catalog: ["生产资料", "管理产品、BOM、供应链、部件编码与生产账号"],
  users: ["录入员管理", "新增实名账号并分配产品型号与供应商权限"],
  records: ["生产记录", "查询与导出已完成的生产归档数据"],
  audit: ["审计日志", "查询账号安全、生产配置与装配操作记录"],
};

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

function escapeHtml(value) {
  return String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#039;");
}

function formatDate(value) {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return escapeHtml(value);
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit",
  }).format(date);
}

function debounce(fn, delay = 260) {
  let timer;
  return (...args) => { clearTimeout(timer); timer = setTimeout(() => fn(...args), delay); };
}

class ApiRequestError extends Error {
  constructor(message, status) { super(message); this.status = status; }
}

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  const method = String(options.method || "GET").toUpperCase();
  if (["POST", "PUT", "PATCH", "DELETE"].includes(method) && state.csrfToken) {
    headers["X-CSRF-Token"] = state.csrfToken;
  }
  if (options.body && typeof options.body !== "string") {
    headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(options.body);
  }
  let response;
  try { response = await fetch(path, { ...options, headers }); }
  catch (_error) { setServerState(false); throw new Error("无法连接服务，请确认服务器已启动"); }
  const contentType = response.headers.get("content-type") || "";
  const payload = contentType.includes("application/json") ? await response.json() : null;
  if (!response.ok || payload?.ok === false) {
    const error = new ApiRequestError(payload?.message || `请求失败（${response.status}）`, response.status);
    if (response.status === 401 && path !== "/api/auth/login") showLogin();
    if (response.status === 428) showPasswordChange();
    throw error;
  }
  setServerState(true);
  return payload?.data;
}

function setServerState(online) {
  const element = $("#server-state");
  element.classList.toggle("online", online);
  element.classList.toggle("offline", !online);
  $("span", element).textContent = online ? "服务运行正常" : "服务连接失败";
}

function toast(title, message = "", type = "success") {
  const element = document.createElement("div");
  element.className = `toast ${type}`;
  element.innerHTML = `<div><strong>${escapeHtml(title)}</strong>${message ? `<span>${escapeHtml(message)}</span>` : ""}</div>`;
  $("#toast-container").append(element);
  setTimeout(() => element.remove(), 3600);
}

function showError(error, title = "操作失败") { toast(title, error?.message || String(error), "error"); }
function icon(name) { return `<svg aria-hidden="true"><use href="#i-${name}"/></svg>`; }
function emptyRow(columns, message) { return `<tr><td colspan="${columns}" class="empty-row">${escapeHtml(message)}</td></tr>`; }

function isAdmin() { return state.user?.role === "ADMIN"; }

function applyRoleVisibility() {
  const admin = isAdmin();
  $$('[data-admin-only]').forEach((element) => element.classList.toggle("role-hidden", !admin));
  document.body.classList.toggle("operator-mode", !admin);
  $("#catalog-nav-label").textContent = admin ? "生产资料" : "部件录入";
  $("#catalog-tabs").classList.toggle("hidden", !admin);
  if (!admin && state.view === "catalog") selectCatalogTab("labels");
}

function setCurrentUser(user) {
  state.user = user;
  state.csrfToken = user?.csrfToken || state.csrfToken || "";
  const name = user?.displayName || user?.username || "未登录";
  const role = user?.role === "ADMIN" ? "管理员" : "录入员";
  $("#sidebar-user-name").textContent = name;
  $("#sidebar-user-role").textContent = `${role} · ${user?.username || ""}`;
  $("#sidebar-user-avatar").textContent = name.slice(0, 1).toUpperCase();
  $("#topbar-user-name").textContent = name;
  $("#operator-name").value = name;
  $("#label-current-operator").textContent = name;
  $("#part-data-current-operator").textContent = name;
  applyRoleVisibility();
}

function showLogin(message = "") {
  state.user = null; state.csrfToken = "";
  $("#app-shell").classList.add("hidden");
  $("#password-modal").classList.add("hidden");
  $("#login-screen").classList.remove("hidden");
  const error = $("#login-error");
  error.textContent = message; error.classList.toggle("hidden", !message);
  setTimeout(() => $("#login-form").elements.username.focus(), 20);
}

function showPasswordChange(required = true) {
  state.passwordChangeRequired = required;
  $("#password-modal-title").textContent = required ? "首次登录，请修改密码" : "修改登录密码";
  $("#password-modal-description").textContent = required
    ? "修改完成后才能进入生产系统。新密码至少 8 个字符。"
    : "修改后其他已登录会话将失效。新密码至少 8 个字符。";
  $("#password-submit").textContent = required ? "修改密码并进入系统" : "确认修改密码";
  $("#password-close").classList.toggle("hidden", required);
  $("#password-modal").classList.remove("hidden");
  $("#password-form").reset();
  $("#password-error").classList.add("hidden");
  setTimeout(() => $("#password-form").elements.currentPassword.focus(), 20);
}

function closePasswordChange() {
  if (state.passwordChangeRequired) return;
  $("#password-modal").classList.add("hidden");
}

function closeAccountMenu() {
  $("#account-popover").classList.add("hidden");
  $("#account-menu-button").setAttribute("aria-expanded", "false");
}

function toggleAccountMenu() {
  const popover = $("#account-popover");
  const opening = popover.classList.contains("hidden");
  popover.classList.toggle("hidden", !opening);
  $("#account-menu-button").setAttribute("aria-expanded", String(opening));
}

async function enterApplication() {
  $("#login-screen").classList.add("hidden");
  $("#app-shell").classList.remove("hidden");
  setCurrentUser(state.user);
  await loadProductCatalog();
  const initialView = location.hash.slice(1);
  await switchView(viewMeta[initialView] ? initialView : "dashboard");
}

async function submitLogin(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const button = $("button[type='submit']", form);
  const errorElement = $("#login-error");
  button.disabled = true; errorElement.classList.add("hidden");
  try {
    const user = await api("/api/auth/login", { method: "POST", body: Object.fromEntries(new FormData(form)) });
    setCurrentUser(user);
    if (user.mustChangePassword) showPasswordChange(true);
    else await enterApplication();
  } catch (error) {
    errorElement.textContent = error.message; errorElement.classList.remove("hidden");
    form.elements.password.select();
  } finally { button.disabled = false; }
}

async function submitPasswordChange(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const errorElement = $("#password-error");
  if (form.elements.newPassword.value !== form.elements.confirmPassword.value) {
    errorElement.textContent = "两次输入的新密码不一致"; errorElement.classList.remove("hidden"); return;
  }
  try {
    const required = state.passwordChangeRequired;
    const user = await api("/api/auth/change-password", {
      method: "POST",
      body: { currentPassword: form.elements.currentPassword.value, newPassword: form.elements.newPassword.value },
    });
    $("#password-modal").classList.add("hidden");
    setCurrentUser(user);
    if (required) {
      await enterApplication();
      toast("密码修改成功", "已进入聚星同创仓库管理系统");
    } else {
      toast("密码修改成功", "账号安全信息已更新");
    }
  } catch (error) { errorElement.textContent = error.message; errorElement.classList.remove("hidden"); }
}

async function logout() {
  closeAccountMenu();
  try { await api("/api/auth/logout", { method: "POST", body: {} }); }
  catch (_error) { /* 无论服务端会话是否存在，都清理本地登录界面。 */ }
  showLogin("已安全退出登录");
}

function getStationId() {
  let stationId = localStorage.getItem("pts_station_id");
  if (!stationId) {
    const random = globalThis.crypto?.randomUUID?.().replaceAll("-", "") || `${Date.now()}${Math.random().toString(16).slice(2)}`;
    stationId = `ST-${random}`;
    localStorage.setItem("pts_station_id", stationId);
  }
  return stationId;
}

function stationDetails() {
  return { stationId: getStationId(), stationName: $("#station-name").value.trim() };
}

function saveStationDetails() {
  const name = $("#station-name").value.trim() || "自动装配工位";
  localStorage.setItem("pts_station_name", name);
}

async function switchView(view) {
  if (!viewMeta[view]) return;
  if (["audit", "users", "dashboard"].includes(view) && !isAdmin()) view = "machines";
  const contentView = view === "users" ? "catalog" : view;
  state.view = view;
  $$(".nav-item").forEach((button) => button.classList.toggle("active", button.dataset.view === view));
  $$(".view").forEach((section) => section.classList.toggle("active", section.id === `view-${contentView}`));
  const meta = view === "catalog" && !isAdmin()
    ? ["部件录入", "选择已授权供应商和部件，生成或补录唯一标签"]
    : viewMeta[view];
  $("#page-title").textContent = meta[0];
  $("#page-subtitle").textContent = meta[1];
  history.replaceState(null, "", `#${view}`);
  try {
    if (view === "dashboard") await loadDashboard();
    if (view === "scanner") { await loadSettings(); await loadScanSession(); focusScanner(); }
    if (view === "machines") await loadMachines();
    if (["catalog", "users"].includes(view)) await loadCatalog();
    if (view === "users") selectCatalogTab("users");
    if (view === "records") await loadRecords();
    if (view === "audit") await loadAudit();
  } catch (error) { showError(error, "数据加载失败"); }
}

async function loadSettings() {
  state.settings = await api("/api/settings");
  $("#require-quality-release").checked = Boolean(state.settings.requireQualityRelease);
  renderDashboardFlow();
}

async function loadProductCatalog() {
  const requests = [api("/api/product-families"), api("/api/product-models")];
  if (isAdmin()) requests.push(api("/api/users"), api("/api/suppliers"));
  const [families, models, users = [], suppliers = []] = await Promise.all(requests);
  state.productFamilies = families; state.productModels = models; state.users = users;
  if (isAdmin()) state.suppliers = suppliers;
  state.bluetoothModels = models.filter((item) => item.active && item.identitySource === "BLUETOOTH");
  renderProductSelects(); renderProductFamilies(); renderProductModels(); renderUserScopeOptions(); renderUsers();
  $("#open-bluetooth-capture").classList.toggle("hidden", state.bluetoothModels.length === 0);
}

function renderProductSelects() {
  const activeFamilies = state.productFamilies.filter((item) => item.active);
  const activeModels = state.productModels.filter((item) => item.active);
  const familyOptions = (isAdmin() ? state.productFamilies : activeFamilies).map((item) =>
    `<option value="${item.id}">${escapeHtml(item.productCode)} · ${escapeHtml(item.name)}</option>`).join("");
  const modelOptions = activeModels.map((item) =>
    `<option value="${item.id}">${escapeHtml(item.productFamilyName)} · ${escapeHtml(item.modelCode)} · ${escapeHtml(item.name)}</option>`).join("");
  $("#model-product-family").innerHTML = '<option value="">请选择产品分类</option>' + familyOptions;
  $("#machine-product-model").innerHTML = `<option value="">${!isAdmin() && !activeModels.length ? "管理员尚未授权可录入产品" : "请选择产品型号"}</option>` + modelOptions;
  $("#bom-product-model").innerHTML = '<option value="">请选择产品型号</option>' + modelOptions;
}

function renderProductFamilies() {
  $("#product-family-count").textContent = `${state.productFamilies.length} 个分类`;
  $("#product-family-table").innerHTML = state.productFamilies.length ? state.productFamilies.map((item) => `
    <tr><td><span class="cell-primary">${escapeHtml(item.productCode)}</span></td>
    <td><span class="cell-primary">${escapeHtml(item.name)}</span></td>
    <td>${escapeHtml(item.description || "-")}</td><td>${item.modelCount} 个型号</td>
    <td class="right"><div class="row-actions"><button class="btn tiny ghost" type="button" data-family-edit="${item.id}">编辑</button>
    <button class="status-toggle ${item.active ? "active" : ""}" type="button" data-family-toggle="${item.id}">${item.active ? "已启用" : "已停用"}</button></div></td></tr>`).join("")
    : emptyRow(5, "尚未配置产品分类");
  $$('[data-family-edit]').forEach((button) => button.addEventListener("click", () => editProductFamily(Number(button.dataset.familyEdit))));
  $$('[data-family-toggle]').forEach((button) => button.addEventListener("click", () => toggleProductFamily(Number(button.dataset.familyToggle))));
}

function renderProductModels() {
  $("#product-model-count").textContent = `${state.productModels.length} 个型号`;
  $("#product-model-table").innerHTML = state.productModels.length ? state.productModels.map((item) => `
    <tr><td><span class="cell-primary">${escapeHtml(item.productFamilyName)}</span><span class="cell-secondary">${escapeHtml(item.productFamilyCode)}</span></td>
    <td><span class="cell-primary">${escapeHtml(item.modelCode)}</span><span class="cell-secondary">${escapeHtml(item.name)} · SN ${escapeHtml(item.serialPrefix || "不限前缀")}</span></td>
    <td><span class="status ${item.identitySource === "BLUETOOTH" ? "done" : "available"}">${item.identitySource === "BLUETOOTH" ? "蓝牙读取" : item.identitySource === "SCANNER" ? "扫码枪" : "手工录入"}</span></td>
    <td>${item.identitySource === "BLUETOOTH" ? `<span class="cell-primary">${escapeHtml(item.bluetoothNamePrefix)}</span><span class="cell-secondary">${escapeHtml(item.bluetoothNotifyUuid || "未配置通知 UUID")}</span>` : '<span class="cell-secondary">不使用蓝牙</span>'}</td>
    <td><span class="cell-primary">${item.finishedProductCount} 件成品</span><span class="cell-secondary">${item.tracePlanCount} 个 BOM 版本</span></td>
    <td class="right"><div class="row-actions"><button class="btn tiny ghost" type="button" data-model-edit="${item.id}">编辑</button><button class="status-toggle ${item.active ? "active" : ""}" type="button" data-model-toggle="${item.id}" aria-label="切换型号状态">${item.active ? "已启用" : "已停用"}</button></div></td></tr>`).join("")
    : emptyRow(6, "尚未配置产品型号");
  $$('[data-model-edit]').forEach((button) => button.addEventListener("click", () => editProductModel(Number(button.dataset.modelEdit))));
  $$('[data-model-toggle]').forEach((button) => button.addEventListener("click", () => toggleProductModel(Number(button.dataset.modelToggle))));
}

function renderUsers() {
  $("#user-count").textContent = `${state.users.length} 个账号`;
  $("#user-table").innerHTML = state.users.length ? state.users.map((user) => `
    <tr><td><span class="cell-primary">${escapeHtml(user.username)}</span></td><td>${escapeHtml(user.displayName)}</td>
    <td><span class="status ${user.role === "ADMIN" ? "done" : "available"}">${user.role === "ADMIN" ? "管理员" : "录入员"}</span></td>
    <td>${user.role === "ADMIN" ? '<span class="cell-primary">全部产品与供应商</span><span class="cell-secondary">系统管理权限</span>' : `<span class="cell-primary">${user.productModels?.length || 0} 个产品型号</span><span class="cell-secondary">${user.suppliers?.length ? user.suppliers.map((item) => escapeHtml(item.name)).join("、") : "未分配供应商"}</span>`}</td>
    <td><span class="status ${user.active ? "done" : "used"}">${user.active ? "已启用" : "已停用"}</span></td>
    <td>${formatDate(user.lastLoginAt)}</td><td>${formatDate(user.createdAt)}</td>
    <td class="right"><div class="row-actions"><button class="btn tiny ghost" type="button" data-user-edit="${user.id}">编辑</button><button class="status-toggle ${user.active ? "active" : ""}" type="button" data-user-toggle="${user.id}" ${user.id === state.user?.id ? "disabled" : ""}>${user.active ? "停用" : "启用"}</button></div></td></tr>`).join("")
    : emptyRow(8, "暂无账号");
  $$('[data-user-edit]').forEach((button) => button.addEventListener("click", () => editUser(Number(button.dataset.userEdit))));
  $$('[data-user-toggle]').forEach((button) => button.addEventListener("click", () => toggleUser(Number(button.dataset.userToggle))));
}

function renderUserScopeOptions() {
  if (!isAdmin()) return;
  const products = state.productModels.filter((item) => item.active);
  $("#user-product-scope").innerHTML = products.length ? products.map((item) => `
    <label class="scope-option"><input type="checkbox" data-user-product-id="${item.id}"><span><strong>${escapeHtml(item.modelCode)}</strong><small>${escapeHtml(item.name)}</small></span></label>`).join("")
    : '<div class="empty-mini">暂无可授权的产品型号</div>';
  $("#user-supplier-scope").innerHTML = state.suppliers.length ? state.suppliers.map((item) => `
    <label class="scope-option"><input type="checkbox" data-user-supplier-id="${item.id}"><span><strong>${escapeHtml(item.supplierCode)}</strong><small>${escapeHtml(item.name)}</small></span></label>`).join("")
    : '<div class="empty-mini">暂无可授权的供应商</div>';
  updateUserScopeState();
}

function updateUserScopeState() {
  if (!isAdmin()) return;
  const adminRole = $("#user-role").value === "ADMIN";
  $("#user-scope-fields").classList.toggle("disabled", adminRole);
  $$("#user-scope-fields input").forEach((input) => { input.disabled = adminRole; });
  $$("#user-scope-fields button").forEach((button) => { button.disabled = adminRole; });
}

async function createProductFamily(event) {
  event.preventDefault(); const form = event.currentTarget;
  try {
    const payload = Object.fromEntries(new FormData(form));
    const familyId = Number(payload.familyId || 0); delete payload.familyId;
    if (familyId) delete payload.productCode;
    const item = await api(familyId ? `/api/product-families/${familyId}` : "/api/product-families", { method: familyId ? "PUT" : "POST", body: payload });
    resetProductFamilyForm(); await loadProductCatalog(); toast(familyId ? "产品分类已更新" : "产品分类已创建", `${item.productCode} · ${item.name}`);
  } catch (error) { showError(error, "保存产品分类失败"); }
}

async function createProductModel(event) {
  event.preventDefault(); const form = event.currentTarget;
  try {
    const payload = Object.fromEntries(new FormData(form));
    const modelId = Number(payload.modelId || 0); delete payload.modelId;
    if (modelId) delete payload.modelCode;
    const item = await api(modelId ? `/api/product-models/${modelId}` : "/api/product-models", { method: modelId ? "PUT" : "POST", body: payload });
    resetProductModelForm(); await loadProductCatalog(); toast(modelId ? "产品型号已更新" : "产品型号已创建", `${item.modelCode} · ${item.name}`);
  } catch (error) { showError(error, "保存产品型号失败"); }
}

function editProductFamily(id) {
  const item = state.productFamilies.find((family) => family.id === id); if (!item) return;
  const form = $("#product-family-form"); form.elements.familyId.value = item.id;
  form.elements.productCode.value = item.productCode; form.elements.productCode.readOnly = true;
  form.elements.name.value = item.name; form.elements.description.value = item.description || "";
  $("#product-family-form-title").textContent = `编辑产品分类 · ${item.productCode}`;
  $("#product-family-submit").textContent = "保存修改"; $("#cancel-product-family-edit").classList.remove("hidden");
  form.scrollIntoView({ behavior: "smooth", block: "center" });
}

function resetProductFamilyForm() {
  const form = $("#product-family-form"); form.reset(); form.elements.familyId.value = ""; form.elements.productCode.readOnly = false;
  $("#product-family-form-title").textContent = "新增产品分类"; $("#product-family-submit").textContent = "保存产品分类";
  $("#cancel-product-family-edit").classList.add("hidden");
}

async function toggleProductFamily(id) {
  const item = state.productFamilies.find((family) => family.id === id); if (!item) return;
  try {
    await api(`/api/product-families/${id}`, { method: "PUT", body: { active: !item.active } });
    await loadProductCatalog(); toast("产品分类状态已更新", `${item.name} 已${item.active ? "停用" : "启用"}`);
  } catch (error) { showError(error, "更新产品分类失败"); }
}

function editProductModel(id) {
  const item = state.productModels.find((model) => model.id === id); if (!item) return;
  const form = $("#product-model-form"); form.elements.modelId.value = item.id;
  form.elements.productFamilyId.value = item.productFamilyId; form.elements.modelCode.value = item.modelCode;
  form.elements.modelCode.readOnly = true; form.elements.name.value = item.name; form.elements.serialPrefix.value = item.serialPrefix || "";
  form.elements.identitySource.value = item.identitySource; form.elements.bluetoothNamePrefix.value = item.bluetoothNamePrefix || "";
  form.elements.bluetoothServiceUuid.value = item.bluetoothServiceUuid || ""; form.elements.bluetoothNotifyUuid.value = item.bluetoothNotifyUuid || "";
  $("#product-model-form-title").textContent = `编辑产品型号 · ${item.modelCode}`;
  $("#product-model-submit").textContent = "保存修改"; $("#cancel-product-model-edit").classList.remove("hidden");
  updateBluetoothFieldState(); form.scrollIntoView({ behavior: "smooth", block: "center" });
}

function resetProductModelForm() {
  const form = $("#product-model-form"); form.reset(); form.elements.modelId.value = ""; form.elements.modelCode.readOnly = false;
  $("#product-model-form-title").textContent = "新增产品型号"; $("#product-model-submit").textContent = "保存产品型号";
  $("#cancel-product-model-edit").classList.add("hidden"); updateBluetoothFieldState();
}

function updateBluetoothFieldState() {
  const form = $("#product-model-form");
  const enabled = form.elements.identitySource.value === "BLUETOOTH";
  $("#model-bluetooth-fields").classList.toggle("hidden", !enabled);
  $$("#model-bluetooth-fields input").forEach((input) => { input.disabled = !enabled; });
  form.elements.bluetoothNotifyUuid.required = enabled;
}

async function toggleProductModel(id) {
  const item = state.productModels.find((model) => model.id === id); if (!item) return;
  try {
    await api(`/api/product-models/${id}`, { method: "PUT", body: { active: !item.active } });
    await loadProductCatalog(); toast("产品型号状态已更新", `${item.modelCode} 已${item.active ? "停用" : "启用"}`);
  } catch (error) { showError(error, "更新型号失败"); }
}

async function createUser(event) {
  event.preventDefault(); const form = event.currentTarget;
  try {
    const payload = Object.fromEntries(new FormData(form));
    const userId = Number(payload.userId || 0); delete payload.userId;
    payload.productModelIds = $$('[data-user-product-id]:checked').map((input) => Number(input.dataset.userProductId));
    payload.supplierIds = $$('[data-user-supplier-id]:checked').map((input) => Number(input.dataset.userSupplierId));
    if (userId) { delete payload.username; if (!payload.password) delete payload.password; }
    const user = await api(userId ? `/api/users/${userId}` : "/api/users", { method: userId ? "PUT" : "POST", body: payload });
    resetUserForm(); await loadProductCatalog(); toast(userId ? "账号已更新" : "生产账号已创建", `${user.displayName} · ${user.role === "ADMIN" ? "管理员" : "录入员"}`);
  } catch (error) { showError(error, "保存账号失败"); }
}

function editUser(id) {
  const user = state.users.find((item) => item.id === id); if (!user) return;
  const form = $("#user-form"); form.elements.userId.value = user.id; form.elements.username.value = user.username;
  form.elements.username.readOnly = true; form.elements.displayName.value = user.displayName; form.elements.password.value = "";
  form.elements.password.required = false; form.elements.role.value = user.role;
  const productIds = new Set(user.productModelIds || []); const supplierIds = new Set(user.supplierIds || []);
  $$('[data-user-product-id]').forEach((input) => { input.checked = productIds.has(Number(input.dataset.userProductId)); });
  $$('[data-user-supplier-id]').forEach((input) => { input.checked = supplierIds.has(Number(input.dataset.userSupplierId)); });
  updateUserScopeState();
  $("#user-form-title").textContent = `编辑账号 · ${user.username}`; $("#user-password-label").textContent = "重置密码（可选）";
  $("#user-submit").textContent = "保存修改"; $("#cancel-user-edit").classList.remove("hidden");
  form.scrollIntoView({ behavior: "smooth", block: "center" });
}

function resetUserForm() {
  const form = $("#user-form"); form.reset(); form.elements.userId.value = ""; form.elements.username.readOnly = false;
  form.elements.password.required = true; $("#user-form-title").textContent = "新增录入员";
  $("#user-password-label").textContent = "初始密码"; $("#user-submit").textContent = "创建账号";
  $("#cancel-user-edit").classList.add("hidden");
  $$('[data-user-product-id], [data-user-supplier-id]').forEach((input) => { input.checked = false; });
  $$('[data-scope-all]').forEach((button) => { button.textContent = "全选"; });
  updateUserScopeState();
}

async function toggleUser(id) {
  const user = state.users.find((item) => item.id === id); if (!user) return;
  try {
    await api(`/api/users/${id}`, { method: "PUT", body: { active: !user.active } });
    await loadProductCatalog(); toast("账号状态已更新", `${user.displayName} 已${user.active ? "停用" : "启用"}`);
  } catch (error) { showError(error, "更新账号失败"); }
}

function renderDashboardFlow() {
  const nodes = [
    { label: "成品建档", detail: "选择型号，生成并贴好成品码" },
    { label: "装配扫码", detail: "成品码 → BOM 提示的部件码" },
    { label: "自动归档", detail: "扫完即完成" },
  ];
  $("#dashboard-flow").innerHTML = nodes.map((node, index) => `
    <div class="flow-node"><div class="flow-dot">${index === nodes.length - 1 ? icon("check") : index + 1}</div>
    <div class="flow-copy"><strong>${node.label}</strong><span>${node.detail}</span></div>
    ${index < nodes.length - 1 ? '<div class="flow-line"></div>' : ""}</div>`).join("");
}

async function loadDashboard() {
  const [dashboard] = await Promise.all([api("/api/dashboard"), loadSettings()]);
  $("#stat-machines").textContent = dashboard.counts.machines;
  $("#stat-plans").textContent = dashboard.counts.activeTracePlans;
  $("#stat-labels").textContent = dashboard.counts.partLabels;
  $("#stat-records").textContent = dashboard.counts.traceRecords;
  $("#stat-today").textContent = dashboard.counts.todayRecords;
  $("#stat-stations").textContent = dashboard.counts.activeStations;
  $("#recent-records").innerHTML = dashboard.recentRecords.length ? dashboard.recentRecords.map((record) => `
    <div class="compact-record"><div class="mini-icon">${icon("machine")}</div><div><strong>${escapeHtml(record.machine.sn)}</strong>
    <span>${escapeHtml(record.machine.model)} · ${record.parts.length} 个部件</span></div><time>${formatDate(record.completedAt)}</time></div>`).join("")
    : '<div class="empty-mini">暂无归档记录</div>';
}

async function loadScanSession() {
  state.scanSession = await api(`/api/scan/session?stationId=${encodeURIComponent(getStationId())}`);
  renderScanSession();
}

// Fixed fallback for the legacy generic (non-BOM) per-unit scan flow. The
// server enforces the authoritative count; this is only the placeholder used
// before the first scan session is loaded.
const DEFAULT_GENERIC_PART_COUNT = 2;

function renderScanSession() {
  const session = state.scanSession || { requiredPartCount: DEFAULT_GENERIC_PART_COUNT, currentStep: 0, machine: null, parts: [], nextExpected: "machine" };
  $("#scan-completion").classList.add("hidden");
  $("#undo-last-scan").disabled = !session.machine || session.parts.length === 0 || state.scanBusy;
  const workflowBadge = $("#scan-workflow-badge");
  const isBom = session.workflowMode === "BOM";
  workflowBadge.className = `workflow-badge ${isBom ? "bom" : "generic"}`;
  workflowBadge.textContent = isBom
    ? `BOM ${session.tracePlan.name} · ${session.tracePlan.version}`
    : "通用兼容流程";
  const steps = [{ label: "成品", detail: session.machine?.sn || "等待扫描" }];
  for (let position = 1; position <= session.requiredPartCount; position += 1) {
    const part = session.parts.find((item) => item.position === position);
    const slot = session.expectedSlots?.find((item) => item.position === position);
    steps.push({
      label: slot?.slotName || `部件 ${position}`,
      detail: part ? `${part.categoryName} · ${part.supplierName}` : slot ? `${slot.categoryCode} · ${slot.categoryName}` : "等待扫描",
    });
  }
  $("#scan-progress").innerHTML = steps.map((step, index) => {
    const done = index < session.currentStep;
    const current = index === session.currentStep;
    return `<div class="scan-step ${done ? "done" : ""} ${current ? "current" : ""}"><div class="scan-step-marker">${done ? icon("check") : index + 1}</div>
      <div class="scan-step-copy"><strong>${step.label}</strong><span title="${escapeHtml(step.detail)}">${escapeHtml(step.detail)}</span></div>
      ${index < steps.length - 1 ? '<div class="scan-step-line"></div>' : ""}</div>`;
  }).join("");
  if (session.nextExpected === "machine") {
    $("#scan-counter").textContent = "等待成品";
    $("#scan-instruction").textContent = "请扫描成品二维码";
    $("#scan-hint").textContent = `随后连续扫描 ${session.requiredPartCount} 个部件二维码，最后一步自动归档。`;
    $("#scan-code").placeholder = "等待成品二维码…";
  } else {
    const next = session.parts.length + 1;
    const slot = session.nextExpectedSlot;
    $("#scan-counter").textContent = `第 ${next} 项 / 共 ${session.requiredPartCount} 项`;
    $("#scan-instruction").textContent = slot ? `请扫描：${slot.slotName}` : `请扫描第 ${next} 个部件二维码`;
    const qualityHint = state.settings.requireQualityRelease ? "仅接受已检验合格部件。" : "";
    $("#scan-hint").textContent = slot
      ? `此位置接受“${slot.categoryName}”分类下${isAdmin() ? "任一已登记供应商" : "本账号已授权供应商"}的部件。${qualityHint} 当前成品：${session.machine?.sn || "-"}。`
      : `当前成品：${session.machine?.sn || "-"}，已完成 ${session.parts.length}/${session.requiredPartCount} 个部件。${qualityHint}`;
    $("#scan-code").placeholder = `等待部件 ${next} 二维码…`;
  }
}

function scanModalOpen() {
  return ["#scan-undo-modal", "#password-modal", "#label-modal", "#part-data-modal", "#bluetooth-modal", "#genealogy-modal", "#batch-detail-modal"]
    .some((selector) => !$(selector).classList.contains("hidden"));
}

function updateScannerFocusState() {
  const focused = document.activeElement === $("#scan-code") && !state.scanBusy;
  const element = $("#scanner-focus-state");
  element.textContent = state.scanBusy ? "正在校验，请稍候" : focused ? "扫码输入已就绪" : "自动接收已启用";
  element.classList.toggle("ready", focused && !state.scanBusy);
}

function focusScanner() {
  const applyFocus = () => {
    if (state.view !== "scanner" || state.scanBusy || scanModalOpen()) return;
    const input = $("#scan-code");
    input.focus({ preventScroll: true });
    const end = input.value.length;
    input.setSelectionRange?.(end, end);
    updateScannerFocusState();
  };
  applyFocus();
  requestAnimationFrame(applyFocus);
  setTimeout(applyFocus, 60);
  setTimeout(applyFocus, 180);
}

function handleScannerKeydown(event) {
  if (state.view !== "scanner" || scanModalOpen() || event.isComposing || event.ctrlKey || event.altKey || event.metaKey) return;
  const input = $("#scan-code");
  const target = event.target;
  const editingAnotherField = target !== input && target.matches?.("input, textarea, select, [contenteditable='true']");
  if (editingAnotherField) return;

  if (event.key === "Enter") {
    if (target !== input && target.matches?.("button, a")) return;
    event.preventDefault();
    input.focus({ preventScroll: true });
    if (state.scanBusy) {
      setScanFeedback("warning", "上一条仍在校验", "请等待当前结果返回后再扫描下一张标签。");
      return;
    }
    if (input.value.trim()) $("#scan-form").requestSubmit();
    return;
  }

  if (event.key.length === 1 && target !== input) {
    event.preventDefault();
    if (state.scanBusy) {
      setScanFeedback("warning", "扫码过快", "上一条仍在校验，本次输入未接收，请稍后重新扫描。");
      return;
    }
    input.focus({ preventScroll: true });
    input.value += event.key;
  }
}

function updateScannerClock() {
  $("#scanner-clock").textContent = new Intl.DateTimeFormat("zh-CN", {
    hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
  }).format(new Date());
}

function setScanFeedback(type, title, message) {
  const element = $("#scan-feedback");
  element.className = `scan-feedback ${type}`;
  element.innerHTML = `${icon(type === "success" ? "check" : type === "error" ? "close" : type === "warning" ? "clock" : "scan")}<div><strong>${escapeHtml(title)}</strong><span>${escapeHtml(message)}</span></div>`;
}

function beep(type) {
  if (!state.scanSoundEnabled) return;
  try {
    const Context = window.AudioContext || window.webkitAudioContext;
    if (!Context) return;
    const context = new Context(); const oscillator = context.createOscillator(); const gain = context.createGain();
    const success = type === "success"; const warning = type === "warning";
    oscillator.frequency.value = success ? 880 : warning ? 440 : 220;
    const duration = success ? .1 : warning ? .14 : .2;
    gain.gain.setValueAtTime(.045, context.currentTime);
    gain.gain.exponentialRampToValueAtTime(.001, context.currentTime + duration);
    oscillator.connect(gain).connect(context.destination); oscillator.start(); oscillator.stop(context.currentTime + duration);
  } catch (_error) { /* 提示音不是核心流程 */ }
}

function scanErrorGuidance(message) {
  if (message.includes("BOM 槽位")) return `${message}；请核对现场物料与页面显示的下一项部件分类。`;
  if (message.includes("其他成品")) return `${message}；请更换未使用标签，或到生产记录核对该部件流向。`;
  if (message.includes("工位")) return `${message}；请到提示工位完成或取消原流程后再试。`;
  if (message.includes("已完成装配")) return `${message}；如需更换部件，应进入后续返工流程，不能覆盖历史装配。`;
  return message;
}

async function submitScan(event) {
  event.preventDefault(); const input = $("#scan-code"); const code = input.value.trim();
  if (!code || state.scanBusy) return focusScanner();
  saveStationDetails(); state.scanBusy = true; input.readOnly = true; updateScannerFocusState();
  $("#undo-last-scan").disabled = true;
  try {
    const result = await api("/api/scan", { method: "POST", body: { ...stationDetails(), code } });
    state.scanSession = result.session; input.value = ""; renderScanSession(); beep("success");
    if (result.completed) {
      setScanFeedback("success", "归档完成", `${result.record.machine.sn} 已关联 ${result.record.parts.length} 个部件，记录单号 ${result.record.traceNo}`);
      $("#scan-completion-title").textContent = result.record.machine.sn;
      $("#scan-completion-detail").textContent = `${result.record.parts.length} 个部件 · 记录单号 ${result.record.traceNo}`;
      $("#scan-completion").classList.remove("hidden");
      toast("生产记录已生成", `${result.record.machine.sn} · ${result.record.traceNo}`);
    } else if (result.scannedType === "machine") setScanFeedback("success", "第 1 步完成：成品已识别", `现在只需按页面提示继续扫描部件。成品：${result.session.machine.sn}`);
    else setScanFeedback("success", "部件码识别成功", `已扫描 ${result.session.parts.length}/${result.session.requiredPartCount} 个部件。`);
  } catch (error) {
    const guidance = scanErrorGuidance(error.message);
    beep("error"); setScanFeedback("error", "本次扫码未录入", guidance); showError(error, "扫码失败"); input.select();
  } finally {
    state.scanBusy = false; input.readOnly = false;
    $("#undo-last-scan").disabled = !state.scanSession?.machine || !state.scanSession?.parts?.length;
    focusScanner(); updateScannerFocusState();
  }
}

async function resetScanSession() {
  try {
    state.scanSession = await api("/api/scan/reset", { method: "POST", body: { stationId: getStationId() } });
    renderScanSession(); $("#scan-code").value = ""; setScanFeedback("neutral", "本次流程已清空", "可以重新扫描一件成品。"); focusScanner();
  } catch (error) { showError(error, "清空失败"); }
}

function openScanUndo() {
  if (!state.scanSession?.parts?.length) return;
  $("#scan-undo-form").reset(); $("#scan-undo-error").classList.add("hidden");
  $("#scan-undo-modal").classList.remove("hidden");
  setTimeout(() => $("#scan-undo-form").elements.reason.focus(), 20);
}

function closeScanUndo() { $("#scan-undo-modal").classList.add("hidden"); focusScanner(); }

async function submitScanUndo(event) {
  event.preventDefault(); const form = event.currentTarget; const errorElement = $("#scan-undo-error");
  try {
    const result = await api("/api/scan/undo", { method: "POST", body: { stationId: getStationId(), reason: form.elements.reason.value } });
    state.scanSession = result.session; closeScanUndo(); renderScanSession(); beep("warning");
    setScanFeedback("warning", "已撤销最后一个部件", `${result.undone.partCode} · ${result.undone.partName} 已释放，请重新扫描正确部件。`);
    toast("扫码已撤销", `第 ${result.undone.position} 项已记录撤销原因`);
  } catch (error) { errorElement.textContent = error.message; errorElement.classList.remove("hidden"); }
}

async function loadMachines() {
  if (!state.productModels.length) await loadProductCatalog();
  const search = $("#machine-search").value.trim();
  state.machines = await api(`/api/machines?search=${encodeURIComponent(search)}`);
  $("#machine-count").textContent = `${state.machines.length} 件成品`;
  $("#machine-table").innerHTML = state.machines.length ? state.machines.map((machine) => `
    <tr><td><span class="cell-primary">${escapeHtml(machine.sn)}</span></td><td><span class="cell-primary">${escapeHtml(machine.productFamilyName || "历史产品")} · ${escapeHtml(machine.model)}</span><span class="cell-secondary">${machine.tracePlanId ? `${escapeHtml(machine.tracePlanName)} · ${escapeHtml(machine.tracePlanVersion)}` : "尚未配置 BOM"}</span></td>
      <td>${escapeHtml(machine.productionDate || "-")}</td><td><code>${escapeHtml(machine.identificationCode)}</code></td>
      <td>${machine.traced ? '<span class="status done">已归档</span>' : machine.reservedStation ? `<span class="status pending">${escapeHtml(machine.reservedStation)} 扫码中</span>` : '<span class="status available">待归档</span>'}</td>
      <td>${formatDate(machine.createdAt)}</td><td class="right"><button class="btn secondary small" data-machine-label="${machine.id}">${icon("qr")}查看标签</button></td></tr>`).join("")
    : emptyRow(7, "尚未录入成品，请选择产品型号后新增");
  $$('[data-machine-label]').forEach((button) => button.addEventListener("click", () => openMachineLabel(Number(button.dataset.machineLabel))));
}

async function createMachine(event) {
  event.preventDefault(); const form = event.currentTarget; const data = Object.fromEntries(new FormData(form));
  try {
    const machine = await api("/api/machines", { method: "POST", body: data });
    form.reset(); $("#machine-search").value = ""; $("#machine-form-panel").classList.add("hidden"); await loadMachines(); openMachineLabel(machine.id);
    toast("成品已登记", `${machine.sn} 的唯一二维码已生成`);
  } catch (error) { showError(error, "新增成品失败"); }
}

function setBluetoothStatus(message, type = "") {
  const element = $("#bluetooth-status"); element.textContent = message; element.className = `bluetooth-status ${type}`.trim();
}

async function openBluetoothCapture() {
  $("#bluetooth-modal").classList.remove("hidden"); $("#bluetooth-device-list").innerHTML = "";
  setBluetoothStatus("正在检查服务器电脑的蓝牙采集组件…");
  try {
    const status = await api("/api/bluetooth/status");
    state.bluetoothModels = status.models || [];
    $("#discover-bluetooth").disabled = !status.available;
    const prefixes = state.bluetoothModels.map((item) => item.bluetoothNamePrefix).filter(Boolean);
    $("#bluetooth-modal-description").textContent = prefixes.length
      ? `当前仅扫描已配置前缀：${prefixes.join("、")}。读取时使用各型号自己的通知 UUID。`
      : "尚未配置可使用蓝牙读取的产品型号。";
    setBluetoothStatus(status.available
      ? "准备扫描附近设备。请确保目标设备已开机，且没有被手机或其他电脑连接。"
      : "蓝牙采集组件尚未安装，请在服务器电脑运行 install.bat 后重启服务。", status.available ? "" : "error");
  } catch (error) { $("#discover-bluetooth").disabled = true; setBluetoothStatus(error.message, "error"); }
}

function closeBluetoothCapture() { $("#bluetooth-modal").classList.add("hidden"); }

async function discoverBluetoothDevices() {
  const button = $("#discover-bluetooth"); button.disabled = true;
  const prefixes = state.bluetoothModels.map((item) => item.bluetoothNamePrefix).filter(Boolean);
  setBluetoothStatus(`正在扫描 ${prefixes.join("、") || "已配置"} 设备，约需 6 秒…`);
  $("#bluetooth-device-list").innerHTML = "";
  try {
    const devices = await api("/api/bluetooth/discover", { method: "POST", body: {} });
    if (!devices.length) {
      setBluetoothStatus("未发现符合已配置产品前缀的设备。请确认设备已开机、靠近服务器电脑，并断开其他蓝牙连接。", "error");
      return;
    }
    setBluetoothStatus(`发现 ${devices.length} 台设备。请选择目标成品读取完整 SN。`, "success");
    $("#bluetooth-device-list").innerHTML = devices.map((device, index) => `
      <div class="bluetooth-device"><div class="device-signal">${icon("server")}</div><div><strong>${escapeHtml(device.name)}</strong><span>${escapeHtml(device.address)}</span></div>
      <button class="btn secondary small" data-ble-index="${index}">读取 SN</button></div>`).join("");
    $$('[data-ble-index]').forEach((item) => item.addEventListener("click", () => readBluetoothSn(devices[Number(item.dataset.bleIndex)], item)));
  } catch (error) { setBluetoothStatus(error.message, "error"); showError(error, "蓝牙扫描失败"); }
  finally { button.disabled = false; }
}

async function readBluetoothSn(device, button) {
  button.disabled = true; setBluetoothStatus(`正在连接 ${device.name}，等待设备从已配置的通知特征返回完整 SN…`);
  try {
    const identity = await api("/api/bluetooth/read-sn", { method: "POST", body: device });
    if (identity.alreadyRegistered) {
      setBluetoothStatus(`SN ${identity.sn} 已在系统中登记。`, "success");
      $("#machine-search").value = ""; await loadMachines(); closeBluetoothCapture();
      const machine = state.machines.find((item) => item.id === identity.machineId);
      if (machine) openMachineLabel(machine.id);
      toast("该成品已登记", identity.sn);
      return;
    }
    const form = $("#machine-form");
    form.elements.sn.value = identity.sn;
    form.elements.productModelId.value = identity.productModel.id;
    form.elements.productionDate.value = identity.production_date || "";
    closeBluetoothCapture(); $("#machine-form-panel").classList.remove("hidden");
    setBluetoothStatus(`已读取完整 SN：${identity.sn}`, "success");
    toast("蓝牙 SN 读取成功", "请核对成品信息并保存打印二维码");
    form.elements.sn.focus();
  } catch (error) { setBluetoothStatus(error.message, "error"); showError(error, "读取 SN 失败"); }
  finally { button.disabled = false; }
}

async function loadCatalog() {
  if (!state.productModels.length || (isAdmin() && !state.users.length)) await loadProductCatalog();
  const search = encodeURIComponent($("#label-search").value.trim());
  const [suppliers, partTypes, batches, labels, tracePlans, settings] = await Promise.all([
    api("/api/suppliers"), api("/api/part-types"),
    api(`/api/part-label-batches?search=${search}`),
    api(`/api/part-labels?search=${search}`),
    api("/api/trace-plans"), api("/api/settings"),
  ]);
  state.suppliers = suppliers; state.partTypes = partTypes; state.partLabelBatches = batches; state.partLabels = labels;
  state.tracePlans = tracePlans; state.settings = settings;
  renderSuppliers(); renderPartTypes(); renderPartLabelBatches(); renderPartLabels(); renderTracePlans(); renderCatalogSelects(); renderBomDraft();
}

function renderPartLabelBatches() {
  $("#batch-count").textContent = `${state.partLabelBatches.length} 个批次`;
  $("#batch-table").innerHTML = state.partLabelBatches.length ? state.partLabelBatches.map((batch) => `
    <tr class="clickable-row" data-batch-row="${batch.id}"><td><button class="batch-link" type="button" data-batch-detail="${batch.id}">${escapeHtml(batch.batchCode)}</button></td>
      <td><span class="cell-primary">${escapeHtml(batch.categoryCode)} · ${escapeHtml(batch.categoryName)}</span><span class="cell-secondary">${escapeHtml(batch.supplierCode)} · ${escapeHtml(batch.supplierName)} · ${escapeHtml(batch.partCode)}</span></td>
      <td><span class="cell-primary">生产批次：${escapeHtml(batch.lotNo || "-")}</span><span class="cell-secondary">供应商批次：${escapeHtml(batch.supplierBatchNo || "-")} · 日期：${escapeHtml(batch.productionDate || "-")}</span></td>
      <td><span class="cell-primary">${batch.generatedCount} / ${batch.quantity}</span><span class="cell-secondary">唯一编码</span></td>
      <td>${batch.dataEnteredCount === batch.quantity ? '<span class="status done">已全部录入</span>' : `<span class="status pending">${batch.dataEnteredCount} / ${batch.quantity}</span>`}</td>
      <td><span class="cell-primary">${escapeHtml(batch.generatedBy || "未填写")}</span><span class="cell-secondary">${formatDate(batch.generatedAt)}</span></td>
      <td class="right"><div class="row-actions"><button class="btn secondary small" type="button" data-batch-detail="${batch.id}">${icon("records")}查看批次</button><a class="btn secondary small" href="/api/part-label-batches/${batch.id}/qrcodes.zip">${icon("download")}下载二维码</a></div></td></tr>`).join("")
    : emptyRow(7, "暂无编码批次，请先批量生成部件编码");
  $$('[data-batch-detail]').forEach((button) => button.addEventListener("click", (event) => {
    event.stopPropagation(); openBatchDetail(Number(button.dataset.batchDetail));
  }));
  $$('[data-batch-row]').forEach((row) => row.addEventListener("click", (event) => {
    if (event.target.closest("a, button")) return;
    openBatchDetail(Number(row.dataset.batchRow));
  }));
}

async function openBatchDetail(batchId) {
  $("#batch-detail-modal").classList.remove("hidden");
  $("#batch-detail-title").textContent = "正在加载批次…";
  $("#batch-detail-subtitle").textContent = "";
  $("#batch-detail-summary").innerHTML = "";
  $("#batch-detail-labels").innerHTML = emptyRow(5, "正在读取本批单码");
  try {
    const data = await api(`/api/part-label-batches/${batchId}`);
    const batch = data.batch;
    state.activeBatchLabels = data.labels;
    $("#batch-detail-title").textContent = batch.batchCode;
    $("#batch-detail-subtitle").textContent = `${batch.categoryCode} · ${batch.categoryName} / ${batch.supplierName}`;
    $("#batch-download").href = `/api/part-label-batches/${batch.id}/qrcodes.zip`;
    const summary = [
      ["生成数量", `${batch.generatedCount} 个`],
      ["单码已录入", `${batch.dataEnteredCount} / ${batch.quantity}`],
      ["生产批次", batch.lotNo || "-"],
      ["供应商批次", batch.supplierBatchNo || "-"],
      ["生成操作员", batch.generatedBy || "未填写"],
      ["生成时间", formatDate(batch.generatedAt)],
    ];
    $("#batch-detail-summary").innerHTML = summary.map(([label, value]) => `
      <div><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`).join("");
    $("#batch-detail-labels").innerHTML = data.labels.length ? data.labels.map((label, index) => `
      <tr><td>${index + 1}</td><td><code>${escapeHtml(label.identificationCode)}</code></td>
      <td>${escapeHtml(label.sourceSerialNo || "-")}</td>
      <td>${label.dataEntered ? `<span class="status done">已录入 · ${escapeHtml(label.enteredBy)}</span>` : '<span class="status pending">待录入</span>'}</td>
      <td class="right"><div class="row-actions"><button class="btn secondary small" type="button" data-batch-label-data="${label.id}">${icon("edit")}录入数据</button><button class="btn secondary small" type="button" data-batch-label-view="${label.id}">${icon("qr")}查看二维码</button></div></td></tr>`).join("")
      : emptyRow(5, "本批次没有单码");
    $$('[data-batch-label-data]').forEach((button) => button.addEventListener("click", () => {
      const id = Number(button.dataset.batchLabelData); const labels = state.activeBatchLabels.slice();
      closeBatchDetail(); state.activeBatchLabels = labels; openPartData(id);
    }));
    $$('[data-batch-label-view]').forEach((button) => button.addEventListener("click", () => {
      const id = Number(button.dataset.batchLabelView); const labels = state.activeBatchLabels.slice();
      closeBatchDetail(); state.activeBatchLabels = labels; openPartLabel(id);
    }));
  } catch (error) {
    $("#batch-detail-title").textContent = "批次读取失败";
    $("#batch-detail-labels").innerHTML = emptyRow(5, error.message);
    showError(error, "批次读取失败");
  }
}

function closeBatchDetail() {
  $("#batch-detail-modal").classList.add("hidden");
  state.activeBatchLabels = [];
}

function partCategories() {
  const categories = new Map();
  state.partTypes.filter((item) => item.active).forEach((item) => {
    if (!categories.has(item.categoryCode)) {
      categories.set(item.categoryCode, {
        code: item.categoryCode,
        name: item.categoryName,
        representativeId: item.id,
        suppliers: [],
      });
    }
    const category = categories.get(item.categoryCode);
    if (!category.suppliers.includes(item.supplierName)) category.suppliers.push(item.supplierName);
  });
  return Array.from(categories.values()).sort((a, b) => a.code.localeCompare(b.code));
}

function renderCatalogSelects() {
  const categories = partCategories();
  $("#part-supplier").innerHTML = '<option value="">请选择供应商</option>' + state.suppliers.map((item) =>
    `<option value="${item.id}">${escapeHtml(item.supplierCode)} · ${escapeHtml(item.name)}</option>`).join("");
  $("#label-supplier-filter").innerHTML = `<option value="">${!isAdmin() && !state.suppliers.length ? "管理员尚未授权可录入供应商" : "请选择已授权供应商"}</option>` + state.suppliers.map((item) =>
    `<option value="${item.id}">${escapeHtml(item.supplierCode)} · ${escapeHtml(item.name)}</option>`).join("");
  updateLabelPartTypeOptions();
  $("#bom-slot-part").innerHTML = '<option value="">请选择部件分类</option>' + categories.map((item) =>
    `<option value="${item.representativeId}">${escapeHtml(item.code)} · ${escapeHtml(item.name)}（${item.suppliers.length} 家供应商）</option>`).join("");
  $("#part-category-codes").innerHTML = categories.map((item) => `<option value="${escapeHtml(item.code)}">${escapeHtml(item.name)}</option>`).join("");
  $("#part-category-names").innerHTML = categories.map((item) => `<option value="${escapeHtml(item.name)}">${escapeHtml(item.code)}</option>`).join("");
}

function updateLabelPartTypeOptions() {
  const supplierId = Number($("#label-supplier-filter").value || 0);
  const partTypes = state.partTypes.filter((item) => item.active && item.supplierId === supplierId);
  const select = $("#label-part-type");
  select.disabled = !supplierId;
  select.innerHTML = `<option value="">${supplierId ? "请选择该供应商的部件类型" : "请先选择供应商"}</option>` + partTypes.map((item) =>
    `<option value="${item.id}">${escapeHtml(item.categoryName)} · ${escapeHtml(item.partCode)} · ${escapeHtml(item.name)}</option>`).join("");
}

function renderSuppliers() {
  $("#supplier-count").textContent = `${state.suppliers.length} 家`;
  $("#supplier-table").innerHTML = state.suppliers.length ? state.suppliers.map((item) =>
    `<tr><td><span class="cell-primary">${escapeHtml(item.supplierCode)}</span></td><td>${escapeHtml(item.name)}</td><td>${escapeHtml(item.contact || "-")}</td><td>${escapeHtml(item.phone || "-")}</td><td>${formatDate(item.createdAt)}</td></tr>`).join("")
    : emptyRow(5, "暂无供应商，请先创建供应商");
}

function renderPartTypes() {
  $("#part-type-count").textContent = `${partCategories().length} 个分类 · ${state.partTypes.length} 条供应商物料`;
  $("#part-type-table").innerHTML = state.partTypes.length ? state.partTypes.map((item) =>
    `<tr><td><span class="cell-primary">${escapeHtml(item.categoryCode)} · ${escapeHtml(item.categoryName)}</span></td><td><span class="cell-primary">${escapeHtml(item.partCode)}</span><span class="cell-secondary">${escapeHtml(item.name)}</span></td><td>${escapeHtml(item.specification || "-")}</td>
    <td><span class="cell-primary">${escapeHtml(item.supplierName)}</span><span class="cell-secondary">${escapeHtml(item.supplierCode)}</span></td><td>${formatDate(item.createdAt)}</td></tr>`).join("")
    : emptyRow(5, "暂无供应商物料");
}

function renderTracePlans() {
  $("#trace-plan-count").textContent = `${state.tracePlans.length} 个版本`;
  $("#trace-plan-table").innerHTML = state.tracePlans.length ? state.tracePlans.map((plan) => `
    <tr>
      <td><span class="cell-primary">${escapeHtml(plan.productFamilyName || "历史产品")}</span><span class="cell-secondary">${escapeHtml(plan.modelCode)}</span></td>
      <td><span class="cell-primary">${escapeHtml(plan.name)}</span><span class="cell-secondary">版本 ${escapeHtml(plan.version)}</span></td>
      <td><div class="slot-chip-list">${plan.slots.map((slot) => `<span class="slot-chip"><b>${slot.position}</b>${escapeHtml(slot.slotName)} · ${escapeHtml(slot.categoryName)}</span>`).join("")}</div></td>
      <td>${plan.status === "ACTIVE" ? '<span class="status done">当前生效</span>' : '<span class="status used">历史归档</span>'}</td>
      <td>${plan.usedMachineCount} 件成品</td>
      <td>${formatDate(plan.activatedAt || plan.createdAt)}</td>
    </tr>`).join("") : emptyRow(6, "尚未配置产品 BOM；成品将暂时使用通用兼容流程");
}

function renderBomDraft() {
  const physicalCount = state.bomDraft.reduce((total, slot) => total + slot.quantity, 0);
  $("#bom-slot-count").textContent = `${physicalCount} / 20 个实物部件`;
  $("#bom-draft-slots").innerHTML = state.bomDraft.length ? state.bomDraft.map((slot, index) => `
    <div class="bom-draft-item"><div class="bom-position">${index + 1}</div><div><strong>${escapeHtml(slot.slotName)}${slot.quantity > 1 ? ` × ${slot.quantity}` : ""}</strong>
    <span>${escapeHtml(slot.categoryCode)} · ${escapeHtml(slot.categoryName)} · 接受同分类所有供应商</span></div>
    <button type="button" class="bom-remove" data-remove-bom-slot="${index}" aria-label="删除槽位">${icon("close")}</button></div>`).join("")
    : '<div class="empty-mini">尚未添加部件槽位</div>';
  $$('[data-remove-bom-slot]').forEach((button) => button.addEventListener("click", () => {
    state.bomDraft.splice(Number(button.dataset.removeBomSlot), 1); renderBomDraft();
  }));
}

function addBomSlot() {
  const slotName = $("#bom-slot-name").value.trim();
  const partTypeId = Number($("#bom-slot-part").value);
  const quantity = Number($("#bom-slot-quantity").value);
  const part = state.partTypes.find((item) => item.id === partTypeId);
  const currentCount = state.bomDraft.reduce((total, slot) => total + slot.quantity, 0);
  if (!slotName) return toast("无法添加槽位", "请填写槽位名称", "error");
  if (!part) return toast("无法添加槽位", "请选择有效的部件分类", "error");
  if (!Number.isInteger(quantity) || quantity < 1 || currentCount + quantity > 20) {
    return toast("无法添加槽位", "BOM 实物部件总数需在 1-20 之间", "error");
  }
  state.bomDraft.push({ slotName, partTypeId, quantity, categoryCode: part.categoryCode, categoryName: part.categoryName });
  $("#bom-slot-name").value = ""; $("#bom-slot-part").value = ""; $("#bom-slot-quantity").value = "1";
  renderBomDraft();
}

function clearBomDraft() { state.bomDraft = []; renderBomDraft(); }

async function createTracePlan(event) {
  event.preventDefault();
  if (!state.bomDraft.length) return toast("无法保存 BOM", "请至少添加一个部件槽位", "error");
  const form = event.currentTarget;
  const master = Object.fromEntries(new FormData(form));
  try {
    const plan = await api("/api/trace-plans", {
      method: "POST",
      body: {
        productModelId: master.productModelId,
        name: master.name,
        version: master.version,
        slots: state.bomDraft.map(({ slotName, partTypeId, quantity }) => ({ slotName, partTypeId, quantity })),
      },
    });
    form.reset(); clearBomDraft(); await loadCatalog();
    toast("BOM 版本已启用", `${plan.modelCode} · ${plan.version} · ${plan.slots.length} 个部件槽位`);
  } catch (error) { showError(error, "保存 BOM 失败"); }
}

function renderPartLabels() {
  $("#label-count").textContent = `${state.partLabels.length} 个标签`;
  $("#label-table").innerHTML = state.partLabels.length ? state.partLabels.map((item) => `
    <tr><td><code>${escapeHtml(item.identificationCode)}</code><span class="cell-secondary">${escapeHtml(item.batchCode || "历史单码")}</span></td>
      <td><span class="cell-primary">${escapeHtml(item.partName)}</span><span class="cell-secondary">${escapeHtml(item.partCode)}</span></td>
      <td><span class="cell-primary">${escapeHtml(item.supplierName)}</span><span class="cell-secondary">${escapeHtml(item.supplierCode)}</span></td>
      <td>${item.dataEntered
        ? `<span class="cell-primary">${escapeHtml(item.sourceSerialNo || "无供应商序列号")} · ${item.inspectionStatus === "PASS" ? "合格" : item.inspectionStatus === "FAIL" ? "不合格" : "待检"}</span><span class="cell-secondary">${escapeHtml(item.enteredBy)} · ${formatDate(item.enteredAt)}</span>`
        : '<span class="status pending">待录入单码数据</span>'}</td>
      <td>${item.used ? '<span class="status used">已使用</span>' : item.reservedStation ? `<span class="status pending">${escapeHtml(item.reservedStation)} 扫码中</span>` : '<span class="status available">可使用</span>'}</td>
      <td class="right"><div class="row-actions"><button class="btn secondary small" data-part-data="${item.id}">${icon("edit")}录入数据</button>${item.used ? `<button class="btn secondary small" data-part-genealogy="${escapeHtml(item.identificationCode)}">${icon("records")}查看去向</button>` : ""}<button class="btn secondary small" data-part-label="${item.id}">${icon("qr")}查看标签</button></div></td></tr>`).join("")
    : emptyRow(6, "暂无部件标签，请在上方生成");
  $$('[data-part-label]').forEach((button) => button.addEventListener("click", () => openPartLabel(Number(button.dataset.partLabel))));
  $$('[data-part-data]').forEach((button) => button.addEventListener("click", () => openPartData(Number(button.dataset.partData))));
  $$('[data-part-genealogy]').forEach((button) => button.addEventListener("click", () => openGenealogy(button.dataset.partGenealogy)));
}

async function createSupplier(event) {
  event.preventDefault(); const form = event.currentTarget;
  try {
    const supplier = await api("/api/suppliers", { method: "POST", body: Object.fromEntries(new FormData(form)) });
    form.reset(); await loadCatalog(); toast("供应商已创建", `${supplier.supplierCode} · ${supplier.name}`);
  } catch (error) { showError(error, "新增供应商失败"); }
}

async function createPartType(event) {
  event.preventDefault(); const form = event.currentTarget;
  try {
    const part = await api("/api/part-types", { method: "POST", body: Object.fromEntries(new FormData(form)) });
    form.reset(); await loadCatalog(); toast("部件类型已创建", `${part.partCode} · ${part.name}`);
  } catch (error) { showError(error, "新增部件类型失败"); }
}

async function createPartLabels(event) {
  event.preventDefault(); const form = event.currentTarget; const payload = Object.fromEntries(new FormData(form));
  try {
    const labels = await api("/api/part-labels", { method: "POST", body: payload });
    form.reset(); form.elements.quantity.value = 1; $("#label-search").value = ""; await loadCatalog();
    if (labels[0]) openBatchDetail(labels[0].labelBatchId);
    toast("部件二维码已生成", `${labels[0]?.batchCode || "新批次"} · ${payload.quantity} 个唯一标签`);
  } catch (error) { showError(error, "生成标签失败"); }
}

function findPartLabel(id) {
  return state.partLabels.find((item) => item.id === id)
    || state.activeBatchLabels.find((item) => item.id === id);
}

function openPartData(id) {
  const label = findPartLabel(id); if (!label) return;
  const form = $("#part-data-form");
  form.elements.labelId.value = label.id;
  form.elements.sourceSerialNo.value = label.sourceSerialNo || "";
  form.elements.productionDate.value = label.productionDate || "";
  form.elements.inspectionStatus.value = label.inspectionStatus || "PENDING";
  form.elements.remarks.value = label.remarks || "";
  $("#part-data-code").textContent = label.identificationCode;
  $("#part-data-modal").classList.remove("hidden");
  form.elements.sourceSerialNo.focus();
}

function closePartData() { $("#part-data-modal").classList.add("hidden"); }

async function savePartData(event) {
  event.preventDefault(); const form = event.currentTarget;
  const payload = Object.fromEntries(new FormData(form));
  const labelId = Number(payload.labelId); delete payload.labelId;
  try {
    const label = await api(`/api/part-labels/${labelId}`, { method: "PUT", body: payload });
    closePartData(); await loadCatalog();
    toast("单码数据已保存", `${label.partCode} · ${label.sourceSerialNo || label.identificationCode}`);
  } catch (error) { showError(error, "保存单码数据失败"); }
}

async function updateSettings(event) {
  event.preventDefault();
  try {
    const requireQualityRelease = $("#require-quality-release").checked;
    state.settings = await api("/api/settings", { method: "PUT", body: { requireQualityRelease } });
    toast("流程设置已保存", `质量门禁${requireQualityRelease ? "已启用" : "保持关闭"}`); renderDashboardFlow();
  } catch (error) { showError(error, "保存设置失败"); }
}

async function loadRecords() {
  const search = $("#record-search").value.trim();
  state.records = await api(`/api/records?search=${encodeURIComponent(search)}`);
  $("#record-count").textContent = `${state.records.length} 条记录`;
  $("#export-records").href = `/api/records/export.xlsx?search=${encodeURIComponent(search)}`;
  $("#record-table").innerHTML = state.records.length ? state.records.map((record) => `
    <tr><td><span class="cell-primary">${escapeHtml(record.traceNo)}</span><span class="cell-secondary">#${record.id}</span></td>
      <td><span class="cell-primary">${escapeHtml(record.machine.sn)}</span><span class="cell-secondary">${escapeHtml(record.machine.model)}</span></td>
      <td><span class="cell-primary">${record.tracePlan ? `${escapeHtml(record.tracePlan.name)} · ${escapeHtml(record.tracePlan.version)}` : "通用流程"}</span>
      <span class="cell-secondary">${record.status === "ASSEMBLED" ? "已装配" : escapeHtml(record.status)}</span></td>
      <td><div class="part-stack">${record.parts.map((part) => `<span class="part-chip"><b>${part.position}</b><span>${escapeHtml(part.partCode)} · ${escapeHtml(part.supplierName)}</span></span>`).join("")}</div></td>
      <td><span class="cell-primary">${escapeHtml(record.stationName || "-")}</span><span class="cell-secondary">${escapeHtml(record.operatorName || "未填写操作员")}</span></td>
      <td>${formatDate(record.completedAt)}</td><td class="right"><button class="btn secondary small" data-genealogy="${escapeHtml(record.machine.identificationCode)}">${icon("records")}产品族谱</button></td></tr>`).join("") : emptyRow(7, "没有符合条件的生产记录");
  $$('[data-genealogy]').forEach((button) => button.addEventListener("click", () => openGenealogy(button.dataset.genealogy)));
}

function auditEventDetail(event) {
  const payload = event.payload || {};
  const details = [];
  if (payload.slotName) details.push(payload.slotName);
  if (payload.requiredPartCount) details.push(`${payload.requiredPartCount} 个部件`);
  if (payload.role) details.push(payload.role === "ADMIN" ? "管理员" : "录入员");
  if (payload.passwordReset) details.push("已重置密码");
  if (event.reason) details.push(event.reason);
  return details.join(" · ") || "-";
}

function renderAudit() {
  const audit = state.audit;
  $("#audit-total").textContent = audit.total;
  $("#audit-today").textContent = audit.today;
  $("#audit-operators").textContent = audit.operators;
  $("#audit-count").textContent = `${audit.filteredTotal} 条日志${audit.items.length < audit.filteredTotal ? ` · 显示最近 ${audit.items.length} 条` : ""}`;

  const currentType = $("#audit-event-type").value;
  $("#audit-event-type").innerHTML = '<option value="">全部事件类型</option>' + audit.eventTypes.map((item) =>
    `<option value="${escapeHtml(item.value)}">${escapeHtml(eventLabels[item.value] || item.value)}（${item.count}）</option>`).join("");
  $("#audit-event-type").value = currentType;

  $("#audit-table").innerHTML = audit.items.length ? audit.items.map((event) => {
    const actor = event.actorDisplayName || event.operatorName || "系统自动记录";
    const actorAccount = event.actorUsername ? `账号 ${event.actorUsername}` : event.operatorName && event.actorDisplayName !== event.operatorName ? event.operatorName : "";
    const object = event.relatedObjectCode
      ? `${event.objectCode} → ${event.relatedObjectCode}`
      : event.objectCode;
    return `<tr>
      <td><span class="cell-primary">${formatDate(event.occurredAt)}</span><span class="cell-secondary">${escapeHtml(event.eventId)}</span></td>
      <td><span class="audit-event-tag">${escapeHtml(eventLabels[event.eventType] || event.eventType)}</span><span class="cell-secondary">${escapeHtml(event.eventType)}</span></td>
      <td><span class="cell-primary">${escapeHtml(actor)}</span><span class="cell-secondary">${escapeHtml(actorAccount)}</span></td>
      <td><span class="cell-primary audit-object">${escapeHtml(object || "-")}</span><span class="cell-secondary">${escapeHtml(event.objectType || "-")}</span></td>
      <td><span class="cell-primary">${escapeHtml(event.stationName || "-")}</span><span class="cell-secondary">${escapeHtml(event.stationId || "")}</span></td>
      <td>${escapeHtml(auditEventDetail(event))}</td>
    </tr>`;
  }).join("") : emptyRow(6, "没有符合条件的审计日志");
}

async function loadAudit() {
  if (!isAdmin()) return;
  const search = $("#audit-search").value.trim();
  const eventType = $("#audit-event-type").value;
  state.audit = await api(`/api/audit-events?search=${encodeURIComponent(search)}&eventType=${encodeURIComponent(eventType)}&limit=200`);
  renderAudit();
}

const eventLabels = {
  MACHINE_COMMISSIONED: "成品建档",
  PART_LABEL_BATCH_GENERATED: "部件编码批次生成",
  PART_LABEL_COMMISSIONED: "部件标签生成",
  PART_LABEL_DATA_ENTERED: "单码数据录入",
  TRACE_PLAN_ACTIVATED: "BOM 版本启用",
  ASSEMBLY_STARTED: "开始装配",
  COMPONENT_SCANNED: "部件扫码绑定",
  COMPONENT_SCAN_UNDONE: "部件扫码撤销",
  ASSEMBLY_COMPLETED: "装配归档完成",
  ASSEMBLY_CANCELLED: "装配流程取消",
  GENERIC_WORKFLOW_CHANGED: "通用流程设置变更",
  USER_LOGIN: "账号登录",
  USER_LOGOUT: "账号退出",
  USER_PASSWORD_CHANGED: "登录密码修改",
  USER_CREATED: "生产账号创建",
  USER_UPDATED: "生产账号更新",
};

async function openGenealogy(code) {
  $("#genealogy-modal").classList.remove("hidden");
  $("#genealogy-title").textContent = "正在读取产品族谱…";
  $("#genealogy-subtitle").textContent = code;
  $("#genealogy-summary").innerHTML = "";
  $("#genealogy-parts").innerHTML = '<div class="empty-mini">正在加载</div>';
  $("#genealogy-events").innerHTML = "";
  try {
    const data = await api(`/api/genealogy?code=${encodeURIComponent(code)}`);
    const record = data.records[0] || null;
    const isMachine = data.queryType === "MACHINE";
    $("#genealogy-title").textContent = isMachine ? data.machine.sn : data.partLabel.partCode;
    $("#genealogy-subtitle").textContent = isMachine
      ? "向上查看：该整机投入的全部部件、供应商、批次和生产事件"
      : "向下追踪：查看该部件最终流向的整机";
    const summary = isMachine ? [
      ["产品型号", data.machine.model],
      ["BOM 版本", data.tracePlan ? `${data.tracePlan.name} · ${data.tracePlan.version}` : "通用流程"],
      ["归档状态", record ? "已装配归档" : "尚未归档"],
      ["部件数量", record ? `${record.parts.length} 个` : "0 个"],
    ] : [
      ["部件类型", `${data.partLabel.partCode} · ${data.partLabel.partName}`],
      ["供应商", data.partLabel.supplierName],
      ["编码批次", data.partLabel.batchCode || "历史单码"],
      ["供应商序列号", data.partLabel.sourceSerialNo || "-"],
      ["录入时间", data.partLabel.enteredAt ? formatDate(data.partLabel.enteredAt) : "尚未录入"],
      ["流向整机", data.records.length ? `${data.records.length} 台` : "尚未使用"],
    ];
    $("#genealogy-summary").innerHTML = summary.map(([label, value]) => `<div class="genealogy-kpi"><span>${escapeHtml(label)}</span><strong title="${escapeHtml(value)}">${escapeHtml(value)}</strong></div>`).join("");

    if (isMachine) {
      $("#genealogy-parts").innerHTML = record?.parts.length ? record.parts.map((part) => `
        <div class="genealogy-part"><div class="bom-position">${part.position}</div><div><strong>${escapeHtml(part.partCode)} · ${escapeHtml(part.partName)}</strong>
        <span>${escapeHtml(part.supplierName)} · 批次 ${escapeHtml(part.lotNo || "-")} · 单件序列号 ${escapeHtml(part.sourceSerialNo || "-")}</span><code>${escapeHtml(part.identificationCode)}</code></div></div>`).join("")
        : '<div class="empty-mini">该成品尚未完成部件归档</div>';
    } else {
      $("#genealogy-parts").innerHTML = data.records.length ? data.records.map((item, index) => `
        <div class="genealogy-part"><div class="bom-position">${index + 1}</div><div><strong>${escapeHtml(item.machine.sn)}</strong>
        <span>${escapeHtml(item.machine.model)} · ${escapeHtml(item.traceNo)}</span><code>${formatDate(item.completedAt)}</code></div></div>`).join("")
        : '<div class="empty-mini">该部件尚未装配到成品</div>';
    }
    $("#genealogy-events").innerHTML = data.events.length ? data.events.map((event) => `
      <div class="event-item"><strong>${escapeHtml(eventLabels[event.eventType] || event.eventType)}</strong>
      <span>${formatDate(event.occurredAt)} · ${escapeHtml(event.stationName || "系统")} · ${escapeHtml(event.operatorName || "自动记录")}${event.payload.slotName ? ` · ${escapeHtml(event.payload.slotName)}` : ""}</span></div>`).join("")
      : '<div class="empty-mini">暂无审计事件</div>';
  } catch (error) {
    $("#genealogy-title").textContent = "族谱读取失败";
    $("#genealogy-parts").innerHTML = `<div class="empty-mini">${escapeHtml(error.message)}</div>`;
    showError(error, "族谱读取失败");
  }
}

function closeGenealogy() { $("#genealogy-modal").classList.add("hidden"); }

function openMachineLabel(id) {
  const machine = state.machines.find((item) => item.id === id); if (!machine) return;
  openLabel({ title: "成品唯一识别标签", subtitle: `SN：${machine.sn}`, code: machine.identificationCode,
    image: `/api/machines/${machine.id}/qr`, meta: `产品：${escapeHtml(machine.productFamilyName || "历史产品")}　型号：${escapeHtml(machine.model)}<br>生产日期：${escapeHtml(machine.productionDate || "-")}` });
}

function openPartLabel(id) {
  const label = findPartLabel(id); if (!label) return;
  openLabel({ title: "部件唯一识别标签", subtitle: `${label.partCode} · ${label.partName}`, code: label.identificationCode,
    image: `/api/part-labels/${label.id}/qr`,
    meta: `供应商：${escapeHtml(label.supplierCode)} · ${escapeHtml(label.supplierName)}<br>编码批次：${escapeHtml(label.batchCode || "历史单码")}<br>生产批次：${escapeHtml(label.lotNo || "-")}　供应商批次：${escapeHtml(label.supplierBatchNo || "-")}<br>供应商单件序列号：${escapeHtml(label.sourceSerialNo || "尚未录入")}` });
}

function openLabel(data) {
  $("#label-modal-title").textContent = data.title; $("#label-modal-subtitle").textContent = data.subtitle;
  $("#label-code").textContent = data.code; $("#label-qr-image").src = data.image; $("#label-meta").innerHTML = data.meta;
  $("#label-modal").classList.remove("hidden");
}

function closeLabel() { $("#label-modal").classList.add("hidden"); $("#label-qr-image").removeAttribute("src"); }

function selectCatalogTab(name) {
  const adminOnlyTabs = new Set(["products", "bom", "parts", "suppliers", "users", "settings"]);
  if (!isAdmin() && adminOnlyTabs.has(name)) name = "labels";
  if (!$(`#catalog-${name}`)) name = "labels";
  $$(".catalog-tab").forEach((button) => button.classList.toggle("active", button.dataset.catalog === name));
  $$(".catalog-page").forEach((page) => page.classList.toggle("active", page.id === `catalog-${name}`));
}

function bindEvents() {
  $("#login-form").addEventListener("submit", submitLogin);
  $("#password-form").addEventListener("submit", submitPasswordChange);
  $("#password-close").addEventListener("click", closePasswordChange);
  $("#account-menu-button").addEventListener("click", toggleAccountMenu);
  $("#change-password-button").addEventListener("click", () => { closeAccountMenu(); showPasswordChange(false); });
  $("#logout-button").addEventListener("click", logout);
  $$(".nav-item").forEach((button) => button.addEventListener("click", () => switchView(button.dataset.view)));
  $$('[data-go]').forEach((button) => button.addEventListener("click", () => switchView(button.dataset.go)));
  $$('[data-open="machine-form"]').forEach((button) => button.addEventListener("click", () => $("#machine-form-panel").classList.remove("hidden")));
  $$('[data-close="machine-form"]').forEach((button) => button.addEventListener("click", () => $("#machine-form-panel").classList.add("hidden")));
  $$(".catalog-tab").forEach((button) => button.addEventListener("click", () => selectCatalogTab(button.dataset.catalog)));
  $$(".modal-close").forEach((button) => button.addEventListener("click", closeLabel));
  $$(".bluetooth-close").forEach((button) => button.addEventListener("click", closeBluetoothCapture));
  $$(".genealogy-close").forEach((button) => button.addEventListener("click", closeGenealogy));
  $$(".part-data-close").forEach((button) => button.addEventListener("click", closePartData));
  $$(".batch-detail-close").forEach((button) => button.addEventListener("click", closeBatchDetail));
  $("#label-modal").addEventListener("click", (event) => { if (event.target === event.currentTarget) closeLabel(); });
  $("#bluetooth-modal").addEventListener("click", (event) => { if (event.target === event.currentTarget) closeBluetoothCapture(); });
  $("#genealogy-modal").addEventListener("click", (event) => { if (event.target === event.currentTarget) closeGenealogy(); });
  $("#part-data-modal").addEventListener("click", (event) => { if (event.target === event.currentTarget) closePartData(); });
  $("#batch-detail-modal").addEventListener("click", (event) => { if (event.target === event.currentTarget) closeBatchDetail(); });
  $("#scan-undo-modal").addEventListener("click", (event) => { if (event.target === event.currentTarget) closeScanUndo(); });
  $$(".scan-undo-close").forEach((button) => button.addEventListener("click", closeScanUndo));
  $("#print-button").addEventListener("click", () => window.print());
  $("#open-bluetooth-capture").addEventListener("click", openBluetoothCapture);
  $("#discover-bluetooth").addEventListener("click", discoverBluetoothDevices);
  document.addEventListener("click", (event) => { if (!event.target.closest(".account-menu")) closeAccountMenu(); });
  document.addEventListener("keydown", handleScannerKeydown, true);
  document.addEventListener("keydown", (event) => { if (event.key === "Escape") { closeLabel(); closeBluetoothCapture(); closeGenealogy(); closePartData(); closeBatchDetail(); closeScanUndo(); closePasswordChange(); closeAccountMenu(); } });

  $("#scan-form").addEventListener("submit", submitScan); $("#reset-session").addEventListener("click", resetScanSession);
  $("#undo-last-scan").addEventListener("click", openScanUndo);
  $("#scan-undo-form").addEventListener("submit", submitScanUndo);
  $("#scan-next-machine").addEventListener("click", () => { $("#scan-completion").classList.add("hidden"); setScanFeedback("neutral", "等待下一台成品", "请直接扫描成品二维码。"); focusScanner(); });
  $("#scan-code").addEventListener("focus", updateScannerFocusState);
  $("#scan-code").addEventListener("blur", updateScannerFocusState);
  $("#scan-sound-toggle").addEventListener("change", (event) => {
    state.scanSoundEnabled = event.target.checked;
    localStorage.setItem("pts_scan_sound", state.scanSoundEnabled ? "1" : "0");
    if (state.scanSoundEnabled) beep("success");
  });
  window.addEventListener("focus", focusScanner);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) focusScanner(); });
  $("#station-name").addEventListener("input", saveStationDetails);
  $("#machine-form").addEventListener("submit", createMachine); $("#supplier-form").addEventListener("submit", createSupplier);
  $("#product-family-form").addEventListener("submit", createProductFamily);
  $("#product-model-form").addEventListener("submit", createProductModel);
  $("#user-form").addEventListener("submit", createUser);
  $("#user-role").addEventListener("change", updateUserScopeState);
  $$('[data-scope-all]').forEach((button) => button.addEventListener("click", () => {
    const selector = button.dataset.scopeAll === "product" ? "[data-user-product-id]" : "[data-user-supplier-id]";
    const inputs = $$(selector); const shouldSelect = inputs.some((input) => !input.checked);
    inputs.forEach((input) => { input.checked = shouldSelect; });
    button.textContent = shouldSelect ? "清空" : "全选";
  }));
  $("#cancel-product-family-edit").addEventListener("click", resetProductFamilyForm);
  $("#cancel-product-model-edit").addEventListener("click", resetProductModelForm);
  $("#cancel-user-edit").addEventListener("click", resetUserForm);
  $("#model-identity-source").addEventListener("change", updateBluetoothFieldState);
  $("#part-type-form").addEventListener("submit", createPartType); $("#label-form").addEventListener("submit", createPartLabels);
  $("#label-supplier-filter").addEventListener("change", updateLabelPartTypeOptions);
  $("#part-type-form").elements.categoryCode.addEventListener("change", (event) => {
    const category = partCategories().find((item) => item.code === event.target.value.trim().toUpperCase());
    if (category) $("#part-type-form").elements.categoryName.value = category.name;
  });
  $("#part-type-form").elements.categoryName.addEventListener("change", (event) => {
    const category = partCategories().find((item) => item.name === event.target.value.trim());
    if (category) $("#part-type-form").elements.categoryCode.value = category.code;
  });
  $("#trace-plan-form").addEventListener("submit", createTracePlan);
  $("#part-data-form").addEventListener("submit", savePartData);
  $("#add-bom-slot").addEventListener("click", addBomSlot);
  $("#clear-bom-draft").addEventListener("click", clearBomDraft);
  $("#settings-form").addEventListener("submit", updateSettings);

  $("#machine-search").addEventListener("input", debounce(() => loadMachines().catch((error) => showError(error, "搜索失败"))));
  $("#label-search").addEventListener("input", debounce(async () => {
    try {
      const search = encodeURIComponent($("#label-search").value.trim());
      [state.partLabelBatches, state.partLabels] = await Promise.all([
        api(`/api/part-label-batches?search=${search}`), api(`/api/part-labels?search=${search}`),
      ]);
      renderPartLabelBatches(); renderPartLabels();
    }
    catch (error) { showError(error, "搜索失败"); }
  }));
  $("#record-search").addEventListener("input", debounce(() => loadRecords().catch((error) => showError(error, "搜索失败"))));
  $("#audit-search").addEventListener("input", debounce(() => loadAudit().catch((error) => showError(error, "日志搜索失败"))));
  $("#audit-event-type").addEventListener("change", () => loadAudit().catch((error) => showError(error, "日志筛选失败")));
}

async function initialize() {
  $("#station-id").textContent = getStationId();
  $("#station-name").value = localStorage.getItem("pts_station_name") || "自动装配工位";
  $("#scan-sound-toggle").checked = state.scanSoundEnabled;
  updateScannerClock(); setInterval(updateScannerClock, 1000);
  saveStationDetails(); bindEvents();
  try {
    await api("/api/health");
    const user = await api("/api/auth/me");
    setCurrentUser(user);
    if (user.mustChangePassword) showPasswordChange(true);
    else await enterApplication();
  } catch (error) {
    if (error.status === 401) return;
    setServerState(false); showLogin("系统服务暂时不可用，请稍后重试"); showError(error, "系统初始化失败");
  }
}

document.addEventListener("DOMContentLoaded", initialize);
