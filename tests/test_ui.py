"""Pictures in the conversation: the MCP Apps image viewer resource, view_image, and the viewer in a browser."""
import base64
import json

import pytest

from webcoder.hub import tools as hub_tools
from webcoder.hub import ui

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQ"
                       "AAAABJRU5ErkJggg==")  # 1x1 pixel


def call(hub, token, tool, **arguments) -> dict:
    response = hub.rpc(token, "tools/call", {"name": tool, "arguments": arguments})
    assert response.status_code == 200, response.text
    return response.json()["result"]


def test_the_viewer_is_an_mcp_apps_resource(hub):
    token = hub.create_token()
    start = hub.rpc(token, "initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                                          "clientInfo": {"name": "test", "version": "1"}}).json()["result"]
    assert "resources" in start["capabilities"]
    listed = hub.rpc(token, "resources/list").json()["result"]["resources"]
    assert [item["uri"] for item in listed] == [ui.IMAGE_VIEWER] and listed[0]["mimeType"] == ui.MIME
    page = hub.rpc(token, "resources/read", {"uri": ui.IMAGE_VIEWER}).json()["result"]["contents"][0]
    assert page["mimeType"] == "text/html;profile=mcp-app" and "ui/initialize" in page["text"]
    assert "src=\"http" not in page["text"] and "<script src" not in page["text"]  # self-contained: default CSP
    missing = hub.rpc(token, "resources/read", {"uri": "ui://webcoder/other.html"}).json()
    assert missing["error"]["code"] == -32002
    tools = {tool["name"]: tool for tool in hub.rpc(token, "tools/list").json()["result"]["tools"]}
    assert tools["view_image"]["_meta"]["ui"] == {"resourceUri": ui.IMAGE_VIEWER}
    assert tools["view_image"]["_meta"]["openai/outputTemplate"] == ui.IMAGE_VIEWER
    assert "ui" not in tools["read_file"]["_meta"]  # only tools that show something get a component


def test_view_image_shows_a_project_picture(hub, start_agent, tmp_path, monkeypatch):
    root = tmp_path / "work"
    device_id, _ = start_agent("pc", root)
    hub.add_project("app", device_id, root)
    (root / "shots").mkdir()
    (root / "shots" / "home.png").write_bytes(PNG)
    (root / "notes.txt").write_bytes(b"not a picture")
    (root / "logo.svg").write_bytes(b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>")
    token = hub.create_token(scopes=("read",), label="viewer")
    shown = call(hub, token, "view_image", project="app", path="shots/home.png")
    assert not shown.get("isError")
    assert shown["_meta"][ui.IMAGE_META] == "data:image/png;base64," + base64.b64encode(PNG).decode()
    assert shown["structuredContent"]["caption"] == "app:shots/home.png"
    assert shown["structuredContent"]["mimeType"] == "image/png"
    # the picture goes to the component only, not into the model's context
    assert "data:" not in shown["content"][0]["text"] and "data:" not in json.dumps(shown["structuredContent"])
    for path, code in (("notes.txt", "NOT_AN_IMAGE"), ("logo.svg", "NOT_AN_IMAGE"), ("gone.png", "NOT_FOUND"),
                       (".env", "PATH_DENIED")):
        text, is_error = hub.call(token, "view_image", project="app", path=path)
        assert is_error and code in text, path
    monkeypatch.setattr(hub_tools, "IMAGE_MAX_BYTES", 10)
    text, is_error = hub.call(token, "view_image", project="app", path="shots/home.png")
    assert is_error and "FILE_TOO_LARGE" in text


# ------------------------------------------------------------------ the viewer itself, in a real browser
HOST = """<!doctype html><meta charset="utf-8"><body>
<iframe id="view" sandbox="allow-scripts" style="width:420px;height:600px;border:0"></iframe>
<script>
const frame = document.getElementById("view");
window.seen = [];
window.send = (params) => frame.contentWindow.postMessage(
  {jsonrpc: "2.0", method: "ui/notifications/tool-result", params}, "*");
window.addEventListener("message", (event) => {
  if (event.source !== frame.contentWindow) return;
  const message = event.data;
  window.seen.push(message.method);
  if (message.method === "ui/initialize") frame.contentWindow.postMessage({jsonrpc: "2.0", id: message.id,
    result: {protocolVersion: "2026-01-26", hostCapabilities: {}, hostInfo: {name: "test", version: "1"},
             hostContext: {}}}, "*");
  if (message.method === "ui/notifications/initialized") window.send(RESULT);
});
frame.srcdoc = VIEWER;
</script>"""


@pytest.fixture
def browser_page(ui_browser):
    with ui_browser.new_context() as context:
        page = context.new_page()
        page.errors = []
        page.on("pageerror", lambda exc: page.errors.append(str(exc)))
        yield page


def test_the_viewer_draws_the_picture_the_host_sends(browser_page, tmp_path):
    html = ui.resource_read(ui.IMAGE_VIEWER)["contents"][0]["text"]
    result = {"content": [{"type": "text", "text": "Showing app:shots/home.png"}],
              "structuredContent": {"caption": "app:shots/home.png"},
              "_meta": {ui.IMAGE_META: "data:image/png;base64," + base64.b64encode(PNG).decode()}}
    host = tmp_path / "host.html"

    def inline(value) -> str:  # a JS literal inside <script>: "</script>" in it would end the host's script
        return json.dumps(value).replace("</", "<\\/")
    host.write_text(HOST.replace("VIEWER", inline(html)).replace("RESULT", inline(result)), encoding="utf-8")
    page = browser_page
    page.goto(host.as_uri())
    image = page.frame_locator("#view").locator("#image")
    page.wait_for_function("window.seen.includes('ui/notifications/size-changed')", timeout=10_000)
    assert image.evaluate("img => img.naturalWidth") == 1 and image.get_attribute("alt") == "app:shots/home.png"
    assert page.frame_locator("#view").locator("#caption").inner_text() == "app:shots/home.png"
    assert page.evaluate("window.seen")[:2] == ["ui/initialize", "ui/notifications/initialized"]
    # anything but an inline raster image is ignored
    for bad in ("https://example.com/x.png", "javascript:alert(1)", "data:image/svg+xml;base64,PHN2Zy8+"):
        page.evaluate("data => window.send({structuredContent: {caption: 'x'}, _meta: {'webcoder/image': data}})", bad)
        page.wait_for_timeout(100)
        assert image.get_attribute("src").startswith("data:image/png;base64,")
    assert not page.errors
