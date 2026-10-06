"""The extension's service worker (background.js) in node, against a stand-in for the chrome.* APIs: what it does
with tabs that never answer, like the ones Edge puts to sleep."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

BACKGROUND = Path(__file__).resolve().parents[1] / "webcoder" / "hub" / "extension" / "background.js"

# Loads background.js in a vm context with stand-ins: timers run 100x faster; a tab listed in `asleep` never
# answers a message and never settles an injection (what a sleeping tab does), the others answer like the page.
HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
const [, , file, scenario] = process.argv;
const fast = (ms) => Math.max(1, Math.floor(ms / 100));
const never = () => new Promise(() => {});
const asleep = new Set();
const quiet = new Set();  // tabs whose page shows no reply: only the buttons of a card the poll cannot place
const tabs = new Map();
const calls = [];
let nextTab = 100;
const event = () => ({ addListener() {} });
const area = () => ({ get: async () => ({}), set: async () => {}, remove: async () => {} });
const chrome = {
  runtime: { getManifest: () => ({ version: 'test' }), onStartup: event(), onInstalled: event(), onMessage: event(),
             id: 'x', getURL: (p) => 'chrome-extension://x/' + p },
  storage: { session: area(), local: area() },
  action: { setBadgeText: async () => {}, setBadgeBackgroundColor: async () => {} },
  alarms: { create() {}, onAlarm: event() },
  tabs: {
    onRemoved: event(),
    query: async () => [...tabs.values()].filter((t) => t.url.startsWith('https://chatgpt.com/')),
    get: async (id) => { if (!tabs.has(id)) throw new Error('no tab'); return tabs.get(id); },
    sendMessage: (id, message) => {
      calls.push(['message', id, message.cmd]);
      if (asleep.has(id)) return never();
      if (quiet.has(id) && message.cmd === 'poll') {
        return Promise.resolve({ ok: true, url: 'https://chatgpt.com/c/x', text: '', started: false, generating: false,
          complete: false, approvals: [], pending: false, blocked: null, buttons: ['允许', '拒绝'] });
      }
      return Promise.resolve(message.cmd === 'draft' ? { ok: true, draft: false } : { ok: true });
    },
    update: async (id, props) => {
      calls.push(['update', id]);
      if (props.url) tabs.get(id).url = props.url;
      return tabs.get(id);
    },
    create: async (props) => {
      const tab = { id: nextTab++, url: props.url, status: 'complete', discarded: false };
      tabs.set(tab.id, tab);
      calls.push(['create', tab.id]);
      return tab;
    },
    reload: async () => {}, remove: async (id) => { tabs.delete(id); },
  },
  scripting: {
    executeScript: ({ target }) => {
      calls.push(['inject', target.tabId]);
      return asleep.has(target.tabId) ? never() : Promise.resolve([]);
    },
  },
  windows: { create: async () => ({ tabs: [] }) },
};
class FakeSocket { static OPEN = 1; constructor() { this.readyState = 0; } send() {} close() {} }
const realNow = Date.now;
const t0 = realNow();
class FastDate extends Date { static now() { return t0 + (realNow() - t0) * 100; } }  // the clock runs with the timers
const context = vm.createContext({
  chrome, console, URL, WebSocket: FakeSocket, navigator: { userAgent: 'Edg/150 Windows' }, Date: FastDate,
  crypto: { randomUUID: () => 'id' },
  setTimeout: (fn, ms) => setTimeout(fn, fast(ms)), clearTimeout, setInterval: () => 0,
});
vm.runInContext(fs.readFileSync(file, 'utf8'), context, { filename: 'background.js' });
const run = (code) => vm.runInContext(code, context);
(async () => {
  await run('ownedLoaded');
  const started = Date.now();
  let result;
  if (scenario === 'draft-asleep') {
    tabs.set(7, { id: 7, url: 'https://chatgpt.com/c/old', status: 'complete', discarded: false });
    asleep.add(7);
    result = await run('mayHoldDraft({ id: 7, discarded: false })');
  } else if (scenario === 'draft-frozen') {
    tabs.set(8, { id: 8, url: 'https://chatgpt.com/c/old', status: 'complete', discarded: false, frozen: true });
    result = await run('mayHoldDraft({ id: 8, discarded: false, frozen: true })');
  } else if (scenario === 'open-past-a-sleeping-spare') {
    tabs.set(9, { id: 9, url: 'https://chatgpt.com/c/old', status: 'complete', discarded: false });
    asleep.add(9);
    run('owned[9] = 1');
    const opened = await run("openTab({ id: 'j', conversation: null, auto_approve: true })");
    result = { tab: opened.tab.id, owned: opened.owned, spareStillOurs: run('9 in owned') };
  } else if (scenario === 'content-asleep') {
    tabs.set(11, { id: 11, url: 'https://chatgpt.com/', status: 'complete', discarded: false });
    asleep.add(11);
    result = await run('ensureContent(11).then(() => "ok", (e) => e.code)');
  } else if (scenario === 'no-reply') {
    tabs.set(12, { id: 12, url: 'https://chatgpt.com/c/x', status: 'complete', discarded: false });
    quiet.add(12);
    result = await run("follow({ id: 'j', app: 'WebCoder', timeout: 3600 }, { cancelled: false }, 12, () => {})" +
      ".then(() => 'done', (e) => e.code + ': ' + e.message)");
  }
  console.log(JSON.stringify({ result, calls, ms: Date.now() - started }));
  process.exit(0);
})();
setTimeout(() => { console.log(JSON.stringify({ hung: true, calls })); process.exit(0); }, 5000);
"""


def run_scenario(tmp_path: Path, scenario: str, background: Path = BACKGROUND) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    harness = tmp_path / "harness.js"
    harness.write_text(HARNESS, encoding="utf-8")
    done = subprocess.run([node, str(harness), str(background), scenario], capture_output=True, text=True,
                          encoding="utf-8", timeout=60)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_a_sleeping_tab_is_taken_to_hold_a_draft_without_waiting_forever(tmp_path):
    outcome = run_scenario(tmp_path, "draft-asleep")
    assert not outcome.get("hung"), outcome["calls"]
    assert outcome["result"] is True  # it could not tell: left alone
    assert ["inject", 7] in outcome["calls"]


def test_a_tab_the_browser_froze_is_left_alone_without_asking(tmp_path):
    outcome = run_scenario(tmp_path, "draft-frozen")
    assert outcome["result"] is True and outcome["calls"] == []


def test_a_task_opens_a_new_tab_when_its_spare_is_asleep(tmp_path):
    """What hung a real review: the spare tab from two days before was asleep, and nothing else happened."""
    outcome = run_scenario(tmp_path, "open-past-a-sleeping-spare")
    assert not outcome.get("hung"), outcome["calls"]
    assert outcome["result"]["tab"] != 9 and outcome["result"]["owned"] is True
    assert outcome["result"]["spareStillOurs"] is False  # it may hold the person's draft: theirs now
    assert ["update", 9] not in outcome["calls"]  # never navigated away from


def test_injecting_into_a_sleeping_tab_ends_in_an_error_not_a_hang(tmp_path):
    outcome = run_scenario(tmp_path, "content-asleep")
    assert not outcome.get("hung"), outcome["calls"]
    assert outcome["result"] == "NO_CONTENT_SCRIPT"


def test_giving_up_on_a_silent_page_names_what_it_shows(tmp_path):
    outcome = run_scenario(tmp_path, "no-reply")
    assert not outcome.get("hung"), outcome["calls"][-3:]
    assert outcome["result"].startswith("NO_REPLY: ChatGPT 三分钟内没有开始回复")
    assert "页面上有按钮：允许、拒绝" in outcome["result"]
