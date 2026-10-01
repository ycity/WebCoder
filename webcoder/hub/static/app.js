"use strict";

const app = document.getElementById("app");
const state = { session: null, poll: null, view: 0,  // view: bumped on every render and on logout
  leave: null,           // set by a page with more to clean up than a poll (collaboration.js); called on navigation
  collaboration: null };  // whether the hub has the collaboration center on (null: not asked yet)

// ---------------------------------------------------------------- helpers
const ESCAPES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (ch) => ESCAPES[ch]);
const fmtTime = (ts) => (ts ? new Date(ts * 1000).toLocaleString("zh-CN", { hour12: false }) : "—");

function fmtAgo(ts) {
  if (!ts) return "—";
  const seconds = Math.max(0, Date.now() / 1000 - ts);
  if (seconds < 60) return "刚刚";
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小时前`;
  return `${Math.floor(seconds / 86400)} 天前`;
}

async function api(path, { method = "GET", body } = {}) {
  const view = state.view;
  const headers = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (method !== "GET" && state.session && state.session.csrf) headers["X-CSRF-Token"] = state.session.csrf;
  const response = await fetch(path, {
    method, headers, credentials: "same-origin", body: body === undefined ? undefined : JSON.stringify(body),
  });
  let data = null;
  try { data = await response.json(); } catch { data = null; }
  if (method === "GET" && view !== state.view) {  // another page, or signed out, meanwhile: never drawn
    const stale = new Error("stale");
    stale.stale = true;
    throw stale;
  }
  if (!response.ok) {
    const error = new Error((data && data.error && data.error.message) || `请求失败（HTTP ${response.status}）`);
    error.status = response.status;
    error.code = data && data.error && data.error.code;
    throw error;
  }
  return data;
}

function toast(message, kind = "ok") {
  const el = document.getElementById("toast");
  el.textContent = message;
  el.className = `toast ${kind}`;
  el.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { el.hidden = true; }, 3500);
}

function openModal(html) {
  document.getElementById("modal-body").innerHTML = html;
  document.getElementById("modal").hidden = false;
}

function closeModal() {
  document.getElementById("modal").hidden = true;
  document.getElementById("modal-body").innerHTML = "";
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    const area = document.createElement("textarea");
    area.value = text;
    document.body.appendChild(area);
    area.select();
    document.execCommand("copy");
    area.remove();
  }
  toast("已复制");
}

const codeBlock = (text) =>
  `<div class="code"><pre>${esc(text)}</pre><button class="copy" type="button" data-copy="${esc(text)}">复制</button></div>`;

function guard(handler) {
  return async (event) => {
    try { await handler(event); } catch (error) { if (!error.stale) toast(error.message, "err"); }
  };
}

document.addEventListener("click", (event) => {
  const copy = event.target.closest("[data-copy]");
  if (copy) { copyText(copy.dataset.copy); return; }
  if (event.target.closest("[data-close]")) closeModal();
});

// ---------------------------------------------------------------- routing
const routes = {
  overview: viewOverview, devices: viewDevices, projects: viewProjects, tokens: viewTokens,
  clients: viewClients, audit: viewAudit, consent: viewConsent, users: viewUsers,
  collaboration: viewCollaboration,
};
const publicRoutes = { invite: viewInvite, reset: viewReset };  // reachable without signing in
// The collaboration page lives in collaboration.js, a later deferred script: all of them have run by DOMContentLoaded.
const scriptsLoaded = new Promise((resolve) => document.addEventListener("DOMContentLoaded", resolve, { once: true }));

async function viewCollaboration(arg) {
  if (!window.WebCoderCollaboration) await scriptsLoaded;
  if (!window.WebCoderCollaboration) throw new Error("协作中心的页面脚本没有加载成功，请刷新页面重试。");
  await window.WebCoderCollaboration.view(arg);
}

async function render() {
  state.view += 1;
  clearInterval(state.poll);
  state.poll = null;
  if (state.leave) {
    const leave = state.leave;
    state.leave = null;
    leave();
  }
  const [name, arg] = location.hash.replace(/^#\/?/, "").split("/");
  try {
    if (publicRoutes[name]) { showChrome(false); await publicRoutes[name](arg); return; }
    if (!state.session || !state.session.authenticated) { showChrome(false); viewLogin(); return; }
    showChrome(true, name || "overview");
    await (routes[name] || viewOverview)(arg);
  } catch (error) {
    if (error.stale) return;  // a newer render took over
    if (error.status === 401 && !publicRoutes[name]) { state.session.authenticated = false; render(); return; }
    app.innerHTML = `<section class="card narrow"><div class="alert error">${esc(error.message)}</div></section>`;
  }
}

function showChrome(visible, active) {
  document.getElementById("topbar").hidden = !visible;
  document.querySelectorAll("#nav a").forEach((a) => a.classList.toggle("active", a.dataset.route === active));
  if (!visible) return;
  document.getElementById("who").textContent = state.session.username || "";
  document.querySelector('#nav a[data-route="users"]').hidden = state.session.role !== "admin";
  collaborationNav();
}

// The collaboration center is opt-in (WEBCODER_COLLABORATION_ENABLED): its link shows only when the hub has it on.
async function collaborationNav() {
  const link = document.querySelector('#nav a[data-route="collaboration"]');
  link.hidden = state.collaboration !== true;
  if (state.collaboration !== null || collaborationNav.pending) return;
  collaborationNav.pending = true;
  try {
    const response = await fetch("/api/collaboration/status", { credentials: "same-origin" });
    if (response.ok) state.collaboration = Boolean((await response.json()).features?.enabled);
  } catch { /* asked again on the next page */ }
  collaborationNav.pending = false;
  link.hidden = state.collaboration !== true;
}

function signedIn(result) {
  Object.assign(state.session, result, { authenticated: true });
  history.replaceState(null, "", "#/overview");  // keep used invite / reset links out of the history
  render();
}

function showAccount() {
  openModal(`<h2>账号：${esc(state.session.username)}</h2>
    <p class="muted">角色：${state.session.role === "admin" ? "管理员" : "普通用户"}</p>
    <form id="password-form" class="form">
      <label>当前密码<input name="current" type="password" autocomplete="current-password" required></label>
      <label>新密码（至少 10 位）<input name="next" type="password" autocomplete="new-password" minlength="10" required></label>
      <label>再次输入新密码<input name="again" type="password" autocomplete="new-password" minlength="10" required></label>
      <p class="error" id="password-error"></p>
      <div class="actions"><button type="button" data-close>取消</button><button class="primary" type="submit">修改密码</button></div>
    </form>`);
  document.getElementById("password-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = new FormData(event.target);
    const errorBox = document.getElementById("password-error");
    if (data.get("next") !== data.get("again")) { errorBox.textContent = "两次输入的新密码不一致"; return; }
    try {
      const result = await api("/api/account/password", { method: "POST",
        body: { current_password: data.get("current"), new_password: data.get("next") } });
      Object.assign(state.session, result);
      closeModal();
      toast("密码已修改，其他设备上的登录已退出");
    } catch (error) {
      errorBox.textContent = error.message;
    }
  });
}

// ---------------------------------------------------------------- login
function viewLogin() {
  if (!state.session.initialized) {
    app.innerHTML = `<section class="card narrow"><h1>WebCoder</h1>
      <p>还没有管理员账号。请在运行 Hub 的服务器上执行：</p>${codeBlock("python -m webcoder.hub init-admin")}
      <p class="muted">创建完成后刷新本页。</p></section>`;
    return;
  }
  app.innerHTML = `<section class="card narrow"><h1>登录 WebCoder</h1>
    <form id="login-form" class="form">
      <label>用户名<input name="username" autocomplete="username" required></label>
      <label>密码<input name="password" type="password" autocomplete="current-password" required></label>
      <button class="primary" type="submit">登录</button>
      <p class="error" id="login-error"></p>
    </form></section>`;
  document.getElementById("login-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = new FormData(event.target);
    try {
      const result = await api("/api/login", {
        method: "POST", body: { username: form.get("username"), password: form.get("password") },
      });
      Object.assign(state.session, result, { authenticated: true });
      render();  // keeps the current route, e.g. an OAuth consent page
    } catch (error) {
      document.getElementById("login-error").textContent = error.message;
    }
  });
}

// ---------------------------------------------------------------- overview
function insecureBanner() {
  if (state.session.https || state.session.local) return "";
  return `<div class="alert warn">Hub 当前通过 HTTP 对外服务：面板密码和访问令牌会明文传输，ChatGPT 网页版直连也要求 HTTPS。
    建议按 README 用 Caddy 配置 HTTPS，或改用 Secure MCP Tunnel。Agent 的配对和它与 Hub 之间的通道自带加密，旁听拿不到设备密钥；但安装命令下载的程序同样走 HTTP，网络上能篡改流量的人仍可能动手脚。</div>`;
}

function auditTarget(target) {  // SSH commands are logged in full; long ones start folded
  const text = target || "";
  if (text.length <= 300 && !text.includes("\n")) return esc(text);
  return `<details class="audit-full"><summary>${esc(text.split("\n")[0].slice(0, 200))} …</summary><pre>${esc(text)}</pre></details>`;
}

function auditTable(rows) {
  if (!rows.length) return `<p class="muted">暂无记录</p>`;
  return `<div class="table-wrap"><table><thead><tr><th>时间</th><th>来源</th><th>操作</th><th>对象</th><th>结果</th><th>耗时</th></tr></thead>
    <tbody>${rows.map((row) => `<tr><td>${esc(fmtTime(row.ts))}</td><td>${esc(row.actor)}</td>
      <td><code>${esc(row.action)}</code></td><td class="wrap">${auditTarget(row.target)}</td>
      <td>${row.status === "ok" ? '<span class="badge ok">成功</span>' : `<span class="badge err">${esc(row.status)}</span>`}</td>
      <td>${row.duration_ms != null ? `${row.duration_ms} ms` : ""}</td></tr>`).join("")}</tbody></table></div>`;
}

async function viewOverview() {
  const data = await api("/api/overview");
  app.innerHTML = `${insecureBanner()}
    <div class="stats">
      <div class="stat"><b>${data.devices.online}/${data.devices.total}</b><span>设备在线</span></div>
      <div class="stat"><b>${data.projects}</b><span>项目</span></div>
      <div class="stat"><b>${data.tokens}</b><span>有效授权（令牌 + OAuth）</span></div>
    </div>
    <section class="card"><h2>MCP 地址</h2>${codeBlock(state.session.mcp_url)}
      <p class="muted">在 ChatGPT 或 Codex 中添加这个地址即可使用，具体步骤见「接入客户端」。</p></section>
    <section class="card"><h2>快速开始</h2><ol class="steps">
      <li>「设备」→ 添加设备，按提示在那台电脑上安装并配对 Agent（Agent 主动连接 Hub，无需开放端口）</li>
      <li>「项目」→ 选择设备上的一个文件夹建立项目，并设置只读或读写</li>
      <li>「访问令牌」→ 创建令牌（Codex、Secure MCP Tunnel 用）；ChatGPT 直连时在 ChatGPT 里走 OAuth 授权即可</li>
      <li>「接入客户端」→ 按 ChatGPT / Codex 的步骤接入</li></ol></section>
    <section class="card"><h2>最近活动</h2>${auditTable(data.recent)}</section>`;
}

// ---------------------------------------------------------------- devices
const STATUS = {
  online: '<span class="badge ok">在线</span>', offline: '<span class="badge">离线</span>',
  pending: '<span class="badge warn">待配对</span>', disabled: '<span class="badge err">已停用</span>',
};

const PLATFORM_NAMES = { windows: "Windows", macos: "macOS", linux: "Linux" };
const ROOT_EXAMPLES = { windows: "C:\\Users\\你\\WebCoder", macos: "/Users/你/WebCoder", linux: "/home/你/WebCoder" };
const splitRoots = (text) => text.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
const AGENT_PLATFORMS = { Windows: "windows", Darwin: "macos", Linux: "linux" };  // what a connected agent reports
const devicePlatform = (device) => AGENT_PLATFORMS[device.info.platform] || device.info.setup?.platform || "windows";
const MOBILE_CHECKBOX = (checked) => `<label class="check"><input type="checkbox" name="mobile" ${checked ? "checked" : ""}>
  允许 AI 控制连到这台电脑的手机 / 模拟器（安卓 adb、鸿蒙 hdc：读屏幕、点击、输入、装应用）</label>
  <p class="muted">默认关闭。开启后还需要在授权时单独勾选「控制手机」，AI 才能使用。</p>`;

function showInstall(name, result) {
  const c = result.commands;
  openModal(`<h2>在「${esc(name)}」上安装 Agent</h2>
    <p>在那台 ${esc(PLATFORM_NAMES[result.setup.platform])} 电脑的 <b>${esc(c.shell)}</b> 里运行下面这条命令（需要 Python 3.10+）。
      配对码 <span class="code-inline">${esc(result.code)}</span> 在 ${esc(fmtTime(result.expires))} 前有效，只能用一次。</p>
    ${codeBlock(c.one_click)}
    <p class="muted">它会：建立独立的 Python 环境 → 安装 Agent → 配对 →
      ${result.setup.roots.length ? "只开放 " + result.setup.roots.map((root) => `<code>${esc(root)}</code>`).join("、") : "不开放任何文件夹（之后可在「修改设置」里添加）"}
      ${result.setup.read_only ? "（只读）" : ""}${result.setup.autostart ? " → 设置开机自动运行" : ""}。文件夹不存在会自动创建。</p>
    <details><summary class="muted">不想用一键命令？分步安装</summary>${c.manual.map(codeBlock).join("")}</details>
    <div class="actions"><button data-close class="primary" type="button">完成</button></div>`);
}

function deviceForm(title, submitLabel, setup, withName, onSubmit) {
  openModal(`<h2>${esc(title)}</h2><form id="device-form" class="form">
      ${withName ? '<label>设备名称<input name="name" required maxlength="80" placeholder="例如：家里台式机"></label>' : ""}
      <label>系统<select name="platform">${Object.entries(PLATFORM_NAMES).map(([id, label]) =>
        `<option value="${id}" ${id === setup.platform ? "selected" : ""}>${label}</option>`).join("")}</select></label>
      <label>允许 AI 访问的文件夹（每行一个完整路径，其余文件都访问不到；只用手机或 SSH 时可以先留空）
        <textarea name="roots" rows="3" placeholder="${esc(ROOT_EXAMPLES[setup.platform])}">${esc(setup.roots.join("\n"))}</textarea></label>
      <label class="check"><input type="checkbox" name="read_only" ${setup.read_only ? "checked" : ""}> 整台设备只读（AI 只能看，不能改）</label>
      <label class="check"><input type="checkbox" name="autostart" ${setup.autostart ? "checked" : ""}> 开机自动运行 Agent</label>
      ${MOBILE_CHECKBOX(setup.mobile)}
      <p class="error" id="device-error"></p>
      <div class="actions"><button type="button" data-close>取消</button><button class="primary" type="submit">${esc(submitLabel)}</button></div>
    </form>`);
  const form = document.getElementById("device-form");
  form.platform.addEventListener("change", () => { form.roots.placeholder = ROOT_EXAMPLES[form.platform.value]; });
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const roots = splitRoots(form.roots.value);  // may be empty: folders can be added later
    try {
      await onSubmit({ name: withName ? form.name.value.trim() : undefined, platform: form.platform.value, roots,
        read_only: form.read_only.checked, autostart: form.autostart.checked, mobile: form.mobile.checked });
    } catch (error) {
      document.getElementById("device-error").textContent = error.message;
    }
  });
}

function rootsForm(device) {
  // What the agent reports, even an empty list; the saved setup only until it has reported
  const current = Array.isArray(device.info.allowed_roots) ? device.info.allowed_roots : device.info.setup?.roots || [];
  const platform = devicePlatform(device);
  const mobile = device.info.mobile ? Boolean(device.info.mobile.enabled) : Boolean(device.info.setup?.mobile);
  const hosts = device.info.ssh ? device.info.ssh.hosts || [] : device.info.setup?.ssh_hosts || [];
  const skills = device.info.skills ? Boolean(device.info.skills.enabled) : Boolean(device.info.setup?.skills);
  const programs = device.info.commands ? (device.info.commands.programs || []).map((item) => item.name)
    : device.info.setup?.programs || [];
  openModal(`<h2>修改「${esc(device.name)}」的设置</h2>
    <p class="muted">出于安全考虑，这些设置只能在设备上修改：保存后会得到一条命令，在那台电脑上运行即可，正在运行的 Agent 几秒内自动生效。</p>
    <form id="roots-form" class="form">
      <label>系统<select name="platform">${Object.entries(PLATFORM_NAMES).map(([id, label]) =>
        `<option value="${id}" ${id === platform ? "selected" : ""}>${label}</option>`).join("")}</select></label>
      <label>开放的文件夹（每行一个完整路径）<textarea name="roots" rows="4">${esc(current.join("\n"))}</textarea></label>
      ${MOBILE_CHECKBOX(mobile)}
      <label>允许 AI 通过 SSH 管理的服务器（每行一个，写这台电脑 ~/.ssh/config 里的主机名；留空 = 不允许）
        <textarea name="ssh_hosts" rows="3" placeholder="web-1&#10;db-1">${esc(hosts.join("\n"))}</textarea></label>
      <p class="muted">SSH 从这台电脑发起，用它自己的 ~/.ssh/config、密钥和 known_hosts；每台服务器要能免密码 <code>ssh 主机名</code>。
        AI 可以在这些服务器上执行任意命令（每条都需要你在 ChatGPT 里确认，并记入审计），授权时还要单独勾选。</p>
      <label class="check"><input type="checkbox" name="skills" ${skills ? "checked" : ""}>
        允许 AI 读取这台电脑上的 skills（~/.codex/skills、~/.claude/skills、~/.agents/skills，只读）</label>
      <p class="muted">只开放这几个 skills 文件夹，~/.codex、~/.claude 里的登录信息、设置和对话记录不会开放；项目里的 skills 一直可以读。</p>
      <label>允许 AI 运行的本机命令行工具（每行一个命令名，例如 lark-cli、meegle；留空 = 不允许）
        <textarea name="programs" rows="2" placeholder="lark-cli&#10;meegle">${esc(programs.join("\n"))}</textarea></label>
      <p class="muted">程序以你的身份和登录状态运行（比如能读写你的飞书文档），每次运行都要你在 ChatGPT 里确认，并记入审计。shell、解释器（cmd、PowerShell、bash、python、node 等）和常见的能执行别的命令或收发任意文件的工具（git、curl、包管理器、构建和测试工具等）不能加入；清单没法列全，只加你愿意让 AI 以任意参数运行的程序。</p>
      <p class="error" id="roots-error"></p>
      <div id="roots-command"></div>
      <div class="actions"><button type="button" data-close>关闭</button><button class="primary" type="submit">生成命令</button></div>
    </form>`);
  const form = document.getElementById("roots-form");
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      const result = await api(`/api/devices/${device.id}/roots-command`, { method: "POST",
        body: { platform: form.platform.value, roots: splitRoots(form.roots.value), mobile: form.mobile.checked,
          ssh_hosts: splitRoots(form.ssh_hosts.value), skills: form.skills.checked,
          programs: splitRoots(form.programs.value) } });
      document.getElementById("roots-error").textContent = "";
      document.getElementById("roots-command").innerHTML = `<p>在「${esc(device.name)}」上运行：</p>${codeBlock(result.command)}`;
    } catch (error) {
      document.getElementById("roots-error").textContent = error.message;
    }
  });
}

function confirmDeleteDevice(device, uninstallCommand, onDeleted) {
  const folder = devicePlatform(device) === "windows" ? "%USERPROFILE%\\.webcoder-agent" : "~/.webcoder-agent";
  const agentNote = device.paired ? `
    <p>那台电脑上的 Agent 会${device.online ? "立即" : "在下次连上 Hub 时"}自动停止，并取消开机自动运行，不用手动处理。
      程序本身和它保存的修改备份会留在 <code>${esc(folder)}</code>。</p>
    <p class="muted">想把 Agent 彻底删干净（包括修改备份），在那台电脑上运行：</p>${codeBlock(uninstallCommand)}` : "";
  openModal(`<h2 id="delete-title">删除设备「${esc(device.name)}」？</h2>
    <p>Hub 上的这台设备和它的 ${device.projects} 个项目会被删除，AI 立即无法再访问；设备上的文件不会被删除。</p>${agentNote}
    <div class="actions" id="delete-actions"><button type="button" data-close>取消</button>
      <button class="danger-solid" type="button" id="confirm-delete">删除设备</button></div>`);
  document.getElementById("confirm-delete").addEventListener("click", guard(async () => {
    await onDeleted();
    if (!device.paired) { closeModal(); return; }
    // Keep the dialog open so the uninstall command can still be copied.
    document.getElementById("delete-title").textContent = `已删除「${device.name}」`;
    document.getElementById("delete-actions").innerHTML = '<button class="primary" type="button" data-close>完成</button>';
  }));
}

async function viewDevices() {
  app.innerHTML = `<section class="card"><div class="row"><h2>设备</h2>
      <button class="primary" type="button" id="add-device">添加设备</button></div>
    <div id="device-list"><p class="muted">加载中…</p></div></section>`;
  const list = document.getElementById("device-list");
  let devices = [];
  let uninstall = {};
  const draw = async () => {
    ({ devices, uninstall } = await api("/api/devices"));
    list.innerHTML = devices.length ? `<div class="table-wrap"><table><thead><tr><th>名称</th><th>状态</th><th>系统</th>
      <th>Agent</th><th>开放的文件夹</th><th>项目</th><th>最后在线</th><th></th></tr></thead><tbody>
      ${devices.map((d) => `<tr><td>${esc(d.name)}<div class="muted">${esc(d.info.hostname || "")}</div></td>
        <td>${STATUS[d.status] || esc(d.status)}${d.info.read_only ? ' <span class="badge info">只读</span>' : ""}${d.info.mobile?.enabled ? ' <span class="badge warn">手机控制</span>' : ""}${d.info.ssh?.hosts?.length ? ` <span class="badge warn">SSH ${d.info.ssh.hosts.length} 台</span>` : ""}${d.info.skills?.enabled ? ' <span class="badge info">Skills</span>' : ""}${d.info.commands?.programs?.length ? ` <span class="badge warn">命令行 ${d.info.commands.programs.length} 个</span>` : ""}</td>
        <td>${esc(d.info.platform || PLATFORM_NAMES[d.info.setup?.platform] || "—")}</td><td>${esc(d.info.agent_version || "—")}</td>
        <td class="wrap">${(d.info.allowed_roots || d.info.setup?.roots || []).map((root) => `<div><code>${esc(root)}</code></div>`).join("") || "—"}</td>
        <td>${d.projects}</td><td>${d.online ? "现在" : esc(fmtAgo(d.last_seen))}</td>
        <td>${d.paired ? `<button class="small" type="button" data-action="roots" data-id="${esc(d.id)}">修改设置</button>` : ""}
          <button class="small" type="button" data-action="pair" data-id="${esc(d.id)}">${d.paired ? "重新配对" : "安装命令"}</button>
          <button class="small" type="button" data-action="toggle" data-id="${esc(d.id)}" data-enabled="${d.enabled}">${d.enabled ? "停用" : "启用"}</button>
          <button class="small danger" type="button" data-action="delete" data-id="${esc(d.id)}" data-name="${esc(d.name)}">删除</button></td></tr>`).join("")}
      </tbody></table></div>` : `<p class="muted">还没有设备。点击“添加设备”，选择要开放的文件夹，然后在那台电脑上运行生成的命令。</p>`;
  };
  document.getElementById("add-device").addEventListener("click", () => {
    deviceForm("添加设备", "生成安装命令", { platform: "windows", roots: [], read_only: false, autostart: true }, true,
      async (setup) => {
        const result = await api("/api/devices", { method: "POST", body: setup });
        showInstall(setup.name, result);
        await draw();
      });
  });
  list.addEventListener("click", guard(async (event) => {
    const button = event.target.closest("button[data-action]");
    if (!button) return;
    const device = devices.find((d) => d.id === button.dataset.id);
    const { action } = button.dataset;
    if (action === "roots") { rootsForm(device); return; }
    if (action === "pair") {
      const setup = { roots: [], read_only: false, autostart: true, mobile: false, ...(device.info.setup || {}),
        platform: devicePlatform(device) };
      if (device.paired && device.info.allowed_roots) {  // the command applies exactly this: start from what the agent reports
        setup.roots = device.info.allowed_roots;
        setup.read_only = Boolean(device.info.read_only);
        if (device.info.mobile) setup.mobile = Boolean(device.info.mobile.enabled);
      }
      deviceForm(device.paired ? `重新配对「${device.name}」` : `「${device.name}」的安装命令`, "生成安装命令", setup, false,
        async (chosen) => {
          if (device.paired && !confirm(`重新配对后，「${device.name}」上现有的 Agent 需要用新命令重新安装，确定吗？`)) return;
          showInstall(device.name, await api(`/api/devices/${device.id}/pairing-code`, { method: "POST", body: chosen }));
          await draw();
        });
      return;
    }
    if (action === "delete") {
      confirmDeleteDevice(device, uninstall[devicePlatform(device)], async () => {
        await api(`/api/devices/${device.id}`, { method: "DELETE" });
        await draw();
      });
      return;
    }
    if (action === "toggle") {
      await api(`/api/devices/${device.id}`, { method: "PATCH", body: { enabled: button.dataset.enabled !== "true" } });
    }
    await draw();
  }));
  await draw();
  state.poll = setInterval(() => { if (document.getElementById("modal").hidden) draw().catch(() => {}); }, 5000);
}

// ---------------------------------------------------------------- projects
async function browseFolder(deviceId, onPick, path = "") {
  const data = await api(`/api/devices/${deviceId}/browse?path=${encodeURIComponent(path)}`);
  const up = data.parent ? `<button type="button" data-dir="${esc(data.parent)}">⬆ 上一级</button>` : (data.path ? '<button type="button" data-dir="">⬆ 允许的文件夹列表</button>' : "");
  openModal(`<h2>选择文件夹</h2><p class="muted">${data.path ? `当前：<code>${esc(data.path)}</code>` : "这台设备允许访问的文件夹："}</p>
    <div class="dir-list">${up}${data.dirs.map((dir) => `<button type="button" data-dir="${esc(dir.path)}">📁 ${esc(dir.name)}</button>`).join("")
      || '<p class="muted">（没有子文件夹）</p>'}</div>
    <div class="actions"><button data-close type="button">取消</button>
      ${data.path ? `<button class="primary" type="button" id="pick-dir">选择此文件夹</button>` : ""}</div>`);
  document.querySelectorAll(".dir-list [data-dir]").forEach((button) => button.addEventListener("click",
    guard(() => browseFolder(deviceId, onPick, button.dataset.dir))));
  const pick = document.getElementById("pick-dir");
  if (pick) pick.addEventListener("click", () => { onPick(data.path); closeModal(); });
}

async function viewProjects() {
  const [{ projects }, { devices }] = await Promise.all([api("/api/projects"), api("/api/devices")]);
  const usable = devices.filter((d) => d.enabled && d.paired);
  app.innerHTML = `<section class="card"><h2>新建项目</h2>
    ${usable.length ? "" : '<div class="alert info">还没有已配对的设备，请先到「设备」页面添加并配对。</div>'}
    <form id="add-project" class="form"><div class="grid">
      <label>项目名（AI 用它引用项目）<input name="alias" required maxlength="64" pattern="[A-Za-z0-9][A-Za-z0-9._\\-]*" placeholder="例如 web-app"></label>
      <label>设备<select name="device_id" required>${usable.map((d) => `<option value="${esc(d.id)}">${esc(d.name)}${d.online ? "" : "（离线）"}</option>`).join("")}</select></label>
      <label>权限<select name="mode"><option value="rw">读写</option><option value="ro">只读</option></select></label></div>
      <label>文件夹（必须位于设备允许的文件夹之内）<div class="with-button"><input name="root" required placeholder="例如 D:\\code\\web-app 或 /home/me/web-app">
        <button type="button" id="browse">浏览…</button></div></label>
      <label>说明（可选，AI 会看到）<input name="description" maxlength="500"></label>
      <div><button class="primary" type="submit" ${usable.length ? "" : "disabled"}>创建项目</button></div></form></section>
    <section class="card"><h2>项目</h2>${projects.length ? `<div class="table-wrap"><table><thead><tr><th>项目名</th><th>设备</th>
      <th>文件夹</th><th>权限</th><th>说明</th><th></th></tr></thead><tbody>${projects.map((p) => `<tr>
      <td><b>${esc(p.alias)}</b></td><td>${esc(p.device_name)} ${p.online ? '<span class="badge ok">在线</span>' : '<span class="badge">离线</span>'}</td>
      <td class="wrap"><code>${esc(p.root)}</code></td>
      <td><button class="small" type="button" data-action="mode" data-id="${esc(p.id)}" data-mode="${p.mode}">${p.mode === "rw" ? "读写" : "只读"}</button></td>
      <td>${esc(p.description)}</td>
      <td>${state.collaboration ? `<a class="table-link" href="#/collaboration/${encodeURIComponent(p.id)}">协作</a>` : ""}
        <button class="small danger" type="button" data-action="delete" data-id="${esc(p.id)}" data-name="${esc(p.alias)}">删除</button></td></tr>`).join("")}
      </tbody></table></div>` : '<p class="muted">还没有项目。</p>'}</section>`;
  const form = document.getElementById("add-project");
  document.getElementById("browse").addEventListener("click", guard(async () => {
    if (!form.device_id.value) throw new Error("请先选择设备");
    await browseFolder(form.device_id.value, (path) => { form.root.value = path; });
  }));
  form.addEventListener("submit", guard(async (event) => {
    event.preventDefault();
    const data = Object.fromEntries(new FormData(form));
    await api("/api/projects", { method: "POST", body: data });
    toast(`已创建项目 ${data.alias}`);
    await viewProjects();
  }));
  const table = app.querySelector("table");
  if (table) table.addEventListener("click", guard(async (event) => {
    const button = event.target.closest("button[data-action]");
    if (!button) return;
    if (button.dataset.action === "mode") {
      const mode = button.dataset.mode === "rw" ? "ro" : "rw";
      await api(`/api/projects/${button.dataset.id}`, { method: "PATCH", body: { mode } });
    } else if (button.dataset.action === "delete") {
      if (!confirm(`删除项目「${button.dataset.name}」？只删除映射，不会删除设备上的文件。`)) return;
      await api(`/api/projects/${button.dataset.id}`, { method: "DELETE" });
    }
    await viewProjects();
  }));
}

// ---------------------------------------------------------------- tokens
function showToken(result) {
  const s = result.snippets;
  openModal(`<h2>访问令牌已创建</h2><div class="alert warn">令牌只显示这一次，请立即复制保存。</div>${codeBlock(result.token)}
    <h3>ChatGPT 网页版 · Secure MCP Tunnel</h3><p class="muted">把令牌写入文件，供隧道里的桥接程序使用（PowerShell 与 macOS/Linux 二选一），完整步骤见「接入客户端」：</p>
    ${codeBlock(s.tunnel_token_file_windows)}${codeBlock(s.tunnel_token_file_unix)}
    <h3>Codex</h3><p class="muted">先把令牌存到环境变量 WEBCODER_TOKEN（二选一，之后重新打开终端），再添加服务器：</p>
    ${codeBlock(s.codex_token_env_windows)}${codeBlock(s.codex_token_env_unix)}${codeBlock(s.codex_token_cli)}
    <div class="actions"><button data-close class="primary" type="button">我已保存</button></div>`);
}

async function viewTokens() {
  const [{ tokens }, { projects }] = await Promise.all([api("/api/tokens"), api("/api/projects")]);
  app.innerHTML = `<section class="card"><h2>创建访问令牌</h2>
    <p class="muted">令牌用于 Codex 和 Secure MCP Tunnel 桥接这类“自带凭据”的接入方式。ChatGPT 网页版直连（公网 HTTPS）不需要令牌，在 ChatGPT 里走 OAuth 授权即可。</p>
    <form id="add-token" class="form"><div class="grid">
      <label>名称<input name="label" required maxlength="80" placeholder="例如：ChatGPT 隧道 / 笔记本上的 Codex"></label>
      <label>权限<select name="scope"><option value="write">读写</option><option value="read">只读</option></select>
        <span class="check"><input type="checkbox" name="mobile"> 还允许控制手机 / 模拟器（mobile_* 工具）</span>
        <span class="check"><input type="checkbox" name="ssh"> 还允许在服务器上执行命令（ssh_* 工具）</span>
        <span class="check"><input type="checkbox" name="cli"> 还允许在本机运行命令行工具（cli_* 工具，如 lark-cli）</span></label>
      <label>有效期<select name="days"><option value="30">30 天</option><option value="90" selected>90 天</option>
        <option value="180">180 天</option><option value="365">365 天</option></select></label></div>
      <fieldset><legend>可访问的项目</legend>
        <label class="check"><input type="radio" name="scope_projects" value="all" checked> 全部项目（包括以后新建的）</label>
        <label class="check"><input type="radio" name="scope_projects" value="some"> 仅以下项目：</label>
        <div class="project-picks">${projects.map((p) => `<label class="check"><input type="checkbox" name="project" value="${esc(p.id)}">
          ${esc(p.alias)} <span class="muted">（${esc(p.device_name)}）</span></label>`).join("") || '<p class="muted">还没有项目</p>'}</div>
      </fieldset>
      <div><button class="primary" type="submit">创建令牌</button></div></form></section>
    <section class="card"><h2>已授权</h2>${tokens.length ? `<div class="table-wrap"><table><thead><tr><th>名称</th><th>方式</th><th>权限</th>
      <th>项目</th><th>创建</th><th>过期</th><th>最近使用</th><th></th></tr></thead><tbody>${tokens.map((t) => `<tr>
      <td>${esc(t.label)}</td><td>${t.kind === "pat" ? '<span class="badge">令牌</span>' : '<span class="badge info">OAuth</span>'}</td>
      <td>${t.scopes.includes("write") ? '<span class="badge warn">读写</span>' : '<span class="badge ok">只读</span>'}${t.scopes.includes("mobile") ? ' <span class="badge warn">控制手机</span>' : ""}${t.scopes.includes("ssh") ? ' <span class="badge err">服务器命令</span>' : ""}${t.scopes.includes("cli") ? ' <span class="badge err">本机命令行</span>' : ""}</td>
      <td class="wrap">${t.projects === "*" ? "全部" : esc(t.projects.join("、"))}</td>
      <td>${esc(fmtTime(t.created))}</td><td>${t.expires ? esc(fmtTime(t.expires)) : "—"}</td><td>${esc(fmtAgo(t.last_used))}</td>
      <td><button class="small danger" type="button" data-revoke="${esc(t.id)}" data-name="${esc(t.label)}">撤销</button></td></tr>`).join("")}
      </tbody></table></div>` : '<p class="muted">还没有令牌或 OAuth 授权。</p>'}</section>`;
  const form = document.getElementById("add-token");
  form.addEventListener("submit", guard(async (event) => {
    event.preventDefault();
    const data = new FormData(form);
    const some = data.get("scope_projects") === "some";
    const picked = data.getAll("project");
    if (some && !picked.length) throw new Error("请至少勾选一个项目");
    const result = await api("/api/tokens", { method: "POST", body: {
      label: data.get("label"),
      scopes: [...(data.get("scope") === "write" ? ["read", "write"] : ["read"]), ...(data.get("mobile") ? ["mobile"] : []),
        ...(data.get("ssh") ? ["ssh"] : []), ...(data.get("cli") ? ["cli"] : [])],
      projects: some ? picked : "*", days: Number(data.get("days")),
    } });
    showToken(result);  // shown only once: before anything else can fail
    viewTokens().catch(() => {});
  }));
  const table = app.querySelector("table");
  if (table) table.addEventListener("click", guard(async (event) => {
    const button = event.target.closest("button[data-revoke]");
    if (!button || !confirm(`撤销「${button.dataset.name}」？使用它的客户端将立即失去访问权限。`)) return;
    await api(`/api/tokens/${button.dataset.revoke}`, { method: "DELETE" });
    await viewTokens();
  }));
}

// ---------------------------------------------------------------- clients
async function viewClients() {
  const data = await api("/api/clients");
  const s = data.snippets;
  const hostedNote = data.https ? "" : `<div class="alert warn">ChatGPT 直连时由 OpenAI 的服务器访问你的 Hub，需要 Hub 有<b>公网 HTTPS 地址</b>
    （当前是 ${esc(state.session.public_url)}）。没有公网 HTTPS 时，请用旁边的「Secure MCP Tunnel」方式。</div>`;
  const chatgptSteps = `<li>ChatGPT →「设置」→「安全与登录（Security and login）」→ 打开「开发者模式（Developer mode）」</li>
        <li>打开 <code>https://chatgpt.com/plugins</code>，点「+」创建开发者模式应用，填写名称和说明</li>`;
  const panels = {
    "chatgpt-oauth": `${hostedNote}<p class="muted">适合 Hub 部署在有域名和 HTTPS 的服务器上（Agent 仍在你的电脑上）。需要 ChatGPT Plus / Pro / Business / Enterprise / Edu。</p>
      <ol class="steps">${chatgptSteps}
        <li>Connection 选公网地址，填写：</li></ol>${codeBlock(s.chatgpt_url)}
      <ol class="steps" start="4"><li>Authentication 选 <b>OAuth</b>（客户端注册用默认的动态注册），创建后会弹出本面板的授权页：登录 → 选择权限和项目 → 允许</li>
        <li>新建对话，从工具菜单加入这个应用，就可以让 ChatGPT 读写项目里的文件（写操作 ChatGPT 会先请你确认）</li></ol>
      <p class="muted">WEBCODER_PUBLIC_URL 必须与这里的地址完全一致；ChatGPT 的回调地址（chatgpt.com）已内置在白名单中。</p>
      <h3>插件包（ChatGPT 桌面端 / Codex）</h3>
      <div class="alert warn">网页版请用上面的「+」方式。ChatGPT 会把包内声明了 MCP 服务器的上传插件标成「仅限桌面端」，这种插件在网页版里用不了。</div>
      <p class="muted">插件包里写好了本 Hub 的地址，并附带一个教模型“先读取、再按 sha256 修改”安全改文件的技能，适合 ChatGPT 桌面端和 Codex。
        若 ChatGPT 提示需要应用 ID，先按上面的步骤用「+」创建一次，把浏览器地址栏里 <code>plugin_asdk_app_…</code> 那段填到下面再下载。</p>
      <div class="with-button"><input id="plugin-app-id" maxlength="120" placeholder="ChatGPT 应用 ID（可选）">
        <button type="button" class="primary" id="download-plugin">下载插件包</button></div>
      <p class="muted">网页版也想要这个技能：<a href="/api/chatgpt-skill.zip">下载技能包</a>，作为技能（Skill）单独上传，配合「+」创建的应用一起用。</p>`,
    "chatgpt-tunnel": `<p class="muted">适合只在自己电脑上用：Hub 可以只监听 127.0.0.1，由 OpenAI 官方 tunnel-client 主动连出，不需要公网地址和域名。</p>
      <ol class="steps">
        <li>在 <code>platform.openai.com</code> →「Settings → Organization → Tunnels」创建隧道，记下 <code>tunnel_id</code>；再创建一个 Runtime API key（需要 Tunnels Read + Use 权限）</li>
        <li>从 <code>github.com/openai/tunnel-client/releases</code> 下载 tunnel-client（Windows 选 windows-amd64）</li>
        <li>在「访问令牌」创建令牌，并按弹窗提示保存到令牌文件</li>
        <li>在装了 WebCoder Agent 的同一台电脑上运行。Agent 安装脚本把 <code>webcoder-bridge</code> 放进了 PATH：Windows 上新开的终端里即可用；macOS / Linux 上它在 <code>~/.local/bin</code>，这个目录要在 PATH 里：</li></ol>
      ${codeBlock(s.tunnel_key_env_windows)}${codeBlock(s.tunnel_init)}${codeBlock(s.tunnel_check)}${codeBlock(s.tunnel_run)}
      <p class="muted">下一步创建应用时，<code>tunnel-client run</code> 要一直开着：ChatGPT 创建时就会经隧道读取工具列表。</p>
      <ol class="steps" start="5">${chatgptSteps}
        <li>Connection 选 <b>Tunnel</b> 并选中刚才的隧道；Authentication 选 <b>No Authentication</b>（隧道只属于你的组织，令牌由桥接程序携带）</li>
        <li>新建对话，从工具菜单加入这个应用即可</li></ol>`,
    codex: `<h3>方式一：访问令牌</h3><p class="muted">1）把令牌存入环境变量 WEBCODER_TOKEN（二选一，之后重新打开终端）：</p>
      ${codeBlock(s.codex_token_env_windows)}${codeBlock(s.codex_token_env_unix)}
      <p class="muted">2）添加服务器：</p>${codeBlock(s.codex_token_cli)}
      <p class="muted">或直接写入 ~/.codex/config.toml：</p>${codeBlock(s.codex_config_toml)}
      <h3>方式二：OAuth 登录</h3>${codeBlock(s.codex_oauth)}
      <p class="muted">验证：<code>codex mcp list</code>。</p>`,
  };
  const tabs = [["chatgpt-oauth", "ChatGPT 网页版 · 公网 HTTPS"], ["chatgpt-tunnel", "ChatGPT 网页版 · Secure MCP Tunnel"],
    ["codex", "Codex"]];
  app.innerHTML = `<section class="card"><h2>接入客户端</h2>
    <p class="muted">MCP 地址：<code>${esc(s.mcp_url)}</code>　支持的客户端：${data.clients.map((c) => `${esc(c.title)}（${esc(c.surfaces)}）`).join("；")}</p>
    <div class="tabs">${tabs.map(([id, label], index) => `<button type="button" data-tab="${id}" class="${index ? "" : "active"}">${label}</button>`).join("")}</div>
    <div id="tab-body">${panels[tabs[0][0]]}</div></section>`;
  app.querySelector(".tabs").addEventListener("click", (event) => {
    const button = event.target.closest("button[data-tab]");
    if (!button) return;
    app.querySelectorAll(".tabs button").forEach((b) => b.classList.toggle("active", b === button));
    document.getElementById("tab-body").innerHTML = panels[button.dataset.tab];
  });
  document.getElementById("tab-body").addEventListener("click", (event) => {
    if (!event.target.closest("#download-plugin")) return;
    const appId = document.getElementById("plugin-app-id").value.trim();
    if (appId && !/^[A-Za-z0-9_-]{6,120}$/.test(appId)) { toast("应用 ID 只能包含字母、数字、_ 和 -", "err"); return; }
    location.href = `/api/chatgpt-plugin.zip${appId ? `?app_id=${encodeURIComponent(appId)}` : ""}`;
  });
}

// ---------------------------------------------------------------- audit
async function viewAudit() {
  const { entries } = await api("/api/audit?limit=300");
  app.innerHTML = `<section class="card"><div class="row"><h2>审计日志</h2><button type="button" id="refresh">刷新</button></div>
    ${auditTable(entries)}</section>`;
  document.getElementById("refresh").addEventListener("click", guard(viewAudit));
}

// ---------------------------------------------------------------- users (administrators)
function showLink(title, link, hint) {
  openModal(`<h2>${esc(title)}</h2><p>${hint}</p>${codeBlock(link.url)}
    <p class="muted">有效期至 ${esc(fmtTime(link.expires))}，只能使用一次。链接相当于临时密码，请通过私密渠道发送。</p>
    <div class="actions"><button data-close class="primary" type="button">完成</button></div>`);
}

async function viewUsers() {
  const { users, invites } = await api("/api/users");
  const me = state.session.username;
  app.innerHTML = `<div id="users-view"><section class="card"><h2>邀请用户</h2>
    <p class="muted">每个用户各自添加设备、项目和令牌，彼此完全隔离、互相看不到；管理员也看不到别人的文件。
      生成邀请链接发给对方，对方打开后自己设置用户名和密码。</p>
    <form id="invite-form" class="form"><div class="grid">
      <label>备注（可选，例如对方的名字）<input name="note" maxlength="100"></label>
      <label>有效期<select name="days"><option value="1">1 天</option><option value="7" selected>7 天</option>
        <option value="30">30 天</option></select></label></div>
      <div><button class="primary" type="submit">生成邀请链接</button></div></form>
    ${invites.length ? `<h3>未使用的邀请</h3><div class="table-wrap"><table><thead><tr><th>备注</th><th>创建</th><th>过期</th><th></th></tr></thead>
      <tbody>${invites.map((i) => `<tr><td>${esc(i.note) || '<span class="muted">—</span>'}</td><td>${esc(fmtTime(i.created))}</td>
        <td>${esc(fmtTime(i.expires))}</td><td><button class="small danger" type="button" data-invite="${esc(i.id)}">撤销</button></td></tr>`).join("")}
      </tbody></table></div>` : ""}</section>
    <section class="card"><h2>用户</h2><div class="table-wrap"><table><thead><tr><th>用户名</th><th>角色</th><th>状态</th>
      <th>设备</th><th>项目</th><th>注册</th><th>最近登录</th><th></th></tr></thead><tbody>
      ${users.map((u) => `<tr><td><b>${esc(u.username)}</b>${u.username === me ? ' <span class="muted">（你）</span>' : ""}</td>
        <td>${u.role === "admin" ? '<span class="badge info">管理员</span>' : '<span class="badge">普通用户</span>'}</td>
        <td>${u.disabled ? '<span class="badge err">已停用</span>' : '<span class="badge ok">正常</span>'}</td>
        <td>${u.devices}</td><td>${u.projects}</td><td>${esc(fmtTime(u.created))}</td><td>${esc(fmtAgo(u.last_login))}</td>
        <td>${u.username === me ? "" : `<button class="small" type="button" data-user="${esc(u.id)}" data-action="role"
            data-role="${u.role}">${u.role === "admin" ? "设为普通用户" : "设为管理员"}</button>
          <button class="small" type="button" data-user="${esc(u.id)}" data-action="toggle" data-disabled="${u.disabled}">${u.disabled ? "启用" : "停用"}</button>`}
          <button class="small" type="button" data-user="${esc(u.id)}" data-action="reset" data-name="${esc(u.username)}">重置密码</button>
          ${u.username === me ? "" : `<button class="small danger" type="button" data-user="${esc(u.id)}" data-action="delete"
            data-name="${esc(u.username)}">删除</button>`}</td></tr>`).join("")}
      </tbody></table></div></section></div>`;
  document.getElementById("invite-form").addEventListener("submit", guard(async (event) => {
    event.preventDefault();
    const data = new FormData(event.target);
    const link = await api("/api/users/invites", { method: "POST",
      body: { note: data.get("note").trim(), days: Number(data.get("days")) } });
    await viewUsers();
    showLink("邀请链接已生成", link, "把这个链接发给对方，对方打开后设置用户名和密码即可开始使用：");
  }));
  document.getElementById("users-view").addEventListener("click", guard(async (event) => {
    const revoke = event.target.closest("button[data-invite]");
    if (revoke) {
      await api(`/api/users/invites/${revoke.dataset.invite}`, { method: "DELETE" });
      await viewUsers();
      return;
    }
    const button = event.target.closest("button[data-user]");
    if (!button) return;
    const { user: id, action, name } = button.dataset;
    if (action === "role") {
      await api(`/api/users/${id}`, { method: "PATCH", body: { role: button.dataset.role === "admin" ? "user" : "admin" } });
    } else if (action === "toggle") {
      const disable = button.dataset.disabled !== "true";
      if (disable && !confirm("停用后该用户会立即退出登录，其设备断开、令牌和 OAuth 授权失效（启用后恢复）。确定吗？")) return;
      await api(`/api/users/${id}`, { method: "PATCH", body: { disabled: disable } });
    } else if (action === "reset") {
      const link = await api(`/api/users/${id}/reset-link`, { method: "POST" });
      showLink(`「${name}」的密码重置链接`, link, `把链接发给 ${esc(name)}，对方打开后设置新密码（旧密码和已登录的会话随即失效）：`);
      return;
    } else if (action === "delete") {
      if (!confirm(`删除用户「${name}」？其设备、项目、令牌和日志将一并删除，不可恢复；设备上的文件不受影响，`
        + "那些电脑上的 Agent 会自动停止并取消开机自动运行。")) return;
      await api(`/api/users/${id}`, { method: "DELETE" });
    }
    await viewUsers();
  }));
}

// ---------------------------------------------------------------- invite / reset links (no sign-in needed)
function passwordFields() {
  return `<label>密码（至少 10 位）<input name="password" type="password" autocomplete="new-password" minlength="10" maxlength="256" required></label>
    <label>再次输入密码<input name="again" type="password" autocomplete="new-password" minlength="10" maxlength="256" required></label>`;
}

function linkForm(formId, send) {
  const form = document.getElementById(formId);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const errorBox = form.querySelector(".error");
    if (form.password.value !== form.again.value) { errorBox.textContent = "两次输入的密码不一致"; return; }
    try {
      signedIn(await send(form));
    } catch (error) {
      errorBox.textContent = error.message;
    }
  });
}

async function viewInvite(token) {
  const path = `/api/links/invite/${encodeURIComponent(token || "")}`;
  const data = await api(path);
  const current = state.session.authenticated ? `<div class="alert info">你当前以 ${esc(state.session.username)} 登录；注册后将切换到新账号。</div>` : "";
  app.innerHTML = `<section class="card narrow"><h1>加入 WebCoder</h1>${current}
    <p>你收到了一个 WebCoder 邀请${data.note ? `（${esc(data.note)}）` : ""}。设置用户名和密码即可注册，链接在 ${esc(fmtTime(data.expires))} 前有效。</p>
    <p class="muted">注册后，你可以把自己电脑上的文件夹接入 ChatGPT / Codex；你的设备和文件只有你自己能访问。</p>
    <form id="join-form" class="form">
      <label>用户名（2–32 位字母、数字、点、下划线或连字符）<input name="username" autocomplete="username" required
        maxlength="32" pattern="[A-Za-z0-9][A-Za-z0-9._\\-]{1,31}"></label>
      ${passwordFields()}
      <p class="error"></p>
      <button class="primary" type="submit">注册并登录</button>
    </form></section>`;
  linkForm("join-form", (form) => api(path, { method: "POST",
    body: { username: form.username.value.trim(), password: form.password.value } }));
}

async function viewReset(token) {
  const path = `/api/links/reset/${encodeURIComponent(token || "")}`;
  const data = await api(path);
  app.innerHTML = `<section class="card narrow"><h1>重置密码</h1>
    <p>为账号 <b>${esc(data.username)}</b> 设置新密码。链接在 ${esc(fmtTime(data.expires))} 前有效。</p>
    <form id="reset-form" class="form">${passwordFields()}
      <p class="error"></p>
      <button class="primary" type="submit">设置新密码并登录</button>
    </form></section>`;
  linkForm("reset-form", (form) => api(path, { method: "POST", body: { password: form.password.value } }));
}

// ---------------------------------------------------------------- OAuth consent
async function viewConsent(id) {
  const data = await api(`/api/oauth/requests/${encodeURIComponent(id || "")}`);
  showChrome(true, "");
  app.innerHTML = `<section class="card narrow"><h1>授权 ${esc(data.surface)} 访问 WebCoder</h1>
    <p>客户端 <b>${esc(data.client_name)}</b> 请求访问你的项目。授权后将返回：<code>${esc(data.redirect_host)}</code></p>
    <p class="muted">当前登录：<b>${esc(data.username)}</b>，授权只涉及这个账号下的项目。
      <button class="small ghost" type="button" id="switch-user">换个账号</button></p>
    ${data.loopback ? `<div class="alert warn">回调地址是本机地址（${esc(data.redirect_host)}）。请确认这是你刚刚在自己电脑上的 Codex 中发起的连接；如果不是，请点击“拒绝”。</div>` : ""}
    <form id="consent" class="form">
      <fieldset><legend>权限</legend>
        <label class="check"><input type="checkbox" checked disabled> 读取文件（必选）</label>
        ${data.scopes.includes("write") ? '<label class="check"><input type="checkbox" name="write" checked> 修改文件（写入、编辑、删除、移动）</label>' : ""}
        ${data.scopes.includes("mobile") ? `<label class="check"><input type="checkbox" name="mobile"> 控制手机 / 模拟器（读屏幕、点击、输入、安装应用）</label>
          <p class="muted">只对你在「设备」里开启了手机控制的电脑有效。默认不勾选，需要时再允许。</p>` : ""}
        ${data.scopes.includes("ssh") ? `<label class="check"><input type="checkbox" name="ssh"> 在服务器上执行命令（SSH：可以执行任意命令）</label>
          <p class="muted">只能连你在「设备 → 修改设置」里列出的服务器。每条命令 ChatGPT 都会先请你确认。默认不勾选。</p>` : ""}
        ${data.scopes.includes("cli") ? `<label class="check"><input type="checkbox" name="cli"> 在本机运行命令行工具（如 lark-cli、meegle，以你的身份操作）</label>
          <p class="muted">只能运行你在「设备 → 修改设置」里列出的程序。每次运行 ChatGPT 都会先请你确认。默认不勾选。</p>` : ""}
      </fieldset>
      <fieldset><legend>可访问的项目</legend>
        <label class="check"><input type="radio" name="scope_projects" value="all" checked> 全部项目（包括以后新建的）</label>
        <label class="check"><input type="radio" name="scope_projects" value="some"> 仅以下项目：</label>
        <div class="project-picks">${data.projects.map((p) => `<label class="check"><input type="checkbox" name="project" value="${esc(p.id)}">
          ${esc(p.alias)} <span class="muted">（${esc(p.device_name)}）</span></label>`).join("") || '<p class="muted">还没有项目</p>'}</div>
      </fieldset>
      <div class="actions"><button type="button" id="deny">拒绝</button><button class="primary" type="submit">允许</button></div>
    </form></section>`;
  const form = document.getElementById("consent");
  form.addEventListener("submit", guard(async (event) => {
    event.preventDefault();
    const values = new FormData(form);
    const some = values.get("scope_projects") === "some";
    const picked = values.getAll("project");
    if (some && !picked.length) throw new Error("请至少勾选一个项目");
    const result = await api(`/api/oauth/requests/${encodeURIComponent(id)}`, { method: "POST", body: {
      approve: true, projects: some ? picked : "*",
      scopes: ["read", ...(values.get("write") ? ["write"] : []), ...(values.get("mobile") ? ["mobile"] : []),
        ...(values.get("ssh") ? ["ssh"] : []), ...(values.get("cli") ? ["cli"] : [])],
    } });
    app.innerHTML = '<section class="card narrow"><h2>已授权</h2><p>正在返回客户端…可以关闭此页面。</p></section>';
    location.href = result.redirect_to;
  }));
  document.getElementById("switch-user").addEventListener("click", guard(async () => {
    state.view += 1;  // as on logout: nothing still loading is drawn after this
    await api("/api/logout", { method: "POST" });
    state.session = await api("/api/session");
    render();  // shows the sign-in form, then comes back to this consent request
  }));
  document.getElementById("deny").addEventListener("click", guard(async () => {
    const result = await api(`/api/oauth/requests/${encodeURIComponent(id)}`, { method: "POST", body: { approve: false } });
    location.href = result.redirect_to;
  }));
}

// ---------------------------------------------------------------- start
async function init() {
  try {
    state.session = await api("/api/session");
  } catch (error) {
    app.innerHTML = `<div class="alert error">无法连接 Hub：${esc(error.message)}</div>`;
    return;
  }
  document.getElementById("who").addEventListener("click", showAccount);
  document.getElementById("logout").addEventListener("click", guard(async () => {
    state.view += 1;  // pages still loading are not drawn after this
    try { await api("/api/logout", { method: "POST" }); } catch { /* already signed out */ }
    state.session = await api("/api/session");
    location.hash = "#/overview";
    render();
  }));
  window.addEventListener("hashchange", render);
  render();
}

init();
