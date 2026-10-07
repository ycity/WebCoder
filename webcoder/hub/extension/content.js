// WebCoder content script on chatgpt.com: types a task into the composer, sends it, and reports the reply.
// The background service worker drives it with messages ({cmd: "submit" | "poll" | "stop" | "ping"}); every
// handler does a bounded amount of work. Waits use MutationObserver rather than chains of timers: Chromium
// throttles timers in background tabs hard (one per minute after five minutes hidden), observers not at all.
// ChatGPT's page changes often: every selector lives in SITE, and several page generations are tried.
(() => {
  if (window.__webcoderContent) return;
  window.__webcoderContent = true;

  const SITE = {
    composer: ['#prompt-textarea', "form[data-chatgpt-composer] [contenteditable='true'][role='textbox']",
      "form[data-chatgpt-composer] .ProseMirror[contenteditable='true']", "form .ProseMirror[contenteditable='true']",
      '#mobile-composer-prompt', "form textarea"],
    form: ['form[data-chatgpt-composer]', 'form[data-mobile-composer]', 'form[data-type="unified-composer"]', 'form'],
    send: ["form[data-chatgpt-composer] button[type='submit']", "[data-testid='send-button']", '#composer-submit-button',
      "form[data-mobile-composer] button[type='submit']"],
    sendLabel: /^(发送|send)/i,
    stop: ["[data-testid='stop-button']", "button[aria-label*='停止']", "button[aria-label*='Stop' i]"],
    // One element per turn, whatever the page generation: the outermost match is the turn.
    turn: "li[data-message-role], [data-message-author-role], [data-turn='assistant'], [data-turn='user'], " +
      "[data-chatgpt-search-unit-key$=':assistant'], [data-chatgpt-search-unit-key$=':user']",
    markdown: ["[data-assistant-markdown]", "[data-markdown-text-style='assistant-message']:not([data-markdown-text-tone='tertiary'])",
      '.markdown'],
    streaming: '[data-message-streaming], .result-streaming',
    submitting: 'form[data-octane-followup-submitting]',
    model: ["[data-testid='model-switcher-dropdown-button']", "form[data-chatgpt-composer] button[aria-label*='模型']",
      "form[data-chatgpt-composer] button[aria-label*='model' i]", "header button[aria-haspopup='menu']"],
    // The "@" menu: rows of [data-composer-overlay-floating-ui] (2026-10), or the usual listbox roles.
    option: "[data-composer-overlay-floating-ui] button[data-list-navigation-item], [data-mention-section-items] button, " +
      "[role='option'], [role='menuitem'], [role='menuitemradio'], [cmdk-item], [data-radix-collection-item]",
    // An app attached to the message: an inline mention in the editor, or a pill beside it.
    chip: "[app-mention-name], [app-mention-display-name], [contenteditable='false'], [data-mention], .mention, " +
      "[data-testid*='pill' i]",
    loggedOut: 'form[data-logged-out], [data-testid="login-button"]',
    // Tool-call confirmations (write tools need one unless remembered for the conversation). Labels are read
    // without the keyboard hint beside them (「允许一次 ⏎」「拒绝 Esc」, 2026-10).
    confirm: /^(confirm|allow|allow once|approve|accept|run|continue|确认|允许|允许一次|批准|同意|运行|继续)$/i,
    deny: /^(deny|decline|reject|cancel|don.t allow|拒绝|取消|不允许)$/i,  // its counterpart on a confirmation card
    remember: /(remember|always|don.t ask|for this (chat|conversation)|记住|始终|总是|不再询问|本次对话|此对话)/i,
    // WebCoder's tool names, to say which one a confirmation is for (shown only; never what makes a card ours).
    tools: /\b(git_[a-z_]+|ssh_[a-z]+|mobile_[a-z]+|cli_run|cli_programs|write_file|edit_file|delete_path|move_path|make_dir|copy_file|restore_backup|read_files?|list_dir|find_files|view_image|list_backups|list_projects|list_skills|read_skill)\b/,
  };

  const $ = (selectors, root = document) => {
    for (const s of [].concat(selectors)) {
      const el = root.querySelector(s);
      if (el) return el;
    }
    return null;
  };
  const visible = (el) => !!el && el.isConnected && (el.offsetParent !== null || el.getClientRects().length > 0);

  function waitFor(cond, timeout) {
    return new Promise((resolve) => {
      const first = cond();
      if (first) { resolve(first); return; }
      let done = false;
      const observer = new MutationObserver(() => { const v = cond(); if (v) finish(v); });
      const timer = setTimeout(() => finish(cond() || null), timeout);
      function finish(value) {
        if (done) return;
        done = true;
        observer.disconnect();
        clearTimeout(timer);
        resolve(value);
      }
      observer.observe(document.documentElement, { subtree: true, childList: true, attributes: true, characterData: true });
    });
  }
  const pause = (ms) => waitFor(() => false, ms);

  // ------------------------------------------------------------------ page model
  function composer() {
    for (const s of SITE.composer) {
      for (const el of document.querySelectorAll(s)) if (visible(el)) return el;
    }
    return null;
  }
  function form(input) { return (input && input.closest('form')) || $(SITE.form); }
  function composerText(input) { return input ? (input.value !== undefined ? input.value : input.innerText || '') : ''; }

  function turns() {
    const all = Array.from(document.querySelectorAll(SITE.turn));
    return all.filter((el) => !el.parentElement || !el.parentElement.closest(SITE.turn));
  }
  function role(el) {
    const key = el.getAttribute('data-chatgpt-search-unit-key') || '';
    return el.getAttribute('data-message-role') || el.getAttribute('data-message-author-role') ||
      el.getAttribute('data-turn') || (key.endsWith(':assistant') ? 'assistant' : key.endsWith(':user') ? 'user' : '');
  }
  function lastUserTurn() {
    const list = turns().filter((el) => role(el) === 'user');
    return list[list.length - 1] || null;
  }
  function replyTurns() {
    const list = turns();
    let start = -1;
    list.forEach((el, i) => { if (role(el) === 'user') start = i; });
    return list.slice(start + 1).filter((el) => role(el) === 'assistant');
  }
  // Buttons on the page below the person's last message, wherever it draws them: a tool's confirmation can come
  // before (or outside) anything the page marks as a reply turn, e.g. just above the composer (2026-10). Never
  // inside the editor or the "@" menu; whether a button is a confirmation, and ours, is decided per card.
  function laterButtons() {
    const user = lastUserTurn();
    if (!user) return [];
    const input = composer();
    return Array.from(document.querySelectorAll('button')).filter((button) => !user.contains(button) &&
      (user.compareDocumentPosition(button) & Node.DOCUMENT_POSITION_FOLLOWING) &&
      !(input && input.contains(button)) && !button.closest('[data-composer-overlay-floating-ui]') && visible(button));
  }
  // Rendered text, with code blocks back as fenced Markdown (innerText would give the language label as a line
  // of its own and lose the fences).
  function fence(pre, label) {
    const code = pre.querySelector('code') || pre;
    const named = (code.className || '').match(/language-([\w+#.-]+)/);
    const header = pre.querySelector('code') && pre.firstElementChild !== code && !pre.firstElementChild.contains(code)
      ? (pre.firstElementChild.innerText || '').trim().split('\n')[0] : '';
    const lang = (named && named[1]) || label || (/^[\w+#.-]{1,20}$/.test(header) ? header : '');
    return '```' + lang + '\n' + code.textContent.replace(/\n+$/, '') + '\n```';
  }
  // Blocks Markdown marks up (lists, headings ...): everything else is taken as rendered (innerText, which leaves
  // out what is hidden).
  const MARKED = 'pre, ul, ol, h1, h2, h3, h4, h5, h6, blockquote, table, hr';
  const INLINE = new Set(['A', 'B', 'STRONG', 'EM', 'I', 'U', 'S', 'DEL', 'CODE', 'SPAN', 'SUP', 'SUB', 'MARK', 'KBD',
    'SMALL', 'BR', 'IMG', 'ABBR', 'TIME']);
  const clean = (text) => (text || '').replace(/\u00a0/g, ' ').trim();
  const isLabel = (el) => el.nextElementSibling && el.nextElementSibling.tagName === 'PRE' && !el.querySelector('pre') &&
    /^[\w+#.-]{1,20}$/.test(clean(el.innerText));
  function blockText(el) {
    const tag = el.tagName;
    if (tag === 'PRE') return fence(el, '');
    if (tag === 'BUTTON' || el.getAttribute('aria-hidden') === 'true') return '';
    if (/^H[1-6]$/.test(tag)) return '#'.repeat(Number(tag[1])) + ' ' + clean(el.innerText);
    if (tag === 'HR') return '---';
    if (tag === 'UL' || tag === 'OL') return listText(el);
    if (tag === 'TABLE') return tableText(el);
    if (tag === 'BLOCKQUOTE') return blockChildren(el).split('\n').map((line) => '> ' + line).join('\n');
    if (!el.querySelector(MARKED)) return clean(el.innerText);
    return blockChildren(el);
  }
  function blockChildren(el) {
    const parts = [];
    let run = '';
    const flush = () => { if (run.trim()) parts.push(run.trim()); run = ''; };
    for (let node = el.firstChild; node; node = node.nextSibling) {
      if (node.nodeType === Node.TEXT_NODE) { run += node.textContent; continue; }
      if (node.nodeType !== Node.ELEMENT_NODE) continue;
      if (isLabel(node)) {  // the language label shown above a code block
        flush();
        parts.push(fence(node.nextElementSibling, clean(node.innerText)));
        node = node.nextElementSibling;
        continue;
      }
      if (INLINE.has(node.tagName) && !node.querySelector(MARKED)) {
        run += node.tagName === 'BR' ? '\n' : node.innerText;
        continue;
      }
      flush();
      parts.push(blockText(node));
    }
    flush();
    return parts.filter(Boolean).join('\n\n');
  }
  function listText(list) {
    let number = Number(list.getAttribute('start') || 1);
    return Array.from(list.children).filter((item) => item.tagName === 'LI').map((item) => {
      const marker = list.tagName === 'OL' ? `${number++}. ` : '- ';
      const lines = (item.querySelector(MARKED) ? blockChildren(item) : clean(item.innerText)).split('\n');
      return marker + lines[0] + lines.slice(1).map((line) => '\n' + (line ? ' '.repeat(marker.length) + line : '')).join('');
    }).join('\n');
  }
  function tableText(table) {
    const rows = Array.from(table.querySelectorAll('tr')).map((row) => Array.from(row.children)
      .map((cell) => clean(cell.innerText).replace(/\s*\n\s*/g, ' ').replace(/\|/g, '\\|')));
    if (!rows.length) return '';
    const lines = rows.map((cells) => '| ' + cells.join(' | ') + ' |');
    lines.splice(1, 0, '|' + ' --- |'.repeat(rows[0].length));
    return lines.join('\n');
  }
  function turnText(el) {
    for (const s of SITE.markdown) {
      const parts = Array.from(el.querySelectorAll(s)).filter((p) => !p.parentElement || !p.parentElement.closest(s));
      if (parts.length) return parts.map(blockText).filter(Boolean).join('\n\n');
    }
    return blockText(el).replace(/^\s*(ChatGPT\s*(说|said)\s*[:：])\s*/i, '').trim();
  }
  function stopButton() {
    for (const s of SITE.stop) {
      for (const el of document.querySelectorAll(s)) if (visible(el)) return el;
    }
    return null;
  }
  function sendButton(input) {
    const root = form(input) || document;
    for (const s of SITE.send) {
      const el = root.querySelector(s) || document.querySelector(s);
      if (visible(el)) return el;
    }
    return Array.from(root.querySelectorAll('button')).find((b) => visible(b) &&
      SITE.sendLabel.test(b.getAttribute('aria-label') || '')) || null;
  }
  function generating() {
    const last = replyTurns().pop();
    return !!(stopButton() || document.querySelector(SITE.submitting) ||
      (last && (last.matches(SITE.streaming) || last.querySelector(SITE.streaming))));
  }
  function blockingDialog() {
    const dialog = Array.from(document.querySelectorAll('dialog[open]')).find(visible);
    if (!dialog) return null;
    return (dialog.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 160) || '一个对话框';
  }
  function modelLabel() {
    const el = $(SITE.model);
    return el ? (el.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 80) : '';
  }

  // ------------------------------------------------------------------ input
  function placeCaretAtEnd(input) {
    input.focus();
    if (input.value !== undefined) {
      input.setSelectionRange(input.value.length, input.value.length);
    } else {
      const selection = getSelection();
      selection.selectAllChildren(input);
      selection.collapseToEnd();
    }
  }
  function typeText(input, text) {
    placeCaretAtEnd(input);
    if (document.execCommand('insertText', false, text)) return true;
    const data = new DataTransfer();  // editors that ignore insertText take a paste
    data.setData('text/plain', text);
    return input.dispatchEvent(new ClipboardEvent('paste', { clipboardData: data, bubbles: true, cancelable: true }));
  }
  function backspace(input, count) {
    placeCaretAtEnd(input);
    for (let i = 0; i < count; i++) document.execCommand('delete', false);
  }
  function press(el) {  // the whole pointer sequence: menus often act on pointerdown / mousedown
    const r = el.getBoundingClientRect();
    const at = { bubbles: true, cancelable: true, composed: true, clientX: r.left + r.width / 2, clientY: r.top + r.height / 2, button: 0 };
    el.dispatchEvent(new PointerEvent('pointerdown', { ...at, pointerType: 'mouse', isPrimary: true }));
    el.dispatchEvent(new MouseEvent('mousedown', at));
    el.dispatchEvent(new PointerEvent('pointerup', { ...at, pointerType: 'mouse', isPrimary: true }));
    el.dispatchEvent(new MouseEvent('mouseup', at));
    el.click();
  }
  // Apps attached to the message (an inline mention in the editor, or a pill beside it), by their names.
  function attachedApps(input) {
    const root = form(input);
    if (!root) return [];  // never the sidebar's list of apps
    return Array.from(root.querySelectorAll(SITE.chip)).filter((el) =>
      el.hasAttribute('app-mention-name') || el.hasAttribute('app-mention-display-name') ||
      (visible(el) && el !== input && !(input.contains(el) && el.isContentEditable) && !el.closest('[app-mention-name]') &&
        el.matches("[contenteditable='false'], [data-mention], .mention, [data-testid*='pill' i]")))
      .map((el) => [el.getAttribute('app-mention-name'), el.getAttribute('app-mention-display-name'),
        (el.innerText || '').trim()].filter(Boolean).map((name) => name.toLowerCase()));
  }
  // Our app is attached: one of them is named exactly so (not "WebCoder Test").
  function hasAppChip(input, app) {
    const key = app.toLowerCase();
    return attachedApps(input).some((names) => names.includes(key));
  }
  // Another app is attached (one the person chose): the message is theirs to send, not ours to add to.
  function otherAppAttached(input, app) {
    const key = (app || '').toLowerCase();
    return attachedApps(input).some((names) => !names.includes(key));
  }
  function options() {
    return Array.from(document.querySelectorAll(SITE.option)).filter(visible);
  }
  // An option's name: the text of its first leaf element (its description comes after it).
  function optionName(option) {
    const leaf = Array.from(option.querySelectorAll('*')).find((el) => !el.children.length && (el.textContent || '').trim());
    return ((leaf || option).textContent || '').trim();
  }
  // What tells our app's confirmation cards from others': its id (from the mention the composer gained when it was
  // attached), and the names of other apps that contain ours ("WebCoder Test"), as offered in the "@" menu.
  const identity = { app: '', id: '', rivals: [], rivalsKnown: false };
  async function attachApp(input, app) {
    const key = app.toLowerCase();
    if (identity.app !== key) Object.assign(identity, { app: key, id: '', rivals: [], rivalsKnown: false });
    const learn = () => {
      const chip = Array.from((form(input) || document).querySelectorAll('[app-mention-path]')).find((el) =>
        (el.getAttribute('app-mention-name') || el.getAttribute('app-mention-display-name') || '').toLowerCase() === key);
      const id = chip && (chip.getAttribute('app-mention-path') || '').match(/asdk_app_[\w-]+/);
      if (id) identity.id = id[0];
    };
    if (hasAppChip(input, app)) { learn(); return { ok: true, already: true }; }
    const typed = '@' + app;
    typeText(input, typed);
    const pick = () => options().find((el) => optionName(el).toLowerCase() === key) || null;  // that app, exactly
    const option = await waitFor(pick, 6000);
    const names = options().map(optionName).filter(Boolean);
    identity.rivals = [...new Set([...identity.rivals, ...names.map((name) => name.toLowerCase())
      .filter((name) => name !== key && name.includes(key))])];
    identity.rivalsKnown = identity.rivalsKnown || !!option;  // the menu listed the apps by that name
    if (!option) {
      backspace(input, typed.length);
      return { ok: false, seen: names.slice(0, 12) };
    }
    press(option);
    const attached = await waitFor(() => !composerText(input).includes(typed) || hasAppChip(input, app), 3000);
    if (!attached) {  // some menus take the keyboard instead
      input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', code: 'Enter', keyCode: 13, bubbles: true, cancelable: true }));
      await pause(400);
    }
    learn();
    return { ok: !composerText(input).includes(typed) || hasAppChip(input, app) };
  }

  // Text in the composer besides attached apps' chips (the elements, not words that look like an app's name): a
  // draft not yet sent.
  function draftText(input) {
    if (input.value !== undefined) return input.value.trim();
    const copy = input.cloneNode(true);
    for (const chip of copy.querySelectorAll("[app-mention-name], [app-mention-display-name], [contenteditable='false']")) {
      chip.remove();
    }
    return (copy.textContent || '').trim();
  }
  function clearComposer(input) {
    input.focus();
    if (input.value !== undefined) input.select();
    else getSelection().selectAllChildren(input);
    document.execCommand('delete', false);
  }

  // What a submit typed here and did not send (it failed or was cancelled midway): only that much of a draft is
  // ours to clear. Anything else in the composer is the person's, whoever opened the tab.
  let unsent = '';
  const squeeze = (text) => text.replace(/\s+/g, '');
  function leftoverOfOurs(draft) {  // exactly what it typed: once edited, the text is the person's
    return !!unsent && squeeze(unsent) === squeeze(draft);
  }

  // ------------------------------------------------------------------ handlers
  let aborted = false;  // the task was cancelled while submit() runs: it stops before it sends anything
  const CANCELLED = { ok: false, code: 'CANCELLED', error: '已取消' };
  async function submit(msg) {
    aborted = false;
    const input = await waitFor(() => composer(), 30000);
    if (aborted) return CANCELLED;
    if (!input) {
      const dialog = blockingDialog();
      return { ok: false, code: dialog ? 'PAGE_BLOCKED' : 'NO_COMPOSER',
        error: dialog ? `ChatGPT 页面被弹窗挡住了：${dialog}` : '找不到 ChatGPT 的输入框（页面可能改版了，或没加载完）' };
    }
    if (msg.app && document.querySelector(SITE.loggedOut)) {
      return { ok: false, code: 'NOT_SIGNED_IN', error: '这个浏览器里的 ChatGPT 没有登录：登录后才能使用应用（MCP）' };
    }
    const dialog = blockingDialog();
    if (dialog) return { ok: false, code: 'PAGE_BLOCKED', error: `ChatGPT 页面被弹窗挡住了，请先在浏览器里处理：${dialog}` };
    if (generating()) {
      const idle = await waitFor(() => aborted || !generating(), 120000);
      if (aborted) return CANCELLED;
      if (!idle) return { ok: false, code: 'BUSY', error: '这个会话还在生成上一条回复' };
    }
    const draft = draftText(input);
    if (otherAppAttached(input, msg.app)) {  // the person chose another app for this message: not ours to send
      return { ok: false, code: 'DRAFT_PRESENT', error: '这个标签页的输入框里选着别的应用' };
    }
    if (draft) {
      // Unsent text is never sent along: cleared only when it is what an earlier attempt here typed.
      if (!leftoverOfOurs(draft)) return { ok: false, code: 'DRAFT_PRESENT', error: '这个标签页的输入框里有还没发送的文字' };
      clearComposer(input);
    }
    unsent = '';
    const warnings = [];
    if (msg.app) {
      const attached = await attachApp(input, msg.app);
      if (aborted) return CANCELLED;
      if (!attached.ok) {
        const seen = attached.seen && attached.seen.length ? `（菜单里有：${attached.seen.join('、')}）` : '（没有出现应用菜单）';
        const text = `在 @ 菜单里没找到应用「${msg.app}」${seen}。用 webc config app <ChatGPT 里的应用名> 设置正确的名字`;
        if (msg.newChat) return { ok: false, code: 'APP_NOT_FOUND', error: text };
        warnings.push(text + '；这次照常发送，沿用会话里已用过的应用');
      }
    }
    const before = lastUserTurn();
    const beforeCount = turns().filter((el) => role(el) === 'user').length;
    const prefix = msg.prompt.slice(0, 24);
    if (aborted) return CANCELLED;
    unsent = msg.prompt;
    typeText(input, msg.prompt);
    const typed = await waitFor(() => composerText(input).replace(/\s+/g, '').includes(prefix.replace(/\s+/g, '')), 5000);
    if (!typed) return { ok: false, code: 'TYPE_FAILED', error: '没能把任务输入到 ChatGPT 的输入框' };
    const button = await waitFor(() => { const b = sendButton(input); return aborted || (b && !b.disabled) ? b : null; }, 15000);
    if (aborted) { clearComposer(input); unsent = ''; return CANCELLED; }
    if (!button) return { ok: false, code: 'SEND_DISABLED', error: 'ChatGPT 的发送按钮一直不可用' };
    button.click();
    const sentCheck = () => {
      const last = lastUserTurn();
      const count = turns().filter((el) => role(el) === 'user').length;
      return (last && last !== before) || count > beforeCount || generating();
    };
    let sent = await waitFor(() => aborted || sentCheck(), 15000);
    if (!sentCheck() && aborted) { clearComposer(input); unsent = ''; return CANCELLED; }  // not sent: and now never
    if (!sent && composerText(input).trim()) {  // try the keyboard once
      input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', code: 'Enter', keyCode: 13, bubbles: true, cancelable: true }));
      sent = await waitFor(sentCheck, 10000);
    }
    if (!sent) return { ok: false, code: 'SEND_FAILED', error: '点了发送，但 ChatGPT 没有收到这条消息' };
    unsent = '';
    return { ok: true, warnings, model: modelLabel(), url: location.href };
  }

  const verdicts = new WeakMap();  // confirm button -> the verdict last reported on it
  const pressed = new WeakSet();   // confirm buttons approved (pressed once, never again)
  // A button's words, without a keyboard hint shown in it (<kbd>Esc</kbd>, ⏎).
  const SHORTCUT = /\s*(esc|enter|return|⏎|↵)$/i;
  const buttonLabel = (button) => {
    let text = button.innerText || '';
    for (const hint of button.querySelectorAll('kbd')) text = text.replace(hint.innerText || '\u0000', '');
    text = text.replace(/\s+/g, ' ').trim();
    while (SHORTCUT.test(text) && text.replace(SHORTCUT, '').trim()) text = text.replace(SHORTCUT, '').trim();
    return text || (button.getAttribute('aria-label') || '').trim();
  };
  const PROSE = SITE.markdown.join(', ');
  // The confirmation card a confirm button belongs to: from the nearest element around it that holds a deny /
  // cancel button (the row of buttons) outwards, as long as nothing else comes in: no other confirm button (a
  // neighbouring card), none of the reply's text (its Markdown), not the reply itself. Only that card decides (see
  // isOurs): a canvas "Run", another app's "Allow", a login "Continue" ... is never ours to press, whatever the
  // reply around it says.
  function confirmCard(button, area) {
    let row = null;
    let card = null;
    const input = composer();
    for (let el = button.parentElement, depth = 0; el && area.contains(el) && depth < 12; el = el.parentElement, depth++) {
      if (el.matches(SITE.turn) || el.matches(PROSE) || el.querySelector(PROSE)) break;
      if (el.querySelector(SITE.turn) || (input && el.contains(input))) break;  // past the card: the thread itself
      const others = Array.from(el.querySelectorAll('button')).filter((other) => other !== button && visible(other));
      if (others.some((other) => SITE.confirm.test(buttonLabel(other)))) break;
      if ((el.innerText || '').length > 2000) break;
      if (!row && !others.some((other) => SITE.deny.test(buttonLabel(other)))) continue;
      row = row || el;
      card = el;
    }
    return card;
  }
  // Whose card it is, decided only when it is clear. An app's icon on the card (connectors://<app id>/..., outside
  // links, code and arguments) settles it: ours when it is our app's id (from the mention the composer gained),
  // another app's when it is any other, or ours unknown. A card without one is ours only by its title (first line,
  // leaving out arguments and code) beginning with our app's exact name, and only when the "@" menu on this page
  // listed the apps by that name, none of which ("WebCoder Test", "Personal WebCoder") the title names. A name
  // further into the title, a tool's name, the arguments or the message being sent prove nothing. Without an
  // app (--no-app) nothing is ours.
  function isOurs(card, key) {
    if (!key || identity.app !== key) return false;
    const within = (el, selector) => {  // inside the card only: the turns themselves sit in lists
      for (let node = el; node && node !== card; node = node.parentElement) if (node.matches(selector)) return true;
      return false;
    };
    const apps = Array.from(card.querySelectorAll('img[src]')).filter((img) => !within(img, `a, ${NOT_TITLE}`))
      .map((img) => ((img.getAttribute('src') || '').match(/^connectors:\/\/(asdk_app_[\w-]+)\//) || [])[1])
      .filter(Boolean);
    if (apps.length) return !!identity.id && apps.every((id) => id === identity.id);
    if (!identity.rivalsKnown) return false;
    const title = cardTitle(card).toLowerCase();
    if (identity.rivals.some((rival) => title.includes(rival))) return false;
    return title.startsWith(key) && !/[\p{L}\p{N}_]/u.test(title.charAt(key.length));
  }
  // The card's first line of text, read from the nodes (block elements end a line), leaving out code, inputs,
  // buttons, lists, tables and hidden parts.
  const BLOCKS = new Set(['P', 'DIV', 'H1', 'H2', 'H3', 'H4', 'H5', 'H6', 'LI', 'SECTION', 'HEADER', 'FOOTER', 'ARTICLE',
    'BR', 'LABEL', 'FIGCAPTION', 'BLOCKQUOTE', 'DT', 'DD']);
  const NOT_TITLE = "pre, code, textarea, input, select, button, details, table, ul, ol, svg, [hidden], [aria-hidden='true']";
  function cardTitle(card) {
    let line = '';
    const walk = (node) => {
      for (let child = node.firstChild; child; child = child.nextSibling) {
        if (child.nodeType === Node.TEXT_NODE) { line += child.textContent; continue; }
        if (child.nodeType !== Node.ELEMENT_NODE || child.matches(NOT_TITLE)) continue;
        const block = BLOCKS.has(child.tagName);
        if (block && line.trim()) return true;
        if (walk(child) || (block && line.trim())) return true;
      }
      return false;
    };
    walk(card);
    return line.replace(/\s+/g, ' ').trim().slice(0, 200);
  }
  // Confirm buttons on the page, judged afresh at every poll from what the card shows now (a card is drawn in
  // steps: its icon may come after its buttons): pressed (ours, auto), left for the person (ours with --confirm:
  // pending; a confirmation card that may not be ours: foreign), or not a confirmation at all (ignored). Each
  // button is pressed at most once, and reported again only when the verdict on it changes. While a pending or
  // foreign card waits, the task waits too: its reply is not over.
  function approvals(app, auto) {
    const found = [];
    let pending = false;
    const areas = [...replyTurns(), ...Array.from(document.querySelectorAll("dialog[open], [role='dialog'], [role='alertdialog']"))];
    const key = (app || '').toLowerCase();
    const report = (button, item) => {
      if (verdicts.get(button) === item.decision) return;
      verdicts.set(button, item.decision);
      found.push(item);
    };
    const candidates = new Map();  // button -> the area its card is looked for in
    for (const area of areas) for (const button of area.querySelectorAll('button')) candidates.set(button, area);
    for (const button of laterButtons()) if (!candidates.has(button)) candidates.set(button, document.body);
    for (const [button, area] of candidates) {
      const label = buttonLabel(button);
      if (!visible(button) || button.disabled || !SITE.confirm.test(label)) continue;
      if (pressed.has(button)) continue;  // ours, approved: its card is on its way out
      const box = confirmCard(button, area);
      if (!box) { report(button, { tool: '', text: label, decision: 'ignored' }); continue; }
      const tool = ((box.innerText || '').match(SITE.tools) || [])[0] || '';
      if (!isOurs(box, key)) {
        pending = true;
        report(button, { tool, text: label, decision: 'foreign', title: cardTitle(box) });
        continue;
      }
      if (!auto) {
        pending = true;
        report(button, { tool, text: label, decision: 'pending' });
        continue;
      }
      pressed.add(button);
      for (const toggle of box.querySelectorAll("input[type='checkbox'], [role='checkbox'], [role='switch']")) {
        const wrap = toggle.closest('label') || toggle.parentElement || toggle;
        const words = (wrap.innerText || '') + ' ' + (toggle.getAttribute('aria-label') || '');
        const on = toggle.checked || toggle.getAttribute('aria-checked') === 'true';
        if (!on && SITE.remember.test(words)) press(toggle);  // remember the choice for this conversation
      }
      press(button);
      report(button, { tool, text: label, decision: 'auto' });
    }
    return { found, pending };
  }

  function poll(msg) {
    const replies = replyTurns();
    const last = replies[replies.length - 1];
    const approval = approvals(msg.app, msg.autoApprove !== false && !aborted);  // a cancelled task approves nothing
    const busy = generating();
    const loggedOut = !!document.querySelector(SITE.loggedOut);
    const composerForm = form(composer());
    return {
      ok: true,
      url: location.href,
      title: document.title,
      model: modelLabel(),
      text: replies.map(turnText).filter(Boolean).join('\n\n'),
      started: replies.length > 0 || busy,
      generating: busy,
      complete: !!last && !busy && (last.hasAttribute('data-message-complete') || !last.hasAttribute('data-message-role')),
      approvals: approval.found,
      pending: approval.pending,
      blocked: blockingDialog(),
      loggedOut,
      // What the page offers below the person's message (besides the composer's own controls), named when a task
      // gives up waiting on it.
      buttons: [...new Set(laterButtons().filter((button) => !(composerForm && composerForm.contains(button)))
        .map(buttonLabel).filter(Boolean))].slice(0, 12),
    };
  }

  function stop() {
    const button = stopButton();
    if (button) press(button);
    return { ok: true, stopped: !!button };
  }

  const abort = () => { aborted = true; return { ok: true }; };
  // Before the extension navigates, reloads or closes this tab: is there text in a composer the person typed
  // (not what an earlier attempt of ours left)? Every composer on the page counts, seen or not; with none to
  // look at, the answer is "unknown" (null), and such a tab is left alone too.
  function draft() {
    const inputs = SITE.composer.flatMap((s) => Array.from(document.querySelectorAll(s)));
    if (!inputs.length) return { ok: true, draft: null };
    if (inputs.some((input) => otherAppAttached(input, identity.app))) return { ok: true, draft: true };
    const text = inputs.map(draftText).filter(Boolean);
    return { ok: true, draft: text.some((item) => !leftoverOfOurs(item)) };
  }
  const HANDLERS = { ping: () => ({ ok: true, url: location.href }), submit, poll, stop, abort, draft };
  chrome.runtime.onMessage.addListener((msg, sender, respond) => {
    const handler = msg && HANDLERS[msg.cmd];
    if (!handler) return false;
    Promise.resolve().then(() => handler(msg)).then(respond,
      (error) => respond({ ok: false, code: 'CONTENT_ERROR', error: String((error && error.message) || error) }));
    return true;
  });
})();
