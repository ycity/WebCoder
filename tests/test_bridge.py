"""The stdio bridge relays JSON-RPC lines to /mcp (the server behind Secure MCP Tunnel)."""
import io
import json
import os
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

from tests.conftest import ROOT
from webcoder.bridge import __main__ as bridge

INITIALIZE = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                         "clientInfo": {"name": "t", "version": "1"}}}


def run_bridge(url: str, messages: list[dict], *, token: str | None = None, extra_args=()) -> list[dict]:
    env = {**os.environ, "PYTHONPATH": str(ROOT), "PYTHONIOENCODING": "utf-8"}
    env.pop("WEBCODER_TOKEN", None)
    if token is not None:
        env["WEBCODER_TOKEN"] = token
    stdin = "".join(json.dumps(message) + "\n" for message in messages)
    done = subprocess.run([sys.executable, "-m", "webcoder.bridge", "--url", url, "--no-proxy", *extra_args],
                          input=stdin.encode(), capture_output=True, env=env, timeout=60)
    return [json.loads(line) for line in done.stdout.decode("utf-8").splitlines() if line.strip()]


def test_bridge_relays_requests_and_skips_notifications(hub, start_agent, tmp_path):
    device_id, _ = start_agent("laptop", tmp_path / "work")
    (tmp_path / "work" / "hello.txt").write_text("hi from the device\n", encoding="utf-8")
    hub.add_project("app", device_id, tmp_path / "work")
    replies = run_bridge(hub.url + "/mcp", [
        INITIALIZE,
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "read_file", "arguments": {"project": "app", "path": "hello.txt"}}},
    ], token=hub.create_token())
    by_id = {reply["id"]: reply for reply in replies}
    assert set(by_id) == {1, 2}
    assert by_id[1]["result"]["protocolVersion"] == "2025-06-18"
    assert "hi from the device" in by_id[2]["result"]["content"][0]["text"]


def test_bridge_reads_token_file_as_the_tunnel_does(hub, tmp_path):
    token_file = tmp_path / "token.txt"
    token_file.write_text(hub.create_token() + "\n", encoding="utf-8")
    replies = run_bridge(hub.url + "/mcp", [INITIALIZE, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}],
                         extra_args=["--token-file", token_file.as_posix()])
    tools = next(reply for reply in replies if reply["id"] == 2)["result"]["tools"]
    assert len(tools) == 35 and tools[0]["securitySchemes"] == [{"type": "noauth"}]


def test_bridge_reports_bad_token_on_the_request_id(hub):
    replies = run_bridge(hub.url + "/mcp", [{"jsonrpc": "2.0", "id": 9, "method": "tools/list"}],
                         token="wc_pat_wrong")
    assert replies[0]["id"] == 9 and "WEBCODER_TOKEN" in replies[0]["error"]["message"]


def test_bridge_forwards_a_bounded_number_of_requests_at_once(monkeypatch):
    """A client that floods stdin waits for free slots instead of getting a thread per request."""
    lock, seen = threading.Lock(), {"now": 0, "peak": 0, "done": 0}

    def slow_forward(opener, url, token, message):
        with lock:
            seen["now"] += 1
            seen["peak"] = max(seen["peak"], seen["now"])
        time.sleep(0.05)
        with lock:
            seen["now"] -= 1
            seen["done"] += 1

    monkeypatch.setattr(bridge, "MAX_IN_FLIGHT", 3)
    monkeypatch.setattr(bridge, "_forward", slow_forward)
    lines = "".join(json.dumps({"jsonrpc": "2.0", "id": n, "method": "ping"}) + "\n" for n in range(20))
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(lines.encode())))
    monkeypatch.setenv("WEBCODER_TOKEN", "wc_pat_x")
    assert bridge.main(["--url", "http://127.0.0.1:9/mcp", "--no-proxy"]) == 0
    assert seen == {"now": 0, "peak": 3, "done": 20}


def test_bridge_never_follows_a_redirect_with_the_token(tmp_path):
    """A redirect (from a proxy in front of the hub, say) would carry the Authorization header along."""
    import http.server

    seen = []

    class Elsewhere(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # urllib turns a redirected POST into a GET
            seen.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.end_headers()

        do_POST = do_GET

        def log_message(self, *args):
            pass

    class Redirector(Elsewhere):
        def do_POST(self):
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{target.server_address[1]}/steal")
            self.send_header("Content-Length", "0")
            self.end_headers()

    target = http.server.HTTPServer(("127.0.0.1", 0), Elsewhere)
    hub = http.server.HTTPServer(("127.0.0.1", 0), Redirector)
    for server in (target, hub):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        replies = run_bridge(f"http://127.0.0.1:{hub.server_address[1]}/mcp",
                             [{"jsonrpc": "2.0", "id": 4, "method": "tools/list"}], token="wc_pat_secret")
    finally:
        for server in (target, hub):
            server.shutdown()
    assert replies[0]["id"] == 4 and "redirect" in replies[0]["error"]["message"] and seen == []
