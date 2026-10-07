"""The extension's content script against a stand-in for chatgpt.com (Playwright's dedicated headless shell).

The stand-in has what the script relies on: a ProseMirror-like composer in form[data-chatgpt-composer], an "@"
menu that turns the app into a chip, turns as li[data-message-role], a stop button and data-message-streaming
while it answers, a write-tool confirmation card with a "remember" box, and a code block with its language
label. What ChatGPT really shows is checked by hand against the live site (see README); this keeps the logic."""
from __future__ import annotations

from pathlib import Path

import pytest

CONTENT = Path(__file__).resolve().parents[1] / "webcoder" / "hub" / "extension" / "content.js"

PAGE = r"""<!doctype html><html><body>
<main><ul id="turns"></ul></main>
<form data-chatgpt-composer id="composer">
  <div id="prompt-textarea" class="ProseMirror" contenteditable="true" role="textbox" style="min-height:20px"></div>
  <button type="button" aria-label="选择 ChatGPT 模型">Thinking</button>
  <button type="submit" aria-label="发送" id="send" disabled>Send</button>
</form>
<script>
// As chatgpt.com in 2026-10: "@" opens [data-composer-overlay-floating-ui] with rows
// button[data-list-navigation-item]; picking one puts an inline span[app-mention-name] into the editor.
const input = document.getElementById('prompt-textarea');
const send = document.getElementById('send');
const form = document.getElementById('composer');
const turns = document.getElementById('turns');
const chip = () => input.querySelector('[app-mention-name]');
const text = () => { const c = chip(); return input.innerText.replace(c ? c.innerText : '\u0000', ''); };
window.sent = [];
function closeMenu() {
  const old = document.querySelector('[data-composer-overlay-floating-ui]');
  if (old) old.remove();
}
input.addEventListener('input', () => {
  send.disabled = !!window.lockSend || !text().trim();
  const m = /@(\w*)$/.exec(input.innerText.trim());
  closeMenu();
  if (!m) return;
  const rows = ['WebCoder', 'WebCoder Test', 'Canva'].filter((a) => a.toLowerCase().startsWith(m[1].toLowerCase()))
    .map((a) => `<button type="button" data-list-navigation-item="true"><div data-menu-row-content="true">` +
      `<span><span>${a}</span><span>${a} plugin</span></span></div></button>`).join('<br>');
  document.body.insertAdjacentHTML('beforeend', '<div data-composer-overlay-floating-ui="true">' +
    '<div data-mention-section-id="plugins"><div>插件</div>' +
    `<div data-mention-section-items="true">${rows}</div></div></div>`);
  document.querySelector('[data-composer-overlay-floating-ui]').addEventListener('click', (e) => {
    const row = e.target.closest('button[data-list-navigation-item]');
    if (!row) return;
    const name = row.querySelector('span span').innerText;
    input.textContent = input.innerText.replace(/@\w*\s*$/, '');
    input.insertAdjacentHTML('beforeend', `<span app-mention-name="${name.toLowerCase()}" ` +
      `app-mention-display-name="${name}" app-mention-path="app://asdk_app_${name.replace(/\W/g, '')}1" ` +
      `contenteditable="false">${name}</span> `);
    closeMenu();
  });
});
send.addEventListener('click', (e) => { e.preventDefault(); if (!window.ignoreSend) answer(); });
document.addEventListener('click', (e) => {  // the person denies a card that is not WebCoder's
  const deny = e.target.closest('.card button');
  if (deny && deny.innerText === 'Deny' && deny.id !== 'deny') deny.closest('.card').remove();
});
input.addEventListener('keydown', (e) => {
  if (e.key === 'Enter') { window.enters = (window.enters || 0) + 1; answer(); }
});
function answer() {
  const asked = text().trim();
  if (!asked) return;
  const app = !!chip();
  window.sent.push({ text: asked, app });
  turns.insertAdjacentHTML('beforeend', '<li data-message-role="user"><div></div></li>');
  turns.lastElementChild.firstChild.textContent = asked;
  input.innerHTML = ''; send.disabled = true; send.hidden = true;
  form.insertAdjacentHTML('beforeend', '<button type="button" data-testid="stop-button" id="stop">Stop</button>');
  if (asked.includes('abovecomposer') && app) {  // as on chatgpt.com 2026-10: 「正在思考」, then the card just above
    setTimeout(() => {                            // the composer, outside the thread, buttons with keyboard hints
      document.getElementById('stop').remove();
      form.insertAdjacentHTML('beforebegin', '<div id="pending-tool"><div>正在思考</div><div class="card">' +
        '<div><img alt="" src="connectors://asdk_app_WebCoder1/logo"><span>WebCoder</span></div>' +
        '<div>允许 ChatGPT 使用 WebCoder?</div>' +
        '<div>在项目 dolrev-1 中创建或覆盖私有审查报告文件。<a href="#">查看详情</a></div>' +
        '<div><button type="button" id="deny">拒绝<kbd>Esc</kbd></button>' +
        '<button type="button" id="confirm">允许一次<kbd>⏎</kbd></button>' +
        '<button type="button" aria-haspopup="menu" aria-label="更多选项"><svg></svg></button></div></div></div>');
      document.getElementById('confirm').addEventListener('click', () => {
        window.confirmed = { above: true };
        document.getElementById('pending-tool').remove();
        turns.insertAdjacentHTML('beforeend', '<li data-message-role="assistant" data-message-complete>' +
          '<div data-assistant-markdown><p>Wrote the report.</p></div></li>');
        send.hidden = false;
      });
    }, 150);
    return;
  }
  if (asked.includes('outside') && app) {  // a confirmation before any reply turn: in a row of its own, no Stop
    setTimeout(() => {
      document.getElementById('stop').remove();
      turns.insertAdjacentHTML('beforeend', '<li class="toolcall"><div class="card">' +
        '<img alt="" src="connectors://asdk_app_WebCoder1/logo"><p>WebCoder wants to use write_file</p>' +
        '<div><button type="button" id="deny">Deny</button><button type="button" id="confirm">Confirm</button></div>' +
        '</div></li>');
      document.getElementById('confirm').addEventListener('click', () => {
        window.confirmed = { outside: true };
        turns.querySelector('.toolcall').remove();
        turns.insertAdjacentHTML('beforeend', '<li data-message-role="assistant" data-message-complete>' +
          '<div data-assistant-markdown><p>Wrote the report.</p></div></li>');
        send.hidden = false;
      });
    }, 150);
    return;
  }
  const li = document.createElement('li');
  li.setAttribute('data-message-role', 'assistant');
  li.setAttribute('data-message-streaming', '');
  li.innerHTML = '<div data-assistant-markdown><p>Reading files</p></div>';
  turns.appendChild(li);
  const md = li.firstChild;
  if (asked.includes('canvas')) {  // a button in the reply that is not a tool confirmation
    md.insertAdjacentHTML('beforeend', '<div><p>Preview</p><button type="button" id="run">Run</button></div>');
    document.getElementById('run').addEventListener('click', () => { window.ran = true; });
  }
  if (asked.includes('otherapp')) {  // another app's confirmation, for a tool named like one of WebCoder's; the
    md.insertAdjacentHTML('beforeend', '<p>Next, WebCoder will edit_file.</p><div class="card">' +  // reply's text
      '<p>OtherApp wants to use write_file</p><pre><code>{"path": "E:\\\\WebCoder\\\\README.md"}</code></pre>' +
      '<p>Message: sent on behalf of WebCoder</p><div><button type="button">Deny</button>' +  // and the card's
      '<button type="button" id="allow">Allow</button></div></div>');  // arguments mention WebCoder
    document.getElementById('allow').addEventListener('click', () => { window.allowed = true; });
  }
  for (const [word, title, extra] of [  // more cards that are not WebCoder's, and one that is only by its id
    ['rival', 'WebCoder Test wants to use edit_file', ''],
    ['lookalike', 'Personal WebCoder wants to use edit_file', ''],
    ['byid', 'Allow this app to use edit_file?', '<img alt="" src="connectors://asdk_app_WebCoder1/logo">'],
    ['linkid', 'OtherApp wants to open a page', '<p><a href="connectors://asdk_app_WebCoder1/x">' +
      '<img alt="" src="connectors://asdk_app_WebCoder1/logo"></a></p>'],  // our id, but in its arguments
    ['rivalicon', 'WebCoder Test wants to use edit_file', '<img alt="" src="connectors://asdk_app_other9/logo">'],
  ]) {
    if (!asked.includes(word)) continue;
    md.insertAdjacentHTML('beforeend', `<div class="card">${extra}<p>${title}</p><div>` +
      `<button type="button">Deny</button><button type="button" id="${word}">Allow</button></div></div>`);
    document.getElementById(word).addEventListener('click', () => { window.clicked = word; });
  }
  const finish = () => setTimeout(() => {
    md.insertAdjacentHTML('beforeend',
      '<div><span>python</span><pre><code><span>print</span>("ok")\n</code></pre></div><p>Done.</p>');
    li.removeAttribute('data-message-streaming');
    li.setAttribute('data-message-complete', '');
    document.getElementById('stop').remove();
    send.hidden = false;
  }, 150);
  if (!app) { finish(); return; }
  setTimeout(() => {  // a write tool waits for a confirmation; nothing streams meanwhile
    li.removeAttribute('data-message-streaming');
    document.getElementById('stop').remove();
    // the card sits in the same content container as the rest of the reply (and any other button in it)
    md.insertAdjacentHTML('beforeend', '<div class="card"><p>WebCoder wants to use edit_file</p>' +
      '<label><input type="checkbox" id="remember"> Remember for this conversation</label>' +
      '<div class="actions"><button type="button" id="deny">Deny</button>' +
      '<button type="button" id="confirm">Confirm</button></div></div>');
    document.getElementById('confirm').addEventListener('click', () => {
      window.confirmed = { remember: document.getElementById('remember').checked };
      md.querySelector('.card').remove();
      li.setAttribute('data-message-streaming', '');
      form.insertAdjacentHTML('beforeend', '<button type="button" data-testid="stop-button" id="stop">Stop</button>');
      md.insertAdjacentHTML('beforeend', '<p>Edited src/app.py</p>');
      finish();
    });
  }, 150);
}
</script></body></html>"""


@pytest.fixture
def page(ui_browser):
    with ui_browser.new_context() as context:
        tab = context.new_page()
        tab.problems = []
        tab.on("pageerror", lambda exc: tab.problems.append(str(exc)))
        tab.set_content(PAGE)
        tab.evaluate("window.chrome = {runtime: {onMessage: {addListener: (fn) => { window.listener = fn; }}}}")
        tab.add_script_tag(path=str(CONTENT))
        yield tab
        assert tab.problems == []


def call(page, message: dict) -> dict:
    return page.evaluate("message => new Promise((resolve) => window.listener(message, {}, resolve))", message)


def follow(page, app: str, auto: bool = True, polls: int = 60) -> tuple[dict, list]:
    seen = []
    for _ in range(polls):
        state = call(page, {"cmd": "poll", "app": app, "autoApprove": auto})
        seen += state["approvals"]
        if state["started"] and state["complete"] and not state["generating"] and not state["pending"]:
            return state, seen
        page.wait_for_timeout(100)
    raise AssertionError(f"the reply did not finish: {state}")


def test_submit_attaches_the_app_and_approves_the_write(page):
    sent = call(page, {"cmd": "submit", "prompt": "fix the bug\nin src/app.py", "app": "WebCoder", "newChat": True})
    assert sent["ok"], sent
    assert sent["model"] == "Thinking"
    assert page.evaluate("window.sent") == [{"text": "fix the bug\nin src/app.py", "app": True}]
    state, approvals = follow(page, "WebCoder")
    assert approvals == [{"tool": "edit_file", "text": "Confirm", "decision": "auto"}]
    assert page.evaluate("window.confirmed") == {"remember": True}  # remembered for the conversation
    assert state["text"] == 'Reading files\n\nEdited src/app.py\n\n```python\nprint("ok")\n```\n\nDone.'

    # A follow-up: the composer was emptied, so the app is attached again
    again = call(page, {"cmd": "submit", "prompt": "and the tests", "app": "WebCoder", "newChat": False})
    assert again["ok"] and page.evaluate("window.sent[1]") == {"text": "and the tests", "app": True}
    state, _ = follow(page, "WebCoder")
    assert state["text"].startswith("Reading files")


def test_a_confirmation_outside_any_reply_turn_is_found(page):
    """What stalled a real review: the card came while the page showed no reply turn and no Stop button."""
    assert call(page, {"cmd": "submit", "prompt": "write it outside", "app": "WebCoder", "newChat": True})["ok"]
    page.wait_for_selector("#confirm")
    waiting = call(page, {"cmd": "poll", "app": "WebCoder", "autoApprove": False})
    assert not waiting["started"] and waiting["pending"]  # the task waits for it rather than giving up
    assert waiting["approvals"] == [{"tool": "write_file", "text": "Confirm", "decision": "pending"}]
    assert waiting["buttons"] == ["Deny", "Confirm"]  # named if the task gives up anyway
    state, approvals = follow(page, "WebCoder")
    assert approvals == [{"tool": "write_file", "text": "Confirm", "decision": "auto"}]
    assert page.evaluate("window.confirmed") == {"outside": True} and state["text"] == "Wrote the report."


def test_the_card_above_the_composer_is_approved_once(page):
    """chatgpt.com 2026-10: the card waits above the composer, its buttons read 「拒绝 Esc」「允许一次 ⏎」."""
    assert call(page, {"cmd": "submit", "prompt": "review abovecomposer", "app": "WebCoder", "newChat": True})["ok"]
    page.wait_for_selector("#confirm")
    waiting = call(page, {"cmd": "poll", "app": "WebCoder", "autoApprove": False})
    assert waiting["pending"] and waiting["approvals"] == [{"tool": "", "text": "允许一次", "decision": "pending"}]
    state, approvals = follow(page, "WebCoder")
    assert approvals == [{"tool": "", "text": "允许一次", "decision": "auto"}]
    assert page.evaluate("window.confirmed") == {"above": True} and state["text"] == "Wrote the report."


def test_a_card_above_the_last_message_is_left_alone(page):
    page.evaluate("""() => {
      document.getElementById('turns').insertAdjacentHTML('beforeend', '<li class="toolcall"><div class="card">' +
        '<img alt="" src="connectors://asdk_app_WebCoder1/logo"><p>WebCoder wants to use delete_path</p><div>' +
        '<button type="button">Deny</button><button type="button" id="stale">Confirm</button></div></div></li>');
      document.getElementById('stale').addEventListener('click', () => { window.stalePressed = true; });
    }""")
    assert call(page, {"cmd": "submit", "prompt": "fix the bug", "app": "WebCoder", "newChat": False})["ok"]
    state, approvals = follow(page, "WebCoder")
    assert [item["tool"] for item in approvals] == ["edit_file"] and state["text"].endswith("Done.")
    assert page.evaluate("window.stalePressed") is None  # from before this message: never ours to press


def test_without_auto_approval_the_confirmation_waits(page):
    assert call(page, {"cmd": "submit", "prompt": "change it", "app": "WebCoder", "newChat": True})["ok"]
    page.wait_for_selector("#confirm")
    state = call(page, {"cmd": "poll", "app": "WebCoder", "autoApprove": False})
    assert state["pending"] and state["approvals"] == [{"tool": "edit_file", "text": "Confirm", "decision": "pending"}]
    assert not call(page, {"cmd": "poll", "app": "WebCoder", "autoApprove": False})["approvals"]  # reported once
    assert page.evaluate("window.confirmed") is None
    page.click("#confirm")  # the person confirms in the browser
    state, _ = follow(page, "WebCoder", auto=False)
    assert state["text"].endswith("Done.")


def test_an_unknown_app_is_reported_with_what_the_menu_offers(page):
    prefix = call(page, {"cmd": "submit", "prompt": "x", "app": "WebCod", "newChat": True})
    assert prefix["code"] == "APP_NOT_FOUND"  # only the app of exactly that name, never one that merely contains it
    assert "WebCoder" in prefix["error"] and "WebCoder Test" in prefix["error"]
    missing = call(page, {"cmd": "submit", "prompt": "x", "app": "Figma", "newChat": True})
    assert missing["code"] == "APP_NOT_FOUND" and "没有出现应用菜单" in missing["error"]
    assert page.evaluate("document.getElementById('prompt-textarea').innerText.trim()") == ""  # "@Figma" removed
    no_app = call(page, {"cmd": "submit", "prompt": "plain question", "app": "", "newChat": True})
    assert no_app["ok"] and page.evaluate("window.sent") == [{"text": "plain question", "app": False}]


@pytest.mark.parametrize("which", ["rival", "lookalike", "linkid", "rivalicon"])
def test_an_app_whose_name_contains_ours_is_not_ours(page, which):
    assert call(page, {"cmd": "submit", "prompt": f"{which} card", "app": "WebCoder", "newChat": True})["ok"]
    page.wait_for_selector("#confirm")
    state = call(page, {"cmd": "poll", "app": "WebCoder", "autoApprove": True})
    decisions = {item["decision"] for item in state["approvals"]}
    assert decisions == {"foreign", "auto"}  # its Allow is left to the person; WebCoder's own Confirm is pressed
    assert state["pending"] and page.evaluate("window.clicked") is None


def test_a_card_is_known_by_the_apps_id_too(page):
    assert call(page, {"cmd": "submit", "prompt": "byid card", "app": "WebCoder", "newChat": True})["ok"]
    page.wait_for_selector("#confirm")
    call(page, {"cmd": "poll", "app": "WebCoder", "autoApprove": True})
    assert page.evaluate("window.clicked") == "byid"


def test_with_the_app_already_attached_only_its_id_counts(page):
    # WebCoder is attached before the submit: the "@" menu never opens, so the look-alike names are unknown.
    page.evaluate("""() => document.getElementById('prompt-textarea').insertAdjacentHTML('beforeend',
        '<span app-mention-name="webcoder" app-mention-display-name="WebCoder" ' +
        'app-mention-path="app://asdk_app_WebCoder1" contenteditable="false">WebCoder</span> ')""")
    assert call(page, {"cmd": "submit", "prompt": "rivalicon card", "app": "WebCoder", "newChat": False})["ok"]
    page.wait_for_selector("#confirm")
    state = call(page, {"cmd": "poll", "app": "WebCoder", "autoApprove": True})
    assert {item["decision"] for item in state["approvals"]} == {"foreign"}  # another app's icon; a bare name
    assert state["pending"]  # left to the person, and waited for
    assert page.evaluate("window.clicked") is None and page.evaluate("window.confirmed") is None


def test_a_card_that_shows_its_icon_late_is_judged_again(page):
    page.evaluate("""() => document.getElementById('prompt-textarea').insertAdjacentHTML('beforeend',
        '<span app-mention-name="webcoder" app-mention-display-name="WebCoder" ' +
        'app-mention-path="app://asdk_app_WebCoder1" contenteditable="false">WebCoder</span> ')""")
    assert call(page, {"cmd": "submit", "prompt": "change it", "app": "WebCoder", "newChat": False})["ok"]
    page.wait_for_selector("#confirm")
    first = call(page, {"cmd": "poll", "app": "WebCoder", "autoApprove": True})
    assert [item["decision"] for item in first["approvals"]] == ["foreign"] and first["pending"]  # no icon yet
    page.evaluate("""() => document.querySelector('.card').insertAdjacentHTML('afterbegin',
        '<img alt="" src="connectors://asdk_app_WebCoder1/logo">')""")
    second = call(page, {"cmd": "poll", "app": "WebCoder", "autoApprove": True})
    assert [item["decision"] for item in second["approvals"]] == ["auto"]
    assert page.evaluate("window.confirmed") == {"remember": True}
    state, _ = follow(page, "WebCoder")
    assert state["text"].endswith("Done.")


def test_another_app_chosen_in_the_composer_is_left_alone(page):
    page.evaluate("""() => document.getElementById('prompt-textarea').insertAdjacentHTML('beforeend',
        '<span app-mention-name="webcoder test" app-mention-display-name="WebCoder Test" ' +
        'app-mention-path="app://asdk_app_other" contenteditable="false">WebCoder Test</span> ')""")
    assert call(page, {"cmd": "draft"})["draft"] is True
    refused = call(page, {"cmd": "submit", "prompt": "x", "app": "WebCoder", "newChat": False, "owned": True})
    assert refused["code"] == "DRAFT_PRESENT"  # never mistaken for WebCoder, never sent with it
    assert page.evaluate("window.sent") == []


def test_buttons_that_are_not_ours_are_never_pressed(page):
    assert call(page, {"cmd": "submit", "prompt": "make a canvas", "app": "", "newChat": True})["ok"]
    state, approvals = follow(page, "WebCoder")
    assert approvals == [{"tool": "", "text": "Run", "decision": "ignored"}]
    assert page.evaluate("window.ran") is None


def test_the_reply_comes_back_as_markdown(page):
    page.evaluate("""() => document.getElementById('turns').insertAdjacentHTML('beforeend',
      '<li data-message-role="user"><div>q</div></li>' +
      '<li data-message-role="assistant" data-message-complete><div data-assistant-markdown><section>' +
      '<h2>Plan</h2><p>Steps:</p>' +
      '<ol><li><p>Read <code>app.py</code></p></li><li>Fix it<ul><li>edge case</li><li>tests</li></ul></li></ol>' +
      '<blockquote><p>careful</p></blockquote>' +
      '<div><table><thead><tr><th>file</th><th>change</th></tr></thead>' +
      '<tbody><tr><td>a.py</td><td>x | y</td></tr></tbody></table><button>Copy table</button></div>' +
      '<div><span>bash</span><pre><code>pytest -q</code></pre></div>' +
      '</section></div></li>')""")
    state = call(page, {"cmd": "poll", "app": "WebCoder", "autoApprove": True})
    assert state["complete"] and not state["generating"]
    assert state["text"] == ("## Plan\n\nSteps:\n\n1. Read app.py\n2. Fix it\n\n   - edge case\n   - tests\n\n"
                             "> careful\n\n| file | change |\n| --- | --- |\n| a.py | x \\| y |\n\n"
                             "```bash\npytest -q\n```")


def test_only_the_tool_card_is_approved_not_a_button_beside_it(page):
    # The same reply holds an unrelated "Run" button and, later, the WebCoder confirmation card.
    assert call(page, {"cmd": "submit", "prompt": "make a canvas, then edit_file it", "app": "WebCoder",
                       "newChat": True})["ok"]
    state, approvals = follow(page, "WebCoder")
    assert {"tool": "", "text": "Run", "decision": "ignored"} in approvals
    assert {"tool": "edit_file", "text": "Confirm", "decision": "auto"} in approvals
    assert page.evaluate("window.ran") is None and page.evaluate("window.confirmed") == {"remember": True}


def test_another_apps_card_is_not_approved_whatever_the_reply_says(page):
    assert call(page, {"cmd": "submit", "prompt": "otherapp", "app": "", "newChat": True})["ok"]
    page.wait_for_selector("#allow")
    page.wait_for_timeout(300)
    state = call(page, {"cmd": "poll", "app": "WebCoder", "autoApprove": True})
    assert [(item["text"], item["decision"]) for item in state["approvals"]] == [("Allow", "foreign")]
    assert state["pending"]  # the task waits for the person
    assert page.evaluate("window.allowed") is None
    # with no app at all (--no-app), no card is ours either
    assert call(page, {"cmd": "poll", "app": "", "autoApprove": True})["pending"]
    page.click(".card button:has-text('Deny')")  # the person denies it in the browser: then the reply is over
    state, _ = follow(page, "WebCoder")
    assert page.evaluate("window.allowed") is None


def test_a_draft_is_never_sent_along_or_cleared(page):
    for draft in ("my own note", "WebCoder"):  # a word that looks like the app's name is still the person's
        page.evaluate("draft => { const input = document.getElementById('prompt-textarea'); input.innerHTML = '';"
                      "input.focus(); document.execCommand('insertText', false, draft); }", draft)
        for owned in (False, True):  # in a tab the extension opened, the person may have typed too
            refused = call(page, {"cmd": "submit", "prompt": "the task", "app": "WebCoder", "newChat": False,
                                  "owned": owned})
            assert refused["code"] == "DRAFT_PRESENT", draft
        assert page.evaluate("document.getElementById('prompt-textarea').innerText.trim()") == draft
    assert page.evaluate("window.sent") == []


def test_what_a_failed_attempt_typed_is_cleared_before_the_next(page):
    page.evaluate("window.lockSend = true")
    failed = call(page, {"cmd": "submit", "prompt": "first try", "app": "", "newChat": False, "owned": True})
    assert failed["code"] == "SEND_DISABLED"
    assert page.evaluate("document.getElementById('prompt-textarea').innerText.trim()") == "first try"
    page.evaluate("window.lockSend = false")
    again = call(page, {"cmd": "submit", "prompt": "second try", "app": "", "newChat": False, "owned": True})
    assert again["ok"] and page.evaluate("window.sent") == [{"text": "second try", "app": False}]


def test_a_leftover_the_person_edited_is_theirs(page):
    page.evaluate("window.lockSend = true")
    assert call(page, {"cmd": "submit", "prompt": "first try", "app": "", "newChat": False})["code"] == "SEND_DISABLED"
    page.evaluate("const input = document.getElementById('prompt-textarea'); input.innerHTML = '';"
                  "input.focus(); document.execCommand('insertText', false, 'try')")
    page.evaluate("window.lockSend = false")
    refused = call(page, {"cmd": "submit", "prompt": "second", "app": "", "newChat": False})
    assert refused["code"] == "DRAFT_PRESENT"
    assert page.evaluate("document.getElementById('prompt-textarea').innerText.trim()") == "try"


def test_cancelling_after_the_click_never_retries_with_enter(page):
    page.evaluate("window.ignoreSend = true")  # the click does not go through
    result = page.evaluate("""() => new Promise((resolve) => {
        window.listener({cmd: 'submit', prompt: 'x', app: '', newChat: false}, {}, resolve);
        setTimeout(() => window.listener({cmd: 'abort'}, {}, () => {}), 1000);  // while it waits to see it sent
    })""")
    assert result["code"] == "CANCELLED"
    assert page.evaluate("window.enters") is None and page.evaluate("window.sent") == []


def test_draft_check_before_a_tab_is_reused(page):
    assert call(page, {"cmd": "draft"}) == {"ok": True, "draft": False}
    page.evaluate("window.lockSend = true")
    call(page, {"cmd": "submit", "prompt": "left behind", "app": "", "newChat": False})
    assert call(page, {"cmd": "draft"})["draft"] is False  # what an attempt of ours typed
    page.evaluate("document.getElementById('prompt-textarea').focus(); document.execCommand('insertText', false, '!')")
    assert call(page, {"cmd": "draft"})["draft"] is True  # edited by the person: theirs
    page.evaluate("document.getElementById('composer').style.display = 'none'")
    assert call(page, {"cmd": "draft"})["draft"] is True  # out of sight, still there
    page.evaluate("document.getElementById('prompt-textarea').remove()")
    assert call(page, {"cmd": "draft"})["draft"] is None  # nothing to look at: unknown


def test_a_cancelled_task_approves_nothing(page):
    assert call(page, {"cmd": "submit", "prompt": "change it", "app": "WebCoder", "newChat": True})["ok"]
    page.wait_for_selector("#confirm")
    call(page, {"cmd": "abort"})
    state = call(page, {"cmd": "poll", "app": "WebCoder", "autoApprove": True})
    assert state["pending"] and state["approvals"][0]["decision"] == "pending"
    assert page.evaluate("window.confirmed") is None


def test_cancelling_stops_a_submit_before_it_sends(page):
    result = page.evaluate("""() => new Promise((resolve) => {
        window.listener({cmd: 'submit', prompt: 'x', app: 'Figma', newChat: false, owned: true}, {}, resolve);
        setTimeout(() => window.listener({cmd: 'abort'}, {}, () => {}), 300);  // while it waits for the @ menu
    })""")
    assert result["code"] == "CANCELLED"
    assert page.evaluate("window.sent") == []
    assert page.evaluate("document.getElementById('prompt-textarea').innerText.trim()") == ""


def test_a_dialog_over_the_page_stops_the_submit(page):
    page.evaluate("document.body.insertAdjacentHTML('beforeend', '<dialog open>Welcome back · log in</dialog>')")
    blocked = call(page, {"cmd": "submit", "prompt": "x", "app": "", "newChat": True})
    assert blocked["code"] == "PAGE_BLOCKED" and "Welcome back" in blocked["error"]
    assert page.evaluate("window.sent") == []
