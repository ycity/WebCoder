"""Phone CDP over fake adb and a real in-process HTTP/WebSocket DevTools peer; no real phone is operated."""

import base64
import json
import threading
import time

import pytest
from websockets.datastructures import Headers
from websockets.http11 import Response
from websockets.sync.server import serve

from tests import test_mobile as fixtures
from tests.test_mobile import code, tool_names
from webcoder.agent import mobile_web as web
from webcoder.common.errors import WebCoderError

ops, phone_computer, phones, project = fixtures.ops, fixtures.phone_computer, fixtures.phones, fixtures.project

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aL1kAAAAASUVORK5CYII=")


class DevTools:
    def __init__(self):
        self.calls = []
        self.extra_pages = []
        self.bad_ws = None
        self.list_fallback = False
        self.html = "<html><body><h1>手机 H5</h1></body></html>"
        self.history = 0
        self.redirect = None
        self.ready = threading.Event()
        self.port = 0

    def http(self, connection, request):
        if request.path in ("/json/list", "/json"):
            if request.path == "/json/list" and self.list_fallback:
                return Response(404, "Not Found", Headers(), b"")
            rows = [
                {
                    "id": "page-one",
                    "title": "H5 demo",
                    "url": "https://example.test/demo",
                    "type": "page",
                    "webSocketDebuggerUrl": self.bad_ws or f"ws://localhost:{self.port}/devtools/page/page-one",
                }
            ]
            body = json.dumps(rows + self.extra_pages).encode()
            return Response(
                200, "OK", Headers({"Content-Type": "application/json", "Content-Length": str(len(body))}), body
            )
        if request.path.startswith("/devtools/") and self.redirect:
            return Response(302, "Found", Headers({"Location": self.redirect, "Content-Length": "0"}), b"")
        return None

    def handle(self, ws):
        try:
            for raw in ws:
                command = json.loads(raw)
                self.calls.append(command)
                method = command["method"]
                params = command["params"]
                result = {}
                if method == "Runtime.enable":
                    for _ in range(self.history):
                        ws.send(
                            json.dumps(
                                {
                                    "method": "Runtime.consoleAPICalled",
                                    "params": {"type": "log", "args": [{"value": "old log"}]},
                                }
                            )
                        )
                elif method == "DOM.getDocument":
                    result = {"root": {"nodeId": 1}}
                elif method == "DOM.getOuterHTML":
                    result = {"outerHTML": self.html}
                elif method == "Runtime.evaluate":
                    result = {"result": {"type": "string", "value": "H5 value"}}
                    if params["expression"] == "throw new Error('bad')":
                        result = {"exceptionDetails": {"text": "Uncaught Error", "exception": {"description": "bad"}}}
                elif method == "Page.captureScreenshot":
                    result = {"data": base64.b64encode(PNG).decode()}
                elif method == "Page.navigate":
                    result = {"frameId": "frame-1"}
                elif method == "Page.reload":
                    for event, details in (
                        ("Runtime.consoleAPICalled", {"type": "log", "args": [{"value": "hello"}]}),
                        ("Network.responseReceived", {"response": {"url": "https://example.test/api", "status": 500}}),
                    ):
                        ws.send(json.dumps({"method": event, "params": details}))
                elif method == "CSS.fail":
                    ws.send(json.dumps({"id": command["id"], "error": {"message": "fixture failure"}}))
                    continue
                ws.send(json.dumps({"id": command["id"], "result": result}))
        except Exception:
            pass  # a test can revoke/close the socket while a command is running


@pytest.fixture
def devtools(monkeypatch):
    peer = DevTools()
    server = serve(peer.handle, "127.0.0.1", 0, process_request=peer.http)
    peer.port = server.socket.getsockname()[1]
    monkeypatch.setattr(web, "allocate_port", lambda: peer.port)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield peer
    finally:
        server.shutdown()
        thread.join(5)


def prepare(phones, devtools, **extra):
    phones.only(hdc=[], cdp_sockets=["webview_devtools_remote_123"], cdp_port=devtools.port, **extra)


def test_pages_dom_evaluate_and_events(ops, phones, devtools):
    prepare(phones, devtools)
    pages = ops.dispatch("mobile.web.pages", {})
    assert pages["pages"][0]["socket"] == "webview_devtools_remote_123"
    assert pages["pages"][0]["target_id"] == "page-one" and pages["serial"] == "emulator-5554"
    dom = ops.dispatch("mobile.web.dom", {})
    assert "手机 H5" in dom["html"] and not dom["truncated"]
    value = ops.dispatch("mobile.web.evaluate", {"expression": "document.title"})
    assert value["result"]["value"] == "H5 value"
    evaluate = next(c for c in devtools.calls if c["method"] == "Runtime.evaluate")
    assert evaluate["params"]["awaitPromise"] and evaluate["params"]["returnByValue"]
    assert ops.dispatch("mobile.web.evaluate", {"expression": "throw new Error('bad')"})["exceptionDetails"]
    devtools.history = web.MAX_EVENTS + 10
    events = ops.dispatch("mobile.web.events", {"seconds": 1, "reload": True})
    assert {e["method"] for e in events["events"]} == {"Runtime.consoleAPICalled", "Network.responseReceived"}
    assert not events["truncated"]
    created = [c for c in phones.calls() if "--no-rebind" in c]
    removed = [c for c in phones.calls() if "--remove" in c]
    assert len(created) == len(removed) == 5
    assert all(c[-2:] == ["--remove", f"tcp:{devtools.port}"] for c in removed)


def test_cdp_errors_cleanup_and_target_validation(ops, phones, devtools):
    prepare(phones, devtools)
    devtools.list_fallback = True
    assert ops.dispatch("mobile.web.dom", {})["html"] == devtools.html
    devtools.bad_ws = "ws://169.254.169.254/devtools/page/page-one"
    with pytest.raises(WebCoderError) as denied:
        ops.dispatch("mobile.web.dom", {})
    assert code(denied) == "MOBILE_CDP_UNAVAILABLE"
    assert phones.calls()[-1][-2:] == ["--remove", f"tcp:{devtools.port}"]
    devtools.bad_ws = None
    with pytest.raises(WebCoderError) as gone:
        ops.dispatch("mobile.web.dom", {"target_id": "missing"})
    assert code(gone) == "MOBILE_CDP_TARGET_GONE"
    devtools.extra_pages = [{"id": "page-two", "type": "page"}]
    with pytest.raises(WebCoderError) as ambiguous:
        ops.dispatch("mobile.web.dom", {})
    assert code(ambiguous) == "MOBILE_CDP_AMBIGUOUS"
    devtools.extra_pages = []
    with pytest.raises(WebCoderError) as bad:
        ops.dispatch("mobile.web.command", {"method": "CSS.fail"})
    assert code(bad) == "MOBILE_CDP_COMMAND_FAILED"
    for name in ("x; reboot", "../socket", "http://localhost:9222"):
        before = len(phones.calls())
        with pytest.raises(WebCoderError) as denied:
            ops.dispatch("mobile.web.dom", {"socket": name})
        assert code(denied) == "INVALID_ARGUMENT" and len(phones.calls()) == before


def test_mobile_switch_readonly_and_screenshot_boundary(ops, project, phones, devtools):
    prepare(phones, devtools)
    ops.mobile.enabled = False
    with pytest.raises(WebCoderError) as denied:
        ops.dispatch("mobile.web.pages", {})
    assert code(denied) == "MOBILE_DISABLED" and not phones.calls()
    ops.mobile.enabled = True
    ops.sb.read_only = True
    assert ops.dispatch("mobile.web.dom", {})["html"] == devtools.html
    for op, args in (
        ("evaluate", {"expression": "1"}),
        ("command", {"method": "Page.reload"}),
        ("events", {"reload": True}),
        ("screenshot", {"root": str(project), "path": "shot.png"}),
    ):
        before = len(phones.calls())
        with pytest.raises(WebCoderError) as denied:
            ops.dispatch("mobile.web." + op, args)
        assert code(denied) == "AGENT_READ_ONLY" and len(phones.calls()) == before
    ops.sb.read_only = False
    result = ops.dispatch("mobile.web.screenshot", {"root": str(project), "path": "h5/page.png"})
    assert result["size"] == len(PNG) and (project / "h5/page.png").read_bytes() == PNG
    for path in ("h5/page.png", "../outside.png", ".git/secret.png"):
        with pytest.raises(WebCoderError):
            ops.dispatch("mobile.web.screenshot", {"root": str(project), "path": path})


def test_revocation_closes_a_cdp_capture(ops, phones, devtools):
    prepare(phones, devtools)
    errors = []

    def capture():
        try:
            ops.dispatch("mobile.web.events", {"seconds": 30})
        except WebCoderError as exc:
            errors.append(exc.code)

    worker = threading.Thread(target=capture)
    worker.start()
    deadline = time.monotonic() + 5
    while not any(c["method"] == "Network.enable" for c in devtools.calls) and time.monotonic() < deadline:
        time.sleep(0.02)
    web.stop_all()
    worker.join(5)
    assert errors == ["STOPPED"] and not worker.is_alive()
    assert phones.calls()[-1][-2:] == ["--remove", f"tcp:{devtools.port}"]


def test_hdc_forward_owns_the_complete_pair_and_cleans_on_error():
    class Hdc:
        tool = "hdc"

        def __init__(self):
            self.calls = []
            self.listing = ""

        def text(self, args, serial, **kwargs):
            self.calls.append(args)
            if args == ["fport", "ls"]:
                return self.listing
            self.listing = f"HM1 {args[1]} {args[2]} [Forward]\n"
            return "Forwardport result:OK"

        def call(self, args, serial, **kwargs):
            self.calls.append(args)

        def remove_task(self, serial, local, remote):
            self.calls.append(["fport", "rm", local, remote])
            self.listing = ""

    backend = Hdc()
    with (
        pytest.raises(ValueError),
        web.Operation() as op,
        web.forward(backend, "HM1", "webview_devtools_remote_123", op),
    ):
        raise ValueError("operation failed")
    created = next(args for args in backend.calls if len(args) == 3 and args[1].startswith("tcp:"))
    assert backend.calls[-1] == ["fport", "rm", created[1], created[2]]
    with web.Operation() as op, web.forward(backend, "HM1", "webview_devtools_remote_123", op):
        backend.listing = backend.listing.replace("HM1", "someone-else")
    assert backend.calls[-1] == ["fport", "ls"]  # never remove someone else's forward


def test_phone_cdp_mcp_permissions_outputs_and_audit(hub, phone_computer, phones, devtools):
    prepare(phones, devtools)
    _, root = phone_computer()
    plain = hub.create_token()
    assert not any(name.startswith("mobile_web_") for name in tool_names(hub, plain))
    token = hub.create_token(scopes=("read", "write", "mobile"))
    assert {
        "mobile_web_pages",
        "mobile_web_dom",
        "mobile_web_evaluate",
        "mobile_web_events",
        "mobile_web_command",
        "mobile_web_screenshot",
    } <= tool_names(hub, token)
    assert "page-one" in hub.ok(token, "mobile_web_pages")
    assert "手机 H5" in hub.ok(token, "mobile_web_dom")
    assert "H5 value" in hub.ok(token, "mobile_web_evaluate", expression="document.title")
    assert "frame-1" in hub.ok(
        token, "mobile_web_command", method="Page.navigate", params={"url": "https://example.test", "referrer": "x"}
    )
    navigate = next(c for c in devtools.calls if c["method"] == "Page.navigate")
    assert navigate["params"] == {"url": "https://example.test", "referrer": "x"}  # what the page received
    shot = hub.rpc(
        token, "tools/call", {"name": "mobile_web_screenshot", "arguments": {"project": "pc-work", "path": "h5.png"}}
    ).json()["result"]
    assert not shot.get("isError") and (root / "h5.png").read_bytes() == PNG
    assert shot["_meta"]["webcoder/image"].startswith("data:image/png;base64,")
    audit = hub.runtime.store.all("SELECT action,target FROM audit WHERE action LIKE 'mobile_web_%'")
    assert any("page-one" in row["target"] for row in audit)


@pytest.mark.parametrize("operation", ["pages", "evaluate"])
def test_revocation_during_device_selection_stops_before_forward(ops, phones, monkeypatch, operation):
    def selecting(serial):
        web.stop_all()  # the Hub revokes while no adb client is alive
        return object(), "emulator-5554"

    monkeypatch.setattr(ops.mobile, "_target", selecting)
    args = {"expression": "document.title"} if operation == "evaluate" else {}
    with pytest.raises(WebCoderError) as error:
        ops.dispatch("mobile.web." + operation, args)
    assert code(error) == "STOPPED" and phones.calls() == []


def test_adb_mapping_is_recovered_if_client_fails_after_creation():
    class Adb:
        tool = "adb"
        mapping = ""
        removed = []

        def text(self, args, serial, **kwargs):
            if args == ["forward", "--list"]:
                return self.mapping
            self.mapping = f"{serial} {args[-2]} {args[-1]}\n"
            raise WebCoderError("MOBILE_TIMEOUT", "client failed after daemon created mapping")

        def call(self, args, serial, **kwargs):
            self.removed.append(args)
            self.mapping = ""

    adb = Adb()
    with pytest.raises(WebCoderError), web.Operation() as op, web.forward(adb, "A", "chrome_devtools_remote", op):
        pytest.fail("The failed creation must not yield")
    assert len(adb.removed) == 1 and adb.removed[0][:2] == ["forward", "--remove"]
    assert not adb.mapping


def test_cdp_deadline_is_enforced_even_when_events_are_always_ready(monkeypatch):
    from types import SimpleNamespace

    ticks = []

    def clock():
        ticks.append(1)
        if len(ticks) > 30:
            pytest.fail("CDP ignored its deadline while events kept arriving")
        return len(ticks) * 0.1

    monkeypatch.setattr(web, "time", SimpleNamespace(monotonic=clock))
    with web.Operation() as op:
        cdp = web.CDP(12345, {"webSocketDebuggerUrl": "ws://localhost:12345/devtools/page/one"}, op)
        cdp.ws = SimpleNamespace(send=lambda _: None)
        cdp.receive = lambda _: {"method": "Runtime.consoleAPICalled", "params": {}}
        with pytest.raises(TimeoutError):
            cdp.call("Runtime.evaluate", {"expression": "1"}, timeout=0.5)
    assert len(ticks) < 10


def test_handshake_redirect_cannot_leave_the_forward(ops, phones, devtools):
    prepare(phones, devtools)
    reached = []

    def redirected(connection, request):
        reached.append(request.path)
        return Response(400, "Bad Request", Headers({"Content-Length": "0"}), b"")

    server = serve(lambda _: None, "127.0.0.1", 0, process_request=redirected)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    devtools.redirect = f"ws://127.0.0.1:{server.socket.getsockname()[1]}/devtools/page/other"
    try:
        with pytest.raises(WebCoderError) as error:
            ops.dispatch("mobile.web.evaluate", {"expression": "1"})
        assert code(error) == "MOBILE_CDP_DISCONNECTED" and not reached
    finally:
        server.shutdown()
        worker.join(5)
    assert phones.calls()[-1][-2:] == ["--remove", f"tcp:{devtools.port}"]


def test_revocation_interrupting_cleanup_retries_and_reports_stopped():
    class Adb:
        tool = "adb"

        def __init__(self):
            self.mapping, self.listings, self.removals = "", 0, []

        def text(self, args, serial, **kwargs):
            if args == ["forward", "--list"]:
                self.listings += 1
                if self.listings == 2:
                    web.stop_all()
                    raise WebCoderError("STOPPED", "cleanup query was stopped")
                return self.mapping
            self.mapping = f"{serial} {args[-2]} {args[-1]}\n"
            return ""

        def call(self, args, serial, **kwargs):
            self.removals.append(args)
            self.mapping = ""

    adb = Adb()
    with (
        pytest.raises(WebCoderError) as error,
        web.Operation() as op,
        web.forward(adb, "A", "chrome_devtools_remote", op),
    ):
        pass
    assert code(error) == "STOPPED" and len(adb.removals) == 1 and not adb.mapping


@pytest.mark.parametrize("bad", ["ws://[::1/devtools/page/page-one", "ws://[/devtools/page/page-one"])
def test_a_malformed_page_socket_url_is_a_cdp_error(ops, phones, devtools, bad):
    # only its path is used (the connection always goes to our forward), so a malformed host must not crash
    prepare(phones, devtools)
    devtools.bad_ws = bad
    with pytest.raises(WebCoderError) as err:
        ops.dispatch("mobile.web.dom", {})
    assert code(err) == "MOBILE_CDP_UNAVAILABLE"


@pytest.mark.parametrize("data", [None, ["x"], 5, "not base64!"])
def test_a_screenshot_without_png_data_is_a_protocol_error(ops, project, phones, devtools, monkeypatch, data):
    prepare(phones, devtools)
    original = web.CDP.call

    def call(self, method, params=None, timeout=15):
        result = original(self, method, params, timeout)
        return {"data": data} if method == "Page.captureScreenshot" else result

    monkeypatch.setattr(web.CDP, "call", call)
    with pytest.raises(WebCoderError) as err:
        ops.dispatch("mobile.web.screenshot", {"root": str(project), "path": "bad.png"})
    assert code(err) == "MOBILE_CDP_PROTOCOL_ERROR" and not (project / "bad.png").exists()


@pytest.mark.parametrize("error", ["boom", ["boom"], None])
def test_a_malformed_cdp_error_is_still_a_cdp_error(ops, phones, devtools, monkeypatch, error):
    prepare(phones, devtools)

    def receive(self, timeout):
        return {"id": self.sequence, "error": error}

    monkeypatch.setattr(web.CDP, "receive", receive)
    with pytest.raises(WebCoderError) as err:
        ops.dispatch("mobile.web.command", {"method": "Page.reload"})
    assert code(err) == "MOBILE_CDP_COMMAND_FAILED"


def test_a_malformed_dom_is_a_protocol_error(ops, phones, devtools, monkeypatch):
    prepare(phones, devtools)
    original = web.CDP.call
    monkeypatch.setattr(web.CDP, "call", lambda self, method, params=None, timeout=15:
                        {"root": None} if method == "DOM.getDocument" else original(self, method, params, timeout))
    with pytest.raises(WebCoderError) as err:
        ops.dispatch("mobile.web.dom", {})
    assert code(err) == "MOBILE_CDP_PROTOCOL_ERROR"


@pytest.mark.parametrize("result", [None, ["x"], "text", 5, "missing"])
def test_a_non_object_cdp_result_is_a_protocol_error(ops, phones, devtools, monkeypatch, result):
    prepare(phones, devtools)
    answer = (lambda self: {"id": self.sequence}) if result == "missing" else (
        lambda self: {"id": self.sequence, "result": result})
    monkeypatch.setattr(web.CDP, "receive", lambda self, timeout: answer(self))
    with pytest.raises(WebCoderError) as err:
        ops.dispatch("mobile.web.evaluate", {"expression": "1"})
    assert code(err) == "MOBILE_CDP_PROTOCOL_ERROR"


def test_a_dom_answer_without_html_is_a_protocol_error(ops, phones, devtools, monkeypatch):
    prepare(phones, devtools)
    original = web.CDP.call
    monkeypatch.setattr(web.CDP, "call", lambda self, method, params=None, timeout=15:
                        {} if method == "DOM.getOuterHTML" else original(self, method, params, timeout))
    with pytest.raises(WebCoderError) as err:  # not an empty page
        ops.dispatch("mobile.web.dom", {})
    assert code(err) == "MOBILE_CDP_PROTOCOL_ERROR"


@pytest.mark.parametrize("method", sorted(web.CODE_METHODS))
def test_code_runs_only_through_evaluate(ops, phones, devtools, method):
    prepare(phones, devtools)
    before = len(phones.calls())
    with pytest.raises(WebCoderError) as err:
        ops.dispatch("mobile.web.command", {"method": method, "params": {"expression": "1"}})
    assert code(err) == "INVALID_ARGUMENT" and "mobile_web_evaluate" in err.value.message
    assert len(phones.calls()) == before and not devtools.calls  # refused before reaching the phone


def test_evaluated_code_is_audited_whole(hub, phone_computer, phones, devtools):
    prepare(phones, devtools)
    phone_computer()
    token = hub.create_token(scopes=("read", "mobile"), label="phone")
    expression = "/* long */ " + "x;" * 4000 + "document.title"
    hub.ok(token, "mobile_web_evaluate", expression=expression)
    target = hub.runtime.store.all("SELECT target FROM audit WHERE action='mobile_web_evaluate'")[-1]["target"]
    assert target.endswith(expression)


def test_a_cdp_command_is_audited_with_its_params(hub, phone_computer, phones, devtools):
    prepare(phones, devtools)
    phone_computer()
    token = hub.create_token(scopes=("read", "mobile"), label="phone")
    hub.ok(token, "mobile_web_command", method="Page.reload", params={"scriptToEvaluateOnLoad": "steal()"})
    target = hub.runtime.store.all("SELECT target FROM audit WHERE action='mobile_web_command'")[-1]["target"]
    assert target.endswith('Page.reload {"scriptToEvaluateOnLoad": "steal()"}')  # the code it carries too
