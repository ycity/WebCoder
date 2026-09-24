"""Headless browser test of the collaboration center page (skipped without Playwright + Edge/Chromium).

Drives webcoder/hub/static/collaboration*.js against a real hub: rooms, messages, dot / Work chat slots with WAJ join
codes, an assistant connection joining and speaking over MCP, agent bindings, message -> task, goals, monitoring, and
a dot chat's host subscriptions, test events and receipt confirmation (event callbacks stay in-process).
Any page error or console error (CSP violations included) fails the test.
Set WEBCODER_SCREENSHOTS=<folder> to keep screenshots of every step.
"""
import base64
import json
import os
import re
import uuid
from pathlib import Path

import pytest

from tests.conftest import ADMIN_PASSWORD, Hub
from webcoder.hub.collaboration.network import Reply

sync_api = pytest.importorskip("playwright.sync_api")

WAJ = re.compile(r"WAJ-(?:[0-9A-F]{4}-){3}[0-9A-F]{4}")
SCOPE = {"project": "imago", "environment_id": "production"}
# MCP Events (as in test_collaboration_http.py): callbacks never leave the process, an in-process receiver answers.
MODERN = "2026-07-28"
META = {"io.modelcontextprotocol/protocolVersion": MODERN, "io.modelcontextprotocol/clientCapabilities": {}}
SECRET = "whsec_" + base64.b64encode(b"fixture-only-signing-material-32b!").decode()
CALLBACK = "https://example.invalid/callback-private-marker"


def key() -> str:
    return uuid.uuid4().hex


@pytest.fixture
def page():
    shots = os.environ.get("WEBCODER_SCREENSHOTS")
    with sync_api.sync_playwright() as playwright:
        browser = None
        for kwargs in ({"channel": "msedge"}, {"channel": "chrome"}, {}):
            try:
                browser = playwright.chromium.launch(headless=True, **kwargs)
                break
            except Exception:  # noqa: BLE001 - try the next available browser
                continue
        if browser is None:
            pytest.skip("no Chromium-based browser available for Playwright")
        context = browser.new_context(viewport={"width": 1280, "height": 900}, locale="zh-CN")
        tab = context.new_page()
        tab.problems = []
        tab.on("pageerror", lambda exc: tab.problems.append(f"pageerror: {exc}"))
        tab.on("console", lambda msg: tab.problems.append(f"console: {msg.text}") if msg.type == "error" else None)
        tab.on("dialog", lambda dialog: dialog.accept())  # the confirm() before destructive actions

        def shot(name):
            if shots:
                Path(shots).mkdir(parents=True, exist_ok=True)
                tab.screenshot(path=str(Path(shots) / f"{name}.png"), full_page=True)

        tab.shot = shot
        yield tab
        browser.close()


@pytest.fixture
def collab_hub(tmp_path):
    instance = Hub(tmp_path / "hub-data", collaboration_enabled=True, mcp_events_enabled=True)
    device = instance.add_device("fixture-pc")
    # A new project's folder is normally checked through its online agent; the page needs none, so the project row
    # is written directly (owned by the panel's admin), as in test_collaboration_http.py.
    store = instance.runtime.store
    owner = store.one("SELECT id FROM users WHERE username='admin'")["id"]
    store.execute("INSERT INTO projects(id, alias, alias_key, device_id, root, mode, created, user_id) "
                  "VALUES ('prj_fixture', 'imago', 'imago', ?, ?, 'rw', 0, ?)",
                  (device["device_id"], str(tmp_path), owner))
    instance.project = {"id": "prj_fixture", "alias": "imago"}
    yield instance
    instance.stop()


def login(page, hub):
    page.goto(hub.url + "/")
    page.fill("input[name=username]", "admin")
    page.fill("input[name=password]", ADMIN_PASSWORD)
    with page.expect_response("**/api/collaboration/status") as status:
        page.click("button[type=submit]")
        page.wait_for_selector("text=MCP 地址")
    assert status.value.ok


def mcp(hub, token, tool, **arguments) -> dict:
    """Call a collaboration tool the way a joined chat's MCP connection does."""
    response = hub.rpc(token, "tools/call", {"name": tool, "arguments": arguments})
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert not result.get("isError"), result
    return result["structuredContent"]


def subscribe(hub, token, request) -> None:
    """The host's native subscription for one requested event (the 2026 protocol's events/subscribe)."""
    params = {**request, "delivery": {"mode": "webhook", "url": CALLBACK, "secret": SECRET}, "_meta": META}
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
               "Authorization": f"Bearer {token}", "MCP-Protocol-Version": MODERN, "Mcp-Method": "events/subscribe",
               "Mcp-Name": request["name"]}
    response = hub.http.post("/mcp", headers=headers,
                             json={"jsonrpc": "2.0", "id": 7, "method": "events/subscribe", "params": params})
    assert response.status_code == 200 and "result" in response.json(), response.text


def grant_id(hub, label) -> str:
    grants = hub.panel.get("/api/collaboration/grants").json()["grants"]
    return next(grant["id"] for grant in grants if grant["label"] == label)


def feedback(page, text):
    page.wait_for_selector(f"#cc-feedback >> text={text}")


def test_collaboration_link_stays_hidden_while_the_feature_is_off(page, hub):
    login(page, hub)
    assert page.locator('#nav a[data-route="collaboration"]').is_hidden()
    page.goto(hub.url + "/#/collaboration")
    page.wait_for_selector("text=协作中心尚未启用")
    assert "WEBCODER_COLLABORATION_ENABLED" in page.inner_text("main")
    assert page.locator('#nav a[data-route="collaboration"]').is_hidden()
    page.shot("collab-00-off")
    assert page.problems == []


def test_collaboration_center_rooms_chats_goals_and_monitoring(page, collab_hub):
    hub = collab_hub
    project = hub.project["id"]
    work_token = hub.create_token(scopes=("read",), projects=[project], label="Work 只读")
    hub.create_token(scopes=("read", "write"), projects=[project], label="Codex 读写")  # never offered as a worker
    work_grant = grant_id(hub, "Work 只读")

    login(page, hub)
    link = page.locator('#nav a[data-route="collaboration"]')
    link.wait_for(state="visible")
    link.click()
    page.wait_for_selector("text=开启这个项目的共享讨论室")
    page.shot("collab-01-intro")

    # An independent room for the project.
    page.click('button[data-cc-action="new-conversation"]')
    page.fill("#cc-new-conversation input[name=title]", "发布排查")
    assert page.is_checked(f'#cc-new-conversation input[name=projects][value="{project}"]')
    page.check("#cc-new-conversation input[name=confirm]")
    page.click("#cc-new-conversation button[type=submit]")
    page.wait_for_selector(".cc-chat-shell")
    feedback(page, "独立聊天室已创建。")
    assert page.inner_text(".cc-room-title h2") == "发布排查"
    room = next(item for item in hub.panel.get("/api/collaboration/conversations").json()["items"]
                if item["title"] == "发布排查")

    # A message: saved, no task.
    page.fill("#cc-message-input", "先看一下 imago 的发布日志。")
    page.press("#cc-message-input", "Enter")
    page.wait_for_selector(".cc-message >> text=先看一下 imago 的发布日志。")
    feedback(page, "消息已保存，未创建任务。")
    page.shot("collab-02-message")

    # A dot chat slot: its WAJ code and the complete instruction to paste into that chat.
    page.click('button[data-cc-view="agents"]')
    page.fill("#cc-join-slot input[name=label]", "项目协调 dot")
    page.select_option("#cc-join-slot select[name=kind]", "dot")
    page.click("#cc-join-slot button[type=submit]")
    code = page.wait_for_selector(".cc-join-code").inner_text()
    assert WAJ.fullmatch(code), code
    instruction = page.input_value(".cc-join-instruction")
    assert instruction.startswith("@WebCoder") and code in instruction and "CodePier" not in instruction
    assert page.inner_text(".cc-join-card .cc-status") == "待在目标聊天加入"
    page.shot("collab-03-join-slot")

    # The chat joins with its existing read-only connection: registered, but no subscription yet (no fake "connected").
    joined = mcp(hub, work_token, "collaboration_join", code=code, idempotency_key=key())
    assert joined["registered"] and not joined["worker_authorized"] and not joined["permissions_changed"]
    page.click(".cc-room-menu > summary")
    page.click('.cc-room-menu button[data-cc-action="refresh"]')
    page.wait_for_selector(".cc-join-card >> text=等待宿主订阅")
    assert "有效订阅 0 / 4" in page.inner_text(".cc-join-card")
    assert "@WebCoder" in page.input_value(".cc-join-instruction")  # the instruction to finish subscribing

    # The owner lets that connection speak in this room.
    page.click('.cc-speaking button[data-cc-action="message-access"]')
    page.check("#cc-message-access input[name=confirm]")
    page.click("#cc-message-access button[type=submit]")
    page.wait_for_selector(".cc-speaking >> text=这个连接已获准在本房间发言")

    # Its read-only connection becomes a Work agent; a read/write connection is not offered.
    page.click(".cc-advanced > summary")
    page.click("#cc-agent-register > summary")
    offered = page.locator("#cc-agent select[name=grant_id] option").all_inner_texts()
    assert any("Work 只读" in text for text in offered) and not any("Codex 读写" in text for text in offered)
    page.fill("#cc-agent input[name=label]", "Work 分析")
    page.select_option("#cc-agent select[name=kind]", "work_cloud")
    page.select_option("#cc-agent select[name=grant_id]", work_grant)
    page.click("#cc-agent button[type=submit]")
    feedback(page, "Work Cloud 只读分析用途 7 天")
    page.wait_for_selector(".cc-agent-card >> text=用途绑定有效")
    page.shot("collab-04-agents")

    # The connection speaks over MCP; the open page polls it in without a reload, marked as unverified.
    page.click('button[data-cc-view="discussion"]')
    page.wait_for_selector("#cc-message-input")
    mcp(hub, work_token, "collaboration_message_create", **SCOPE, conversation_id=room["id"],
        room_id=room["projects"][0]["room_id"], body_text="我看了日志：第 3 步迁移超时。",
        client_message_id=key(), idempotency_key=key())
    said = page.locator(".cc-message", has_text="我看了日志：第 3 步迁移超时。")
    said.wait_for(timeout=15000)
    assert "身份未验证" in said.inner_text()

    # The owner's message becomes a read-only task for the Work agent, after explicit confirmation.
    owner_message = page.locator(".cc-message", has_text="先看一下 imago 的发布日志。")
    owner_message.locator('[data-cc-action="convert"]').click()
    page.select_option("#cc-task-review select[name=target_project]", project)
    page.select_option("#cc-task-review select[name=assignee]", index=1)
    page.check("#cc-task-review input[name=confirm]")
    page.click("#cc-task-review button[type=submit]")
    feedback(page, "已按确认范围创建只读任务")
    page.wait_for_selector(".cc-linked-card >> text=只读任务")
    page.shot("collab-05-task")

    # Goals: a plan first, then the owner approves the exact version; the first work item waits to be claimed.
    page.click('button[data-cc-view="jobs"]')
    page.wait_for_selector(".cc-coordination-list >> text=目标与工作项")
    page.click('.cc-coordination-list button[data-cc-action="goal-new"]')
    page.fill("#cc-goal-draft textarea[name=objective]", "找出发布失败的原因")
    page.fill("#cc-goal-draft textarea[name=acceptance]", "给出带日志证据的结论")
    page.check(f'#cc-goal-draft input[name=participant_grant_ids][value="{work_grant}"]')
    page.click("#cc-goal-draft button[type=submit]")
    feedback(page, "目标规划已保存，执行尚未启用。")
    page.click('.cc-coordination-list button[data-cc-action="goal-review"]')
    page.check("#cc-goal-approve input[name=confirm]")
    page.click("#cc-goal-approve button[type=submit]")
    feedback(page, "此目标版本已批准")
    page.click('.cc-coordination-list button[data-cc-action="goal-detail"]')
    page.wait_for_selector(".cc-goal-detail .cc-work-card >> text=等待领取")
    assert "暂无实际操作记录" in page.inner_text(".cc-goal-detail")
    page.shot("collab-06-goal")
    page.click('#cc-drawer .cc-drawer-header button[data-cc-action="close-drawer"]')

    # Monitoring: a fixed probe is registered without starting collection.
    page.click('button[data-cc-view="monitor"]')
    page.wait_for_selector("text=尚无计划")
    page.click("details:has(#cc-probe) > summary")
    page.fill("#cc-probe input[name=label]", "就绪检查")
    page.fill("#cc-probe input[name=url]", "https://imago.example/ready")
    page.check("#cc-probe input[name=confirm]")
    page.click("#cc-probe button[type=submit]")
    feedback(page, "探针已登记，尚未启动采集。")
    assert "链路状态" in page.inner_text(".cc-main")
    page.shot("collab-07-monitor")

    # The projects page links each project to its own room.
    page.click('#nav a[data-route="projects"]')
    page.click(f'a.table-link[href="#/collaboration/{project}"]')
    page.wait_for_function("() => document.querySelector('.cc-room-title h2')?.textContent === 'imago'")
    assert page.url.endswith("#/collaboration")

    # Leaving the page stops its polling; signing out from elsewhere then drops its memory-only state.
    page.click('#nav a[data-route="overview"]')
    page.wait_for_selector("text=MCP 地址")
    page.click("#logout")
    page.wait_for_selector("text=登录 WebCoder")
    assert page.problems == []


def test_dot_chat_subscriptions_test_events_and_receipt_confirmation(page, collab_hub):
    """No step shows as connected before it is: registered -> subscribed -> test accepted -> receipt confirmed."""
    hub = collab_hub
    project = hub.project["id"]
    dot_token = hub.create_token(scopes=("read",), projects=[project], label="dot 聊天")
    received = []

    async def receiver(url, body, headers):
        payload = json.loads(body)
        received.append(payload)
        if payload.get("type") == "verification":
            return Reply(200, json.dumps({"challenge": payload["challenge"]}).encode())
        return Reply(204)

    hub.runtime.collaboration.events.sender = receiver
    login(page, hub)
    page.click('#nav a[data-route="collaboration"]')
    page.click('button[data-cc-action="create-room"]')
    feedback(page, "项目讨论室已创建")
    assert page.inner_text(".cc-room-title h2") == "imago"

    page.click(".cc-text-button[data-cc-open-joins]")  # 智能体与订阅 ›
    page.fill("#cc-join-slot input[name=label]", "dot 协调")
    page.click("#cc-join-slot button[type=submit]")
    code = page.wait_for_selector(".cc-join-code").inner_text()
    joined = mcp(hub, dot_token, "collaboration_join", code=code.lower(), idempotency_key=key())
    assert len(joined["subscription_requests"]) == 4  # dot: tasks, status, results, incidents
    for request in joined["subscription_requests"]:
        subscribe(hub, dot_token, request)

    def refresh():
        page.click(".cc-room-menu > summary")
        page.click('.cc-room-menu button[data-cc-action="refresh"]')
        feedback(page, "状态已刷新。")

    refresh()
    card = page.locator(".cc-join-card")
    assert card.locator(".cc-status").first.inner_text() == "订阅回调已验证"
    assert "有效订阅 4 / 4 · 连接验证尚未完成" in card.inner_text()
    assert card.locator(".cc-join-routes li").count() == 4 and "尚未发送测试事件" in card.inner_text()
    page.shot("collab-10-subscribed")

    card.locator('button[data-cc-action="join-test"]').click()
    feedback(page, "测试已排队")
    hub.wait_for(lambda: sum(1 for item in received if item.get("data", {}).get("test")) == 4, timeout=30,
                 what="four test events delivered by the event loop")
    refresh()
    assert card.locator(".cc-status").first.inner_text() == "测试已获接收回执"
    assert card.inner_text().count("尚未确认目标聊天收件") == 4
    card.locator(".cc-join-confirm input[name=confirmed_received]").check()
    card.locator(".cc-join-confirm button[type=submit]").click()
    feedback(page, "已记录你的目标聊天收件确认")
    assert card.locator(".cc-status").first.inner_text() == "用户已确认聊天收件"
    assert "必需订阅与人工收件确认齐全" in card.inner_text()
    page.shot("collab-11-confirmed")

    # The owner can pause one subscription's delivery and resume it; the slot shows the gap meanwhile.
    page.click(".cc-advanced > summary")
    pause = '[data-cc-action="subscription_pause"]'
    subscriptions = page.locator(".cc-advanced article.cc-card", has=page.locator(pause))
    assert subscriptions.count() == 4
    subscriptions.first.locator(pause).click()
    feedback(page, "订阅已暂停投递。")
    assert card.locator(".cc-status").first.inner_text() != "用户已确认聊天收件"
    page.locator('.cc-advanced [data-cc-action="subscription_resume"]').click()
    feedback(page, "订阅已恢复投递")
    assert card.locator(".cc-status").first.inner_text() == "用户已确认聊天收件"
    page.shot("collab-12-resumed")
    assert page.problems == []


def test_mention_receipt_updates_while_the_page_polls(page, collab_hub):
    """A mention's receipt changes without any new message: the open discussion re-reads it as the page polls
    (waiting -> being delivered -> accepted by the host), never claiming more than the host's receipt."""
    import asyncio
    import threading

    hub = collab_hub
    project = hub.project["id"]
    dot_token = hub.create_token(scopes=("read",), projects=[project], label="dot 聊天")
    gate = threading.Event()

    async def receiver(url, body, headers):
        payload = json.loads(body)
        if payload.get("type") == "verification":
            return Reply(200, json.dumps({"challenge": payload["challenge"]}).encode())
        await asyncio.to_thread(gate.wait, 30)  # the host holds the mention until the test lets it through
        return Reply(204)

    hub.runtime.collaboration.events.sender = receiver
    login(page, hub)
    page.click('#nav a[data-route="collaboration"]')
    page.click('button[data-cc-action="create-room"]')
    feedback(page, "项目讨论室已创建")
    page.click(".cc-text-button[data-cc-open-joins]")  # 智能体与订阅 ›
    page.fill("#cc-join-slot input[name=label]", "dot 协调")
    page.click("#cc-join-slot button[type=submit]")
    code = page.wait_for_selector(".cc-join-code").inner_text()
    slot = mcp(hub, dot_token, "collaboration_join", code=code, idempotency_key=key())["slot"]["id"]
    room = hub.panel.get("/api/collaboration", params=SCOPE).json()["room"]["id"]
    subscribe(hub, dot_token, {"name": "webcoder.collaboration.message_mentioned.v1", "arguments": {
        "conversation_id": room, "project_id": project, "environment_id": "production", "slot_id": slot}})

    page.click('button[data-cc-view="discussion"]')
    page.click(".cc-room-menu > summary")
    page.click('.cc-room-menu button[data-cc-action="refresh"]')
    feedback(page, "状态已刷新。")
    page.click('button[data-cc-action="mentions"]')
    page.click(f'.cc-mention-option[data-id="{slot}"]')
    page.click('.cc-drawer button[data-cc-action="close-drawer"]')
    page.fill("#cc-message-input", "请汇总今天的发布状态。")
    page.press("#cc-message-input", "Enter")
    message = page.locator(".cc-message", has_text="请汇总今天的发布状态。")
    message.locator("text=正在投递").wait_for(timeout=20000)  # the event loop has it; the host has not answered yet
    gate.set()
    message.locator("text=宿主已接收 · 不代表已读").wait_for(timeout=20000)
    assert "已读" not in message.inner_text().replace("不代表已读", "")
    page.shot("collab-13-mention-receipt")
    assert page.problems == []


def test_proposal_decided_elsewhere_updates_while_the_page_polls(page, collab_hub):
    """A proposal rejected on another page changes no message sequence: the open discussion still shows the
    decision as it polls (the room's change feed), and stops offering the approval buttons."""
    hub = collab_hub
    project = hub.project["id"]
    work_token = hub.create_token(scopes=("read",), projects=[project], label="Work 只读")
    login(page, hub)
    page.click('#nav a[data-route="collaboration"]')
    page.click('button[data-cc-action="create-room"]')
    feedback(page, "项目讨论室已创建")
    room = hub.panel.get("/api/collaboration", params=SCOPE).json()["room"]["id"]
    agent = hub.panel.post("/api/collaboration/agent", json={**SCOPE, "label": "Work 分析", "kind": "work_cloud",
                                                             "grant_id": grant_id(hub, "Work 只读"),
                                                             "idempotency_key": key()}).json()
    proposal = mcp(hub, work_token, "collaboration_command_create", **SCOPE, room_id=room,
                   structured_mentions=[{"agent_id": agent["id"]}], request="Read the release log",
                   source_message_id=key(), idempotency_key=key())
    message = page.locator(f'[data-message-id="{proposal["id"]}"]')
    message.locator('[data-cc-action="reject_proposal"]').wait_for(timeout=20000)  # arrived by polling
    assert "待批准" in message.inner_text()
    decided = hub.panel.post("/api/collaboration/control", json={
        **SCOPE, "target_id": proposal["id"], "action": "reject_proposal", "expected_version": 1,
        "reason": "Not needed now", "idempotency_key": key()})
    assert decided.status_code == 200, decided.text
    message.locator("text=未批准").wait_for(timeout=20000)
    assert message.locator('[data-cc-action="accept_proposal"]').count() == 0
    page.shot("collab-14-proposal-decided")
    assert page.problems == []


def test_decision_right_after_load_and_a_failed_read_still_reach_the_page(page, collab_hub):
    """The change feed's position is taken before the timeline snapshot, so a decision made right after the page
    loaded is not skipped; and a message whose re-read fails stays pending until a later poll reads it."""
    hub = collab_hub
    project = hub.project["id"]
    work_token = hub.create_token(scopes=("read",), projects=[project], label="Work 只读")
    room = hub.panel.post("/api/collaboration/room", json={**SCOPE, "idempotency_key": key()}).json()["room"]["id"]
    agent = hub.panel.post("/api/collaboration/agent", json={**SCOPE, "label": "Work 分析", "kind": "work_cloud",
                                                             "grant_id": grant_id(hub, "Work 只读"),
                                                             "idempotency_key": key()}).json()
    proposal = mcp(hub, work_token, "collaboration_command_create", **SCOPE, room_id=room,
                   structured_mentions=[{"agent_id": agent["id"]}], request="Read the release log",
                   source_message_id=key(), idempotency_key=key())
    status_reads = []

    def first_status_read_fails(route):
        status_reads.append(route.request.url)
        route.abort() if len(status_reads) == 1 else route.continue_()

    page.route(re.compile(r".*/api/collaboration\?.*kind=message_status.*"), first_status_read_fails)
    login(page, hub)
    page.click('#nav a[data-route="collaboration"]')
    message = page.locator(f'[data-message-id="{proposal["id"]}"]')
    message.locator('[data-cc-action="reject_proposal"]').wait_for()  # loaded with the room, before any poll
    decided = hub.panel.post("/api/collaboration/control", json={
        **SCOPE, "target_id": proposal["id"], "action": "reject_proposal", "expected_version": 1,
        "reason": "Not needed now", "idempotency_key": key()})
    assert decided.status_code == 200, decided.text
    message.locator("text=未批准").wait_for(timeout=30000)
    assert len(status_reads) >= 2  # the failed read was retried
    assert message.locator('[data-cc-action="accept_proposal"]').count() == 0
    assert all("ERR_FAILED" in problem or "Failed to load resource" in problem for problem in page.problems)


def test_proposal_decided_between_a_polls_reads_still_reaches_the_page(page, collab_hub):
    """A proposal first seen in a poll's timeline and decided before that poll reads the change feed: the poll shows
    it before taking the feed's changes, so the decision is re-read instead of lost behind the advanced cursors."""
    hub = collab_hub
    project = hub.project["id"]
    work_token = hub.create_token(scopes=("read",), projects=[project], label="Work 只读")
    room = hub.panel.post("/api/collaboration/room", json={**SCOPE, "idempotency_key": key()}).json()["room"]["id"]
    agent = hub.panel.post("/api/collaboration/agent", json={**SCOPE, "label": "Work 分析", "kind": "work_cloud",
                                                             "grant_id": grant_id(hub, "Work 只读"),
                                                             "idempotency_key": key()}).json()
    login(page, hub)
    page.click('#nav a[data-route="collaboration"]')
    page.wait_for_selector("#cc-message-input")  # the room is open and its change feed started
    proposal, seen, decided = {}, [], []

    def timeline(route):
        response = route.fetch()
        if proposal and proposal["id"] in response.text():
            seen.append(route.request.url)
        route.fulfill(response=response)

    def changes(route):
        if seen and not decided:  # this poll's timeline brought the proposal: decide it before the feed is read
            decided.append(hub.panel.post("/api/collaboration/control", json={
                **SCOPE, "target_id": proposal["id"], "action": "reject_proposal", "expected_version": 1,
                "reason": "Not needed now", "idempotency_key": key()}))
        route.continue_()

    page.route(re.compile(r".*/api/collaboration\?.*kind=timeline.*"), timeline)
    page.route(re.compile(r".*/api/collaboration\?.*kind=changes.*"), changes)
    proposal.update(mcp(hub, work_token, "collaboration_command_create", **SCOPE, room_id=room,
                        structured_mentions=[{"agent_id": agent["id"]}], request="Read the release log",
                        source_message_id=key(), idempotency_key=key()))
    message = page.locator(f'[data-message-id="{proposal["id"]}"]')
    message.locator("text=未批准").wait_for(timeout=30000)
    page.unroute_all(behavior="ignoreErrors")  # no routed read left in flight when the hub stops
    assert len(decided) == 1 and decided[0].status_code == 200, decided[0].text if decided else "never decided"
    assert message.locator('[data-cc-action="accept_proposal"]').count() == 0
    assert page.problems == []
