// WebCoder extension, service worker: keeps a WebSocket to the WebCoder hub and runs the tasks it hands over
// (from `webcoder` on the command line) in chatgpt.com tabs, reporting the reply back as it streams.
//
// The socket carries its credential as a subprotocol: ["webcoder.v1", "token.<token>"], or "pair.<code>" once to
// link this browser. A ping every 20 s keeps the socket, and so this service worker, alive (Chrome 116+); an
// alarm brings it back if Chrome stopped it anyway. Timing (polling the page) runs here, not in the page: a
// background tab's timers are throttled, this worker's are not.
'use strict';

const VERSION = chrome.runtime.getManifest().version;
const PROTOCOL = 'webcoder.v1';
const POLL_MS = 1000;
const CHATGPT = 'https://chatgpt.com/';

let socket = null;
let backoff = 1000;
let reconnectTimer = null;
let status = { state: 'idle', detail: '' };
const jobs = new Map();  // job id -> {cancelled, tabId}

class JobError extends Error {
  constructor(code, message) { super(message); this.code = code; }
}
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
// A tab the browser put to sleep (Edge's sleeping tabs) never runs what is injected into it, and the call never
// settles: anything that waits on a tab gets a time limit.
const bounded = (promise, ms) => Promise.race([promise.catch(() => {}), sleep(ms)]);
const injectContent = (tabId) => bounded(chrome.scripting.executeScript({ target: { tabId }, files: ['content.js'] }),
  3000);

// ---------------------------------------------------------------- status shown in the popup and the badge
function setStatus(state, detail = '') {
  status = { state, detail, at: Date.now() };
  chrome.storage.session.set({ status }).catch(() => {});
  badge();
}
function badge() {
  const running = jobs.size;
  const text = running ? String(running) : status.state === 'online' ? '' : status.state === 'unlinked' ? '?' : '!';
  chrome.action.setBadgeText({ text }).catch(() => {});
  chrome.action.setBadgeBackgroundColor({ color: running ? '#2563eb' : '#b45309' }).catch(() => {});
}

function hubSocketUrl(hub) {
  const url = new URL(hub);
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
  url.pathname = '/browser/ws';
  url.search = '';
  return url.href;
}
function normalizeHub(text) {
  const url = new URL(String(text || '').trim());
  if (!['http:', 'https:'].includes(url.protocol)) throw new Error('Hub 地址要以 https:// 或 http:// 开头');
  return url.origin;
}
function browserName() {
  const ua = navigator.userAgent;
  const name = /Edg\//.test(ua) ? 'Edge' : /OPR\//.test(ua) ? 'Opera' : /Chrome\//.test(ua) ? 'Chrome' : 'Browser';
  const os = /Windows/.test(ua) ? 'Windows' : /Mac OS/.test(ua) ? 'macOS' : /Linux/.test(ua) ? 'Linux' : '';
  return os ? `${name} · ${os}` : name;
}

// ---------------------------------------------------------------- the hub connection
function send(message) {
  if (socket && socket.readyState === WebSocket.OPEN) {
    socket.send(JSON.stringify(message));
    return true;
  }
  return false;
}

// Task events the hub has not had yet (sent while the socket was down), in order. Kept in session storage too, so
// a restarted service worker still delivers them; the hub keeps a task waiting for its browser for a while. A
// task's final event (done / error) stays until the hub acknowledges it: a message handed to a socket that then
// drops is lost, and the reply with it.
const MAX_OUTBOX = 2000;
let outbox = [];
let socketId = '';  // which connection an outbox entry was last sent on (random: unique across restarts too)
const outboxLoaded = chrome.storage.session.get('outbox').then((stored) => {
  outbox = [...(stored.outbox || []), ...outbox];
}).catch(() => {});
function saveOutbox() { chrome.storage.session.set({ outbox }).catch(() => {}); }
const isFinal = (event) => event.type === 'done' || event.type === 'error';
function deliver(jobId, event) {
  const message = { t: 'event', job: jobId, event };
  if (!isFinal(event)) {
    if (!outbox.length && send(message)) return;
    if (outbox.length >= MAX_OUTBOX && (event.type === 'delta' || event.type === 'text' || event.type === 'status')) {
      return;  // the final event carries the whole reply anyway
    }
  }
  outbox.push(message);
  flushOutbox();
}
function flushOutbox() {
  const keep = [];
  for (let i = 0; i < outbox.length; i++) {
    const item = outbox[i];
    if (item.sentOn === socketId) { keep.push(item); continue; }  // a final one, waiting for its ack
    if (!send({ t: 'event', job: item.job, event: item.event })) { keep.push(...outbox.slice(i)); break; }
    if (isFinal(item.event)) keep.push({ ...item, sentOn: socketId });
  }
  outbox = keep;
  saveOutbox();
}
function acknowledged(jobId) {
  outbox = outbox.filter((item) => !(item.job === jobId && isFinal(item.event)));
  saveOutbox();
}
function knownJobs() {  // running here, or with events still to deliver
  return [...new Set([...jobs.keys(), ...outbox.map((message) => message.job)])];
}
// The link is gone for good (revoked, account disabled): stop every task, drop what was still to deliver.
function abandonAll() {
  for (const id of [...jobs.keys()]) cancelJob(id);
  outbox = [];
  saveOutbox();
}

function scheduleReconnect() {
  if (reconnectTimer) return;
  const delay = backoff;
  backoff = Math.min(backoff * 2, 30000);
  reconnectTimer = setTimeout(() => { reconnectTimer = null; connect(); }, delay);
}

async function connect() {
  if (socket && socket.readyState <= WebSocket.OPEN) return;
  await outboxLoaded;
  if (socket && socket.readyState <= WebSocket.OPEN) return;
  const { hubUrl, token, name } = await chrome.storage.local.get(['hubUrl', 'token', 'name']);
  if (!hubUrl || !token) { setStatus('unlinked', '还没有连接到 WebCoder Hub'); return; }
  let ws;
  try {
    ws = new WebSocket(hubSocketUrl(hubUrl), [PROTOCOL, 'token.' + token]);
  } catch (error) {
    setStatus('error', String(error.message || error));
    return;
  }
  socket = ws;
  socketId = crypto.randomUUID();
  setStatus('connecting', hubUrl);
  ws.onopen = () => {
    backoff = 1000;
    ws.send(JSON.stringify({ t: 'hello', name: name || browserName(), version: VERSION, running: knownJobs() }));
  };
  ws.onmessage = (event) => {
    let message;
    try { message = JSON.parse(event.data); } catch { return; }
    onMessage(message, hubUrl);
  };
  ws.onclose = (event) => {
    if (socket !== ws) return;  // replaced (linked again) or closed on purpose (unlinked): not news
    socket = null;
    if (event.code === 4401) {  // revoked or unknown: linking again is the only way back
      chrome.storage.local.remove('token');
      abandonAll();
      setStatus('unlinked', '这个浏览器的连接已失效，请重新运行 webc link 连接');
      return;
    }
    if (event.code === 4403) {  // the account was disabled: its tasks stop
      abandonAll();
      setStatus('error', 'Hub 上的账号已被停用');
      scheduleReconnect();
      return;
    }
    if (event.code === 4409) { setStatus('offline', '另一个连接取代了这个连接'); return; }
    setStatus('offline', `与 Hub 的连接断开（${event.code}），稍后自动重连`);
    scheduleReconnect();
  };
  ws.onerror = () => {};
}

function onMessage(message, hubUrl) {
  if (message.t === 'welcome') {
    setStatus('online', `${hubUrl} · 账号 ${message.user}`);
    chrome.storage.local.set({ linkId: message.link_id });
    flushOutbox();
  } else if (message.t === 'job' && message.job && message.job.id) {
    runJob(message.job);
  } else if (message.t === 'cancel') {
    cancelJob(message.job);
  } else if (message.t === 'ack') {
    acknowledged(message.job);
  }
}

setInterval(() => { send({ t: 'ping' }); }, 20000);
chrome.alarms.create('webcoder-keepalive', { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener((alarm) => { if (alarm.name === 'webcoder-keepalive') connect(); });
chrome.runtime.onStartup.addListener(connect);
chrome.runtime.onInstalled.addListener(connect);
connect();

// ---------------------------------------------------------------- linking (from the popup)
function pair(hubText, codeText) {
  return new Promise((resolve) => {
    let hubUrl;
    try { hubUrl = normalizeHub(hubText); } catch (error) { resolve({ ok: false, error: error.message }); return; }
    const code = String(codeText || '').trim().toUpperCase();
    if (!/^[0-9A-Z-]{4,20}$/.test(code)) { resolve({ ok: false, error: '连接码格式不对（形如 ABCD-EFGH）' }); return; }
    let settled = false;
    const finish = (result) => { if (!settled) { settled = true; clearTimeout(timer); resolve(result); } };
    let ws;
    try { ws = new WebSocket(hubSocketUrl(hubUrl), [PROTOCOL, 'pair.' + code]); } catch (error) {
      resolve({ ok: false, error: String(error.message || error) });
      return;
    }
    const timer = setTimeout(() => { ws.close(); finish({ ok: false, error: '连接 Hub 超时' }); }, 15000);
    let paired = false;  // the hub closes right after handing over the token: that close is no failure
    ws.onmessage = async (event) => {
      let message;
      try { message = JSON.parse(event.data); } catch { return; }
      if (message.t !== 'paired' || !message.token) return;
      paired = true;
      await chrome.storage.local.set({ hubUrl, token: message.token, linkId: message.link_id });
      ws.close();
      if (socket) { const old = socket; socket = null; old.close(); }
      backoff = 1000;
      connect();
      finish({ ok: true });
    };
    ws.onclose = (event) => paired || finish({ ok: false, error: event.code === 4401 ? '连接码无效、已用过或已过期：重新运行 webc link'
      : `连不上 Hub（${event.code}）：检查地址，或者 Hub 还没有更新到支持 WebCoder 扩展的版本` });
  });
}

chrome.runtime.onMessage.addListener((message, sender, respond) => {
  // Only this extension's own pages (the popup, or the options page in a tab), never a content script.
  if (sender.id !== chrome.runtime.id || !String(sender.url || '').startsWith(chrome.runtime.getURL(''))) return false;
  if (message.cmd === 'status') {
    chrome.storage.local.get(['hubUrl', 'name', 'linkId', 'token']).then((stored) => respond({
      status, hubUrl: stored.hubUrl || '', name: stored.name || '', defaultName: browserName(),
      linked: !!stored.token, running: [...jobs.keys()], version: VERSION }));
    return true;
  }
  if (message.cmd === 'pair') { pair(message.hubUrl, message.code).then(respond); return true; }
  if (message.cmd === 'rename') {
    chrome.storage.local.set({ name: String(message.name || '').slice(0, 80) }).then(() => {
      if (socket) socket.close();  // say hello again with the new name
      respond({ ok: true });
    });
    return true;
  }
  if (message.cmd === 'unlink') {
    chrome.storage.local.remove(['token', 'linkId']).then(() => {
      if (socket) { const old = socket; socket = null; old.close(); }
      abandonAll();
      setStatus('unlinked', '已断开');
      respond({ ok: true });
    });
    return true;
  }
  return false;
});

// ---------------------------------------------------------------- tasks
function tabCall(tabId, message, timeout) {
  return Promise.race([
    chrome.tabs.sendMessage(tabId, message),
    sleep(timeout).then(() => { throw new JobError('PAGE_TIMEOUT', 'ChatGPT 页面没有响应'); }),
  ]);
}

async function tabAlive(tabId) {
  try { await chrome.tabs.get(tabId); return true; } catch { return false; }
}

async function waitLoaded(tabId, timeout) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    const tab = await chrome.tabs.get(tabId);
    if (tab.status === 'complete') return tab;
    await sleep(300);
  }
  throw new JobError('PAGE_TIMEOUT', 'ChatGPT 页面加载超时');
}

async function ensureContent(tabId) {
  for (let attempt = 0; attempt < 10; attempt++) {
    try {
      const answer = await tabCall(tabId, { cmd: 'ping' }, 3000);
      if (answer && answer.ok) return;
    } catch { /* not there yet */ }
    if (attempt === 1) {  // a tab opened before the extension was installed or reloaded
      await injectContent(tabId);
    }
    await sleep(700);
  }
  throw new JobError('NO_CONTENT_SCRIPT', '无法在 ChatGPT 页面里运行扩展脚本');
}

function samePath(a, b) {
  try { return new URL(a).pathname.replace(/\/$/, '') === new URL(b).pathname.replace(/\/$/, ''); } catch { return false; }
}

// Tabs this extension opened, reused for later tasks (at most MAX_IDLE_TABS kept waiting) so they do not pile
// up; a tab the person opened is only ever used when it already shows the task's conversation. A tab is taken
// (busyTabs) in the same step that picks it, before anything is awaited, so two tasks never share one. The map
// of owned tabs lives in memory (one object, changed in place) and is copied to session storage.
const MAX_IDLE_TABS = 3;
const busyTabs = new Set();
let owned = null;  // tab id -> last used (ms)
const ownedLoaded = chrome.storage.session.get('owned').then((stored) => { owned = stored.owned || {}; })
  .catch(() => { owned = {}; });
function saveOwned() { chrome.storage.session.set({ owned }).catch(() => {}); }
chrome.tabs.onRemoved.addListener(async (tabId) => {
  await ownedLoaded;
  if (tabId in owned) { delete owned[tabId]; saveOwned(); }
});

async function releaseTab(tabId) {
  await ownedLoaded;
  busyTabs.delete(tabId);
  if (!(tabId in owned)) return;
  owned[tabId] = Date.now();
  saveOwned();
  // Beyond MAX_IDLE_TABS idle tabs of ours, the least recently used close: each taken first (so no task picks it
  // meanwhile) and only when it holds no draft of the person's; one that does is theirs from now on.
  const idle = Object.keys(owned).map(Number).filter((id) => !busyTabs.has(id)).sort((a, b) => owned[a] - owned[b]);
  for (const id of idle.slice(0, Math.max(0, idle.length - MAX_IDLE_TABS))) {
    if (busyTabs.has(id) || !(id in owned)) continue;
    busyTabs.add(id);
    try {
      const tab = await chrome.tabs.get(id).catch(() => null);
      if (tab && await mayHoldDraft(tab)) { adoptTab(id); continue; }
      delete owned[id];
      saveOwned();
      if (tab) await chrome.tabs.remove(id).catch(() => {});
    } finally {
      busyTabs.delete(id);
    }
  }
}

// The person typed into a tab the extension opened: from now on it is theirs (never reused, never closed).
function adoptTab(tabId) {
  if (owned && tabId in owned) { delete owned[tabId]; saveOwned(); }
}

// Whether the tab may hold unsent text the person typed, asked before navigating, reloading or closing it (which
// would lose it). Anything short of the page saying "no" counts as yes: a discarded tab, one still loading, one
// asleep, one that does not answer is left alone.
async function mayHoldDraft(tab) {
  if (tab.discarded || tab.frozen) return true;
  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      const answer = await tabCall(tab.id, { cmd: 'draft' }, 3000);
      if (answer && answer.ok) return answer.draft !== false;  // null: the page could not tell
    } catch { /* no content script there yet */ }
    await injectContent(tab.id);
  }
  return true;
}

// {tab, owned}. fresh: never the person's own tab (it holds a draft), always one of the extension's; avoid: a
// tab not to use. A tab is taken (busyTabs) before anything is awaited; one found holding the person's draft is
// given up (and becomes theirs) instead of being navigated or reloaded.
async function openTab(job, { fresh = false, avoid = null } = {}) {
  await ownedLoaded;
  const tabs = (await chrome.tabs.query({ url: CHATGPT + '*' })).filter((tab) => tab.id !== avoid);
  const url = new URL(job.conversation || CHATGPT);
  if (!job.conversation && job.model) url.searchParams.set('model', job.model);
  // With --confirm, approvals remembered in a conversation must not carry over: ChatGPT forgets them when the
  // page loads again, so the conversation is reloaded (a tab of ours) or opened anew (never the person's tab).
  const manual = job.auto_approve === false;
  const matching = job.conversation ? tabs.filter((tab) => tab.url && samePath(tab.url, job.conversation) &&
    (tab.id in owned || (!fresh && !manual))) : [];
  for (const found of matching) {
    if (busyTabs.has(found.id)) continue;
    busyTabs.add(found.id);
    try {
      if (found.discarded || manual) {
        if (await mayHoldDraft(found)) { busyTabs.delete(found.id); adoptTab(found.id); continue; }
        await chrome.tabs.reload(found.id);
        await sleep(800);
      }
    } catch (error) { busyTabs.delete(found.id); throw error; }
    return { tab: found, owned: found.id in owned };
  }
  const spares = tabs.filter((tab) => tab.id in owned).sort((a, b) => owned[b.id] - owned[a.id]);
  for (const spare of spares) {
    if (busyTabs.has(spare.id)) continue;
    busyTabs.add(spare.id);
    try {
      if (await mayHoldDraft(spare)) { busyTabs.delete(spare.id); adoptTab(spare.id); continue; }
      owned[spare.id] = Date.now();
      const tab = await chrome.tabs.update(spare.id, { url: url.href });
      await sleep(800);  // let the navigation start: the old page still reports "complete" for a moment
      saveOwned();
      return { tab, owned: true };
    } catch (error) { busyTabs.delete(spare.id); throw error; }
  }
  let tab;
  try {
    tab = await chrome.tabs.create({ url: url.href, active: false });
  } catch {  // no browser window to put it in
    const win = await chrome.windows.create({ url: url.href, focused: false });
    tab = win.tabs[0];
  }
  busyTabs.add(tab.id);
  owned[tab.id] = Date.now();
  saveOwned();
  return { tab, owned: true };
}

function cancelJob(id) {
  const run = jobs.get(id);
  if (!run) return;
  run.cancelled = true;
  if (run.tabId != null) {
    tabCall(run.tabId, { cmd: 'abort' }, 5000).catch(() => {});  // a submit under way stops before it sends
    tabCall(run.tabId, { cmd: 'stop' }, 5000).catch(() => {});
  }
}

// After a cancel, a timeout or an unclear submit: stop the generation, which may only start a moment after the
// message went out. Idle twice in a row, a few seconds apart, counts as stopped.
async function stopGeneration(tabId) {
  let idle = 0;
  for (let attempt = 0; attempt < 20 && idle < 2; attempt++) {
    const stopped = await tabCall(tabId, { cmd: 'stop' }, 5000).catch(() => null);
    const state = await tabCall(tabId, { cmd: 'poll', app: '', autoApprove: false }, 5000).catch(() => null);
    idle = state && !state.generating && !(stopped && stopped.stopped) ? idle + 1 : 0;
    await sleep(1500);
  }
}

async function prepare(tab, run) {
  run.tabId = tab.id;
  chrome.tabs.update(tab.id, { autoDiscardable: false }).catch(() => {});
  await waitLoaded(tab.id, 60000);
  await ensureContent(tab.id);
}

async function runJob(job) {
  if (jobs.has(job.id)) return;
  const run = { cancelled: false, tabId: null, sent: false };
  jobs.set(job.id, run);
  badge();
  const emit = (event) => deliver(job.id, event);
  const submit = (owned) => {
    run.sent = true;  // until the page answers otherwise: the message may go out even if its answer never comes
    return tabCall(run.tabId, { cmd: 'submit', prompt: job.prompt, app: job.app || '',
      newChat: !job.conversation, owned }, 120000).then((answer) => {
      if (answer && !answer.ok && answer.code !== 'SEND_FAILED') run.sent = false;  // stopped before sending
      return answer;
    });
  };
  try {
    emit({ type: 'status', text: job.conversation ? '打开 ChatGPT 会话' : '打开新的 ChatGPT 会话' });
    let opened = await openTab(job);
    await prepare(opened.tab, run);
    if (run.cancelled) return;
    emit({ type: 'status', text: job.app ? `在输入框里 @${job.app} 并输入任务` : '输入任务' });
    let submitted = await submit(opened.owned);
    if (submitted && submitted.code === 'DRAFT_PRESENT') {
      // Someone's unsent text is in this tab: leave it (and the tab) to them, send from another tab of our own.
      emit({ type: 'warning', text: '这个会话的标签页里有还没发送的文字，没有动它，改在另一个标签页里发送' });
      adoptTab(opened.tab.id);
      await releaseTab(opened.tab.id);
      opened = await openTab(job, { fresh: true, avoid: opened.tab.id });
      await prepare(opened.tab, run);
      if (run.cancelled) return;
      submitted = await submit(true);
    }
    if (run.cancelled) throw new JobError('CANCELLED', '已取消');
    if (!submitted || !submitted.ok) {
      throw new JobError((submitted && submitted.code) || 'SUBMIT_FAILED', (submitted && submitted.error) || '发送失败');
    }
    for (const text of submitted.warnings || []) emit({ type: 'warning', text });
    emit({ type: 'status', text: '已发送，等待 ChatGPT 回复' });
    const result = await follow(job, run, run.tabId, emit);
    emit({ type: 'done', text: result.text, url: result.url });
  } catch (error) {
    // However it ended (cancelled, timed out, the page misbehaved ...): nothing more may happen in that tab for
    // this task before it is free for the next one; no approval, no generation going on.
    if (run.tabId != null) {
      await tabCall(run.tabId, { cmd: 'abort' }, 5000).catch(() => {});
      if (run.sent) await stopGeneration(run.tabId).catch(() => {});
    }
    if (!run.cancelled) emit({ type: 'error', code: error.code || 'EXTENSION_ERROR', text: String(error.message || error) });
  } finally {
    jobs.delete(job.id);
    if (run.tabId != null) await releaseTab(run.tabId).catch(() => {});
    badge();
  }
}

function conversationUrl(href) {
  try {
    const url = new URL(href);
    return /\/c\/[\w-]+/.test(url.pathname) ? url.origin + url.pathname : '';
  } catch { return ''; }
}

async function follow(job, run, tabId, emit) {
  const deadline = Date.now() + (job.timeout || 3600) * 1000;
  let text = '';
  let url = '';
  let model = '';
  let seenModel = '';
  let calm = 0;
  let waitingSince = Date.now();
  let warnedBlocked = '';
  for (;;) {
    if (Date.now() > deadline) throw new JobError('TIMEOUT', `超过 ${job.timeout} 秒还没有完成`);
    await sleep(POLL_MS);
    if (run.cancelled) throw new JobError('CANCELLED', '已取消');  // checked after the wait: no poll approves after it
    let state;
    try {
      state = await tabCall(tabId, { cmd: 'poll', app: job.app || '', autoApprove: job.auto_approve !== false }, 15000);
    } catch (error) {
      if (!(await tabAlive(tabId))) throw new JobError('TAB_CLOSED', 'ChatGPT 标签页被关闭了');
      await ensureContent(tabId).catch(() => {});  // it navigated or reloaded
      continue;
    }
    if (!state || !state.ok) continue;
    const here = conversationUrl(state.url);
    if (here && here !== url) { url = here; emit({ type: 'conversation', url, title: state.title || '' }); }
    // The model picker's label settles a moment after the page loads: report it once it reads the same twice.
    if (state.model && state.model !== model) {
      if (state.model === seenModel) { model = state.model; emit({ type: 'model', text: model }); }
      seenModel = state.model;
    }
    for (const item of state.approvals || []) {
      if (item.decision === 'ignored') {
        emit({ type: 'warning', text: `ChatGPT 页面上出现「${item.text}」按钮，它不是工具确认，没有点击` });
      } else if (item.decision === 'foreign') {
        emit({ type: 'warning', text: `有一个工具确认（${item.title || item.text}）不能确定是 ${job.app || 'WebCoder'} 的，` +
          '没有自动批准：请到浏览器里确认或拒绝，任务会等你处理完' });
      } else {
        emit({ type: 'approval', tool: item.tool, text: item.text, decision: item.decision });
      }
    }
    if (state.text !== text) {
      emit(state.text.startsWith(text) ? { type: 'delta', text: state.text.slice(text.length) } : { type: 'text', text: state.text });
      text = state.text;
      waitingSince = Date.now();
    }
    if (state.blocked && state.blocked !== warnedBlocked) {
      warnedBlocked = state.blocked;
      emit({ type: 'warning', text: `ChatGPT 页面上有弹窗：${state.blocked}` });
    }
    const busy = state.generating || state.pending;
    if (busy) waitingSince = Date.now();
    calm = state.started && !busy && state.complete ? calm + 1 : 0;
    if (calm >= 2) return { text, url };
    if (!state.started && Date.now() - waitingSince > 180000) {
      const shown = (state.buttons || []).join('、');
      throw new JobError('NO_REPLY', 'ChatGPT 三分钟内没有开始回复（看一下浏览器里的那个标签页' +
        (shown ? `；页面上有按钮：${shown}` : '') + (state.blocked ? `；弹窗：${state.blocked}` : '') + '）');
    }
  }
}
