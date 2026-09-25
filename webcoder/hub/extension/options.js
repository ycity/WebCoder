'use strict';
const $ = (id) => document.getElementById(id);
const STATES = { online: '已连接', connecting: '正在连接…', offline: '未连上 Hub', error: '出错了', unlinked: '还没有连接', idle: '…' };

async function refresh() {
  const info = await chrome.runtime.sendMessage({ cmd: 'status' });
  const state = info.linked ? info.status.state : 'unlinked';
  $('dot').className = 'dot ' + state;
  $('state').textContent = STATES[state] || state;
  $('detail').textContent = info.linked ? info.status.detail : '';
  $('link').hidden = info.linked;
  $('linked').hidden = !info.linked;
  if (!$('hub').value) $('hub').value = info.hubUrl;
  if (document.activeElement !== $('name')) $('name').value = info.name || info.defaultName;
  $('running').textContent = info.running.length ? `正在运行 ${info.running.length} 个任务` : `扩展版本 ${info.version}`;
}

$('pair').addEventListener('click', async () => {
  $('pair-error').textContent = '';
  $('pair').disabled = true;
  try {
    const result = await chrome.runtime.sendMessage({ cmd: 'pair', hubUrl: $('hub').value, code: $('code').value });
    if (!result.ok) $('pair-error').textContent = result.error;
    else $('code').value = '';
  } finally {
    $('pair').disabled = false;
    refresh();
  }
});
$('rename').addEventListener('click', async () => {
  await chrome.runtime.sendMessage({ cmd: 'rename', name: $('name').value.trim() });
  refresh();
});
$('unlink').addEventListener('click', async () => {
  await chrome.runtime.sendMessage({ cmd: 'unlink' });
  refresh();
});

refresh();
setInterval(refresh, 1500);
