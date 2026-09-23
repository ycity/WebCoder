"""Headless browser smoke test of the web panel (skipped without Playwright + Edge/Chromium).

Set WEBCODER_SCREENSHOTS=<folder> to keep screenshots of every page.
"""
import os
import secrets
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from tests.conftest import ADMIN_PASSWORD
from webcoder.common.crypto import pkce_challenge

sync_api = pytest.importorskip("playwright.sync_api")


@pytest.fixture
def page(hub):
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

        def shot(name):
            if shots:
                Path(shots).mkdir(parents=True, exist_ok=True)
                tab.screenshot(path=str(Path(shots) / f"{name}.png"), full_page=True)

        tab.shot = shot
        yield tab
        browser.close()


def problems(page, *provoked):
    """Script and console errors, minus failed requests the test provoked on purpose (by HTTP status)."""
    expected = tuple(f"status of {status} (" for status in provoked)
    return [p for p in page.problems
            if not (p.startswith("console: Failed to load resource") and any(e in p for e in expected))]


def login(page, hub, username="admin", password=ADMIN_PASSWORD):
    page.goto(hub.url + "/")
    page.fill("input[name=username]", username)
    page.fill("input[name=password]", password)
    page.click("button[type=submit]")
    page.wait_for_selector("text=MCP 地址")


def test_panel_pages_and_actions(page, hub, start_agent, tmp_path):
    root = tmp_path / "work"
    start_agent("workstation", root)
    (root / "site").mkdir()
    page.goto(hub.url + "/")
    page.wait_for_selector("text=登录 WebCoder")
    page.shot("01-login")
    login(page, hub)
    page.shot("02-overview")

    page.click("#nav a[data-route=devices]")
    page.wait_for_selector("text=workstation")
    assert page.locator("text=在线").count() >= 1
    page.click("#add-device")  # folders can come later: a computer that is only for phones or SSH
    page.fill("#device-form input[name=name]", "phone-box")
    page.click("#device-form button[type=submit]")
    page.wait_for_selector("#modal:not([hidden]) >> text=不开放任何文件夹")
    page.click("#modal button.primary[data-close]")
    page.wait_for_selector("td >> text=phone-box")
    page.click("#add-device")
    page.fill("#device-form input[name=name]", "laptop")
    page.fill("#device-form textarea[name=roots]", "work")
    page.click("#device-form button[type=submit]")
    page.wait_for_selector("#device-error >> text=不是绝对路径")
    page.fill("#device-form textarea[name=roots]", "C:\\Users\\me\\WebCoder\nD:\\code")
    assert not page.is_checked("#device-form input[name=mobile]")  # phone control is opt-in
    page.check("#device-form input[name=mobile]")
    page.shot("03-devices-add")
    page.click("#device-form button[type=submit]")
    page.wait_for_selector("#modal:not([hidden]) >> text=在「laptop」上安装 Agent")
    modal = page.inner_text("#modal")
    assert "/downloads/install-agent.ps1" in modal and "-Root 'C:\\Users\\me\\WebCoder','D:\\code'" in modal
    assert "开机自动运行" in modal and "-NoAutostart" not in modal and modal.count("-Mobile") == 1
    page.shot("03-devices-install")
    page.click("#modal button.primary[data-close]")
    page.wait_for_selector("td >> text=laptop")
    page.locator("tr", has_text="workstation").locator("button[data-action=roots]").click()
    page.wait_for_selector("#roots-form")
    assert str(root) in page.input_value("#roots-form textarea[name=roots]")
    page.fill("#roots-form textarea[name=roots]", str(root) + "\n" + str(tmp_path / "more"))
    page.check("#roots-form input[name=mobile]")
    page.click("#roots-form button[type=submit]")
    page.wait_for_selector("#roots-command >> text=roots set --create")
    assert "webcoder.agent mobile on" in page.inner_text("#roots-command")
    page.shot("03-devices-roots")
    page.click("#modal [data-close]")

    page.click("#nav a[data-route=projects]")
    page.wait_for_selector("#add-project")
    page.fill("#add-project input[name=alias]", "site")
    page.fill("#add-project input[name=root]", str(root / "site"))
    page.click("#add-project button[type=submit]")
    page.wait_for_selector("td >> text=site")
    page.shot("04-projects")

    page.click("#nav a[data-route=tokens]")
    page.fill("#add-token input[name=label]", "chatgpt tunnel")
    page.click("#add-token button[type=submit]")
    page.wait_for_selector("#modal:not([hidden]) >> text=访问令牌已创建")
    modal = page.inner_text("#modal")
    assert "wc_pat_" in modal and ".webcoder\\token.txt" in modal and "codex mcp add" in modal
    assert "claude" not in modal.lower()
    page.shot("05-token-created")
    page.click("#modal [data-close]")
    page.wait_for_selector("td >> text=chatgpt tunnel")

    page.click("#nav a[data-route=clients]")
    page.wait_for_selector("text=接入客户端")
    assert "chatgpt.com/plugins" in page.inner_text("#tab-body")
    page.shot("06-clients-chatgpt-oauth")
    with page.expect_download() as download:
        page.click("#download-plugin")
    assert download.value.suggested_filename.startswith("webcoder-") and \
        download.value.suggested_filename.endswith(".zip")
    page.click(".tabs button[data-tab=chatgpt-tunnel]")
    page.wait_for_selector("text=tunnel-client init")
    assert "No Authentication" in page.inner_text("#tab-body")
    page.shot("06-clients-chatgpt-tunnel")
    page.click(".tabs button[data-tab=codex]")
    page.wait_for_selector("text=bearer_token_env_var")

    page.click("#nav a[data-route=audit]")
    page.wait_for_selector("text=project.created")
    page.shot("07-audit")

    page.click("#nav a[data-route=devices]")
    page.locator("tr", has_text="workstation").locator("button[data-action=delete]").click()
    page.wait_for_selector("#modal:not([hidden]) >> text=删除设备「workstation」？")
    modal = page.inner_text("#modal")
    assert "立即自动停止" in modal and ".webcoder-agent" in page.inner_text("#modal pre")
    page.shot("08-device-delete")
    page.click("#confirm-delete")
    page.wait_for_selector("#modal >> text=已删除「workstation」")  # stays open so the command can be copied
    page.click("#modal [data-close]")
    page.wait_for_selector("tr >> text=workstation", state="detached")
    start_agent.runners[-1].thread.join(20)
    assert not start_agent.runners[-1].thread.is_alive()  # the agent retired itself
    assert problems(page, 400) == []  # 400: the relative folder path rejected on purpose


def test_oauth_consent_page_redirects_back_to_client(page, hub, start_agent, tmp_path):
    start_agent("workstation", tmp_path / "work")
    redirect = "http://127.0.0.1:47123/callback"
    client_id = hub.http.post("/oauth/register", json={"client_name": "Codex", "redirect_uris": [redirect]}
                              ).json()["client_id"]
    verifier = secrets.token_urlsafe(48)
    authorize = hub.http.get("/oauth/authorize", params={
        "client_id": client_id, "redirect_uri": redirect, "response_type": "code", "state": "s1",
        "code_challenge": pkce_challenge(verifier), "code_challenge_method": "S256", "scope": "read write"})
    consent_url = hub.url + authorize.headers["location"]
    captured = {}
    page.route("http://127.0.0.1:47123/**", lambda route: (captured.setdefault("url", route.request.url),
                                                           route.fulfill(status=200, body="ok")))
    page.goto(consent_url)
    page.fill("input[name=username]", "admin")
    page.fill("input[name=password]", ADMIN_PASSWORD)
    page.click("button[type=submit]")
    page.wait_for_selector("text=授权 Codex 访问 WebCoder")
    assert "本机地址" in page.inner_text("main") and "当前登录：admin" in page.inner_text("main")
    page.shot("08-consent")
    page.click("#consent button[type=submit]")
    page.wait_for_function("() => document.body.innerText.includes('ok')")
    query = parse_qs(urlsplit(captured["url"]).query)
    assert query["state"] == ["s1"] and query["iss"] == [hub.url] and query["code"][0].startswith("wc_code_")
    assert page.problems == []


def test_invite_reset_and_isolation(page, hub, start_agent, tmp_path):
    start_agent("admin-pc", tmp_path / "admin-work")
    login(page, hub)
    page.click("#nav a[data-route=users]")
    page.fill("#invite-form input[name=note]", "Alice")
    page.click("#invite-form button[type=submit]")
    page.wait_for_selector("#modal:not([hidden]) >> text=邀请链接已生成")
    invite_url = page.inner_text("#modal pre")
    assert invite_url.startswith(hub.url + "/#/invite/")
    page.click("#modal [data-close]")
    assert "Alice" in page.inner_text("#users-view")
    page.shot("09-users")

    page.goto(invite_url)
    page.wait_for_selector("text=加入 WebCoder")
    assert "Alice" in page.inner_text("main") and "当前以 admin 登录" in page.inner_text("main")
    page.fill("#join-form input[name=username]", "alice")
    page.fill("#join-form input[name=password]", "alice-password-1")
    page.fill("#join-form input[name=again]", "alice-password-2")
    page.click("#join-form button[type=submit]")
    page.wait_for_selector("#join-form >> text=两次输入的密码不一致")
    page.fill("#join-form input[name=again]", "alice-password-1")
    page.shot("10-invite")
    page.click("#join-form button[type=submit]")
    page.wait_for_selector("text=MCP 地址")
    assert page.inner_text("#who") == "alice" and "/invite/" not in page.url
    assert page.locator('#nav a[data-route="users"]').is_hidden()
    page.click("#nav a[data-route=devices]")
    page.wait_for_selector("text=还没有设备")          # the admin's device stays invisible
    page.goto(hub.url + "/#/users")
    page.wait_for_selector("text=只有管理员可以执行此操作")

    page.goto(invite_url)                                 # single use
    page.wait_for_selector("text=链接无效、已使用或已过期")

    alice = next(u for u in hub.panel.get("/api/users").json()["users"] if u["username"] == "alice")
    reset_url = hub.panel.post(f"/api/users/{alice['id']}/reset-link").json()["url"]
    page.goto(reset_url)
    page.wait_for_selector("text=为账号 alice 设置新密码")
    page.fill("#reset-form input[name=password]", "alice-password-new")
    page.fill("#reset-form input[name=again]", "alice-password-new")
    page.click("#reset-form button[type=submit]")
    page.wait_for_selector("text=MCP 地址")
    page.click("#logout")
    login(page, hub, "alice", "alice-password-new")
    assert problems(page, 403, 404) == []  # 403: users page as non-admin, 404: reused invite
