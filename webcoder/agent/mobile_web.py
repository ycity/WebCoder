"""CDP on phone Chrome/WebView/ArkWeb sockets, reachable only through short-lived adb / hdc forwards.

No caller-supplied host or WebSocket URL is used. Each call rediscovers the socket and page on the selected phone,
talks only to the local forward and removes it in finally. Event capture lasts only for the call.
"""

from __future__ import annotations

import base64
import contextlib
import http.client
import json
import re
import socket
import threading
import time
from urllib.parse import urlsplit

from websockets.exceptions import WebSocketException
from websockets.sync.client import connect

from webcoder.agent.mobile import _has_task
from webcoder.common.errors import WebCoderError

SOCKET_RE = re.compile(r"^[A-Za-z0-9_.-]*devtools_remote[A-Za-z0-9_.-]*$")
TARGET_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,256}$")
METHOD_RE = re.compile(
    r"^(DOM|CSS|Page|Runtime|Network|Log|Input|Accessibility|Emulation|Debugger|Profiler)\.[A-Za-z]+$"
)
# Methods that run code on the page: only mobile_web_evaluate runs JavaScript, so the hub audits all of it.
CODE_METHODS = {"Runtime.evaluate", "Runtime.callFunctionOn", "Runtime.compileScript", "Runtime.runScript",
                "Debugger.evaluateOnCallFrame", "Debugger.setScriptSource", "Page.addScriptToEvaluateOnNewDocument"}
MAX_JSON = 8 * 1024 * 1024
MAX_EVENTS = 500
MAX_EVENT_BYTES = 1024 * 1024
_active: set = set()
_lock = threading.Lock()
_forward_lock = threading.Lock()


class Operation:
    def __init__(self):
        self.stopped = threading.Event()
        self.ws = None

    def check(self):
        if self.stopped.is_set():
            raise WebCoderError("STOPPED", "Stopped: this device was disabled or deleted in the hub panel")

    def __enter__(self):
        with _lock:
            _active.add(self)
        return self

    def __exit__(self, *_):
        with _lock:
            _active.discard(self)


def stop_all():
    with _lock:
        victims = list(_active)
        for op in victims:
            op.stopped.set()
    for op in victims:
        if op.ws is not None:
            with contextlib.suppress(Exception):
                op.ws.close()


def _sockets(backend, serial):
    text = backend.text(["shell", "cat", "/proc/net/unix"], serial, timeout=10)
    return sorted(
        {
            line.split()[-1].lstrip("@")
            for line in text.splitlines()
            if line.split() and line.split()[-1].startswith("@") and SOCKET_RE.fullmatch(line.split()[-1][1:])
        }
    )[:16]


def allocate_port():
    with socket.socket() as reserve:
        reserve.bind(("127.0.0.1", 0))
        return reserve.getsockname()[1]


@contextlib.contextmanager
def forward(backend, serial, name, op):
    op.check()
    local = None
    remote = "localabstract:" + name
    listing_args = ["forward", "--list"] if backend.tool == "adb" else ["fport", "ls"]
    try:
        # Know the endpoint before starting the command: a daemon may create its mapping even if its client dies.
        # no-rebind (adb), or hdc's bind refusal, prevents replacing an existing listener.
        with _forward_lock:
            op.check()
            port = allocate_port()
            candidate = f"tcp:{port}"
            existing = backend.text(listing_args, serial, timeout=10)
            op.check()
            if re.search(r"(?<![0-9])" + re.escape(candidate) + r"(?![0-9])", existing):
                raise WebCoderError("MOBILE_CDP_UNAVAILABLE", "The allocated forwarding port is already in use")
            local = candidate
            args = ["forward", "--no-rebind", local, remote] if backend.tool == "adb" else ["fport", local, remote]
            value = backend.text(args, serial, timeout=10)
            if backend.tool == "hdc" and "forwardportresult:ok" not in value.lower().replace(" ", ""):
                raise WebCoderError(
                    "MOBILE_CDP_UNAVAILABLE", "hdc refused the DevTools forwarding task: " + value.strip()[-300:]
                )
        op.check()
        yield port
    finally:
        if local:
            args = ["forward", "--remove", local] if backend.tool == "adb" else ["fport", "rm", local, remote]
            for attempt in range(3):
                try:
                    listing = backend.text(listing_args, serial, timeout=10)
                    owned = any(
                        _has_task(line.split(), serial, local, remote) and "[Reverse]" not in line
                        for line in listing.splitlines()
                    )
                    if owned and backend.tool == "hdc":
                        backend.remove_task(serial, local, remote)  # confirmed: hdc's exit code means nothing
                    elif owned:
                        backend.call(args, serial, timeout=10)
                    break
                except WebCoderError:
                    if attempt == 2:
                        raise WebCoderError(
                            "MOBILE_CDP_CLEANUP_FAILED",
                            f"Cannot clean up {local} → {remote}; "
                            "check adb forward --list / hdc fport ls on this computer",
                        ) from None
            op.check()  # revocation during cleanup must not produce a success response


def targets(port):
    client = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    try:
        client.request("GET", "/json/list")
        response = client.getresponse()
        if response.status == 404:
            response.read(MAX_JSON + 1)
            client.request("GET", "/json")  # ArkWeb documents this endpoint
            response = client.getresponse()
        if response.status != 200:
            raise WebCoderError("MOBILE_CDP_UNAVAILABLE", "The phone's DevTools socket did not return a page list")
        raw = response.read(MAX_JSON + 1)
        if len(raw) > MAX_JSON:
            raise WebCoderError("MOBILE_CDP_TOO_LARGE", "The phone's page list is too large")
        value = json.loads(raw)
        if not isinstance(value, list):
            raise ValueError()
        return [
            row
            for row in value[:100]
            if isinstance(row, dict)
            and row.get("type") in ("page", "webview")
            and isinstance(row.get("id"), str)
            and TARGET_RE.fullmatch(row["id"])
        ]
    except (OSError, ValueError, http.client.HTTPException):
        raise WebCoderError(
            "MOBILE_CDP_UNAVAILABLE",
            "Cannot read DevTools pages: enable Chrome/WebView/ArkWeb web debugging and keep the page open",
        ) from None
    finally:
        client.close()


def page_view(name, row):
    return {
        "socket": name,
        "target_id": row["id"],
        "title": str(row.get("title", ""))[:500],
        "url": str(row.get("url", ""))[:2048],
        "type": row["type"],
    }


class CDP:
    def __init__(self, port, row, op):
        try:
            parsed = urlsplit(str(row.get("webSocketDebuggerUrl", "")))
            host = parsed.hostname
        except ValueError:  # e.g. an unclosed IPv6 bracket from the phone
            raise WebCoderError("MOBILE_CDP_UNAVAILABLE",
                                "The phone returned an invalid page DevTools WebSocket path") from None
        if (
            parsed.scheme != "ws"
            or host not in ("localhost", "127.0.0.1", "::1")
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or not re.fullmatch(r"/devtools/page/[A-Za-z0-9_.:-]{1,256}", parsed.path)
        ):
            raise WebCoderError("MOBILE_CDP_UNAVAILABLE", "The phone returned an invalid page DevTools WebSocket path")
        self.port = port
        self.url = f"ws://127.0.0.1:{port}{parsed.path}"
        self.op, self.sequence, self.events, self.event_bytes, self.dropped = op, 0, [], 0, False

    def __enter__(self):
        self.op.check()
        transport = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        try:
            self.op.check()
            # A preconnected socket fixes the peer; websockets cannot follow handshake redirects with it.
            self.ws = connect(self.url, sock=transport, proxy=None, open_timeout=5, close_timeout=1, max_size=MAX_JSON)
        except ValueError:
            transport.close()
            self.op.check()
            raise WebCoderError(
                "MOBILE_CDP_DISCONNECTED",
                "The phone's CDP handshake was rejected; redirects outside the temporary forward are not allowed",
            ) from None
        except BaseException:
            transport.close()
            raise
        self.op.ws = self.ws
        try:
            self.op.check()
        except WebCoderError:
            self.ws.close()
            self.op.ws = None
            raise
        return self

    def __exit__(self, *_):
        self.ws.close()
        self.op.ws = None

    def receive(self, timeout):
        self.op.check()
        raw = self.ws.recv(timeout=timeout)
        try:
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError()
            return value
        except ValueError:
            raise WebCoderError("MOBILE_CDP_PROTOCOL_ERROR", "The phone returned invalid CDP JSON") from None

    def remember(self, event):
        if len(self.events) >= MAX_EVENTS:
            self.dropped = True
            return
        encoded = json.dumps(event, ensure_ascii=True)
        if self.event_bytes + len(encoded) > MAX_EVENT_BYTES:
            self.dropped = True
            return
        self.event_bytes += len(encoded)
        self.events.append(event)

    def call(self, method, params=None, timeout=15):
        self.op.check()
        self.sequence += 1
        identifier = self.sequence
        self.ws.send(json.dumps({"id": identifier, "method": method, "params": params or {}}, allow_nan=False))
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("CDP command deadline exceeded")
            value = self.receive(remaining)
            if value.get("id") == identifier:
                if "error" in value:
                    error = value["error"]
                    text = error.get("message", "CDP command failed") if isinstance(error, dict) else error
                    message = str(text)[:2000]
                    raise WebCoderError("MOBILE_CDP_COMMAND_FAILED", f"{method}: {message}")
                result = value.get("result")
                if not isinstance(result, dict):  # CDP: every answer carries an object result, or an error
                    raise WebCoderError("MOBILE_CDP_PROTOCOL_ERROR", f"{method}: the page returned a malformed result")
                return result
            if "method" in value:
                self.remember(value)

    def collect(self, seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline and not self.dropped:
            try:
                value = self.receive(min(1, max(0, deadline - time.monotonic())))
            except TimeoutError:
                continue
            if "method" in value:
                self.remember(value)


class MobileWeb:
    def __init__(self, mobile):
        self.mobile = mobile

    def pages(self, serial=None):
        pages, errors = [], []
        with Operation() as op:
            backend, found = self.mobile._target(serial)
            op.check()
            for name in _sockets(backend, found):
                op.check()
                try:
                    with forward(backend, found, name, op) as port:
                        pages.extend(page_view(name, row) for row in targets(port))
                except WebCoderError as exc:
                    if exc.code in ("STOPPED", "MOBILE_CDP_CLEANUP_FAILED"):  # a mapping may be left: not a success
                        raise
                    errors.append({"socket": name, "code": exc.code, "message": exc.message})
        return {
            "serial": found,
            "platform": backend.platform,
            "pages": pages,
            "errors": errors,
            "hint": "Enable WebView/ArkWeb web debugging if no pages are listed; USB debugging alone is not enough",
        }

    @contextlib.contextmanager
    def page(self, serial, name, target_id):
        if name is not None and (not isinstance(name, str) or not SOCKET_RE.fullmatch(name)):
            raise WebCoderError("INVALID_ARGUMENT", "socket must be a DevTools socket from mobile_web_pages")
        if target_id is not None and (not isinstance(target_id, str) or not TARGET_RE.fullmatch(target_id)):
            raise WebCoderError("INVALID_ARGUMENT", "target_id must be a page id from mobile_web_pages")
        with Operation() as op:
            backend, found = self.mobile._target(serial)
            op.check()
            names = _sockets(backend, found)
            if name is None:
                if len(names) != 1:
                    raise WebCoderError(
                        "MOBILE_CDP_AMBIGUOUS" if names else "MOBILE_CDP_UNAVAILABLE",
                        "Pass a DevTools socket from mobile_web_pages"
                        if names
                        else "No DevTools sockets: enable WebView/ArkWeb web debugging and open the page",
                    )
                name = names[0]
            elif name not in names:
                raise WebCoderError("MOBILE_CDP_UNAVAILABLE", "That DevTools socket is no longer on this phone")
            with forward(backend, found, name, op) as port:
                rows = targets(port)
                chosen = [row for row in rows if target_id is None or row["id"] == target_id]
                if len(chosen) != 1:
                    raise WebCoderError(
                        "MOBILE_CDP_AMBIGUOUS" if len(chosen) > 1 else "MOBILE_CDP_TARGET_GONE",
                        "Pass target_id from mobile_web_pages" if chosen else "The page is no longer open",
                    )
                try:
                    with CDP(port, chosen[0], op) as cdp:
                        yield cdp, {"serial": found, "platform": backend.platform, **page_view(name, chosen[0])}
                except (OSError, TimeoutError, WebSocketException):
                    op.check()
                    raise WebCoderError(
                        "MOBILE_CDP_DISCONNECTED",
                        "The page's DevTools connection closed or timed out; list pages again if the app restarted",
                    ) from None

    def dom(self, serial=None, socket=None, target_id=None, max_chars=50000):
        from webcoder.agent.mobile import _int

        limit = _int(max_chars, "max_chars", 1000, 200000)
        with self.page(serial, socket, target_id) as (cdp, facts):
            try:
                root = cdp.call("DOM.getDocument", {"depth": 0, "pierce": True})["root"]["nodeId"]
                html = cdp.call("DOM.getOuterHTML", {"nodeId": root})["outerHTML"]
                if not isinstance(html, str):
                    raise TypeError()
            except (KeyError, TypeError, AttributeError):
                raise WebCoderError("MOBILE_CDP_PROTOCOL_ERROR", "The page returned a malformed DOM") from None
            return {**facts, "html": html[:limit], "truncated": len(html) > limit}

    def evaluate(self, expression, serial=None, socket=None, target_id=None, timeout=15):
        from webcoder.agent.mobile import _int

        seconds = _int(timeout, "timeout", 1, 60)
        if not isinstance(expression, str) or not expression.strip() or len(expression) > 20000:
            raise WebCoderError("INVALID_ARGUMENT", "expression must be 1-20000 characters of JavaScript")
        with self.page(serial, socket, target_id) as (cdp, facts):
            result = cdp.call(
                "Runtime.evaluate",
                {"expression": expression, "returnByValue": True, "awaitPromise": True, "timeout": seconds * 1000},
                seconds + 2,
            )
            return {**facts, **result}

    def command(self, method, params=None, serial=None, socket=None, target_id=None):
        if method in CODE_METHODS:
            raise WebCoderError("INVALID_ARGUMENT", f"{method} runs code on the page: use mobile_web_evaluate instead")
        if not isinstance(method, str) or not METHOD_RE.fullmatch(method):
            raise WebCoderError(
                "INVALID_ARGUMENT",
                "method must be a page CDP command (Page, Runtime, DOM, CSS, "
                "Network, Log, Input, Accessibility, Emulation, Debugger or Profiler)",
            )
        try:
            if params is not None and not isinstance(params, dict):
                raise ValueError()
            if len(json.dumps(params or {}, allow_nan=False)) > 50000:
                raise ValueError()
        except (ValueError, TypeError):
            raise WebCoderError(
                "INVALID_ARGUMENT", "params must be a finite JSON object under 50000 characters"
            ) from None
        with self.page(serial, socket, target_id) as (cdp, facts):
            return {**facts, "method": method, "result": cdp.call(method, params)}

    def events(self, serial=None, socket=None, target_id=None, seconds=5, reload=False):
        from webcoder.agent.mobile import _int

        duration = _int(seconds, "seconds", 1, 30)
        if not isinstance(reload, bool):
            raise WebCoderError("INVALID_ARGUMENT", "reload must be a boolean")
        if reload and self.mobile.files.sb.read_only:
            raise WebCoderError("AGENT_READ_ONLY", "A read-only device cannot reload a phone page")
        with self.page(serial, socket, target_id) as (cdp, facts):
            cdp.call("Runtime.enable")
            cdp.call("Log.enable")
            cdp.call("Network.enable")
            cdp.events.clear()  # domain enablement can replay cached console/log events
            cdp.event_bytes, cdp.dropped = 0, False
            if reload:
                cdp.call("Page.reload", {"ignoreCache": False})
            cdp.collect(duration)
            return {**facts, "events": cdp.events, "truncated": cdp.dropped, "seconds": duration}

    def screenshot(self, root, path, serial=None, socket=None, target_id=None):
        if not str(path).lower().endswith(".png"):
            raise WebCoderError("INVALID_ARGUMENT", "CDP page screenshots must end in .png")
        with self.page(serial, socket, target_id) as (cdp, facts):
            result = cdp.call("Page.captureScreenshot", {"format": "png"})
            try:
                data = base64.b64decode(result["data"], validate=True)
                if not data.startswith(b"\x89PNG\r\n\x1a\n"):
                    raise ValueError()
            except (KeyError, ValueError, TypeError):  # data missing, not text, or not base64 / PNG
                raise WebCoderError("MOBILE_CDP_PROTOCOL_ERROR", "The page did not return a valid PNG") from None
            cdp.op.check()
            return {**facts, **self.mobile.files.create_file(root, path, data, max_bytes=MAX_JSON)}
