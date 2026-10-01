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


def run_bridge(url: str, messages: list[dict], *, token: str | None = None, extra_args=(), env_extra=None,
               no_proxy: bool = True, result: dict | None = None) -> list[dict]:
    env = {**os.environ, "PYTHONPATH": str(ROOT), "PYTHONIOENCODING": "utf-8"}
    env.pop("WEBCODER_TOKEN", None)
    if token is not None:
        env["WEBCODER_TOKEN"] = token
    env.update(env_extra or {})
    stdin = "".join(json.dumps(message) + "\n" for message in messages)
    done = subprocess.run([sys.executable, "-m", "webcoder.bridge", "--url", url,
                           *(["--no-proxy"] if no_proxy else []), *extra_args],
                          input=stdin.encode(), capture_output=True, env=env, timeout=60)
    if result is not None:
        result.update(code=done.returncode, stderr=done.stderr.decode("utf-8", errors="replace"))
    return [json.loads(line) for line in done.stdout.decode("utf-8").splitlines() if line.strip()]


def home_env(home) -> dict:
    """Where ~ points for the bridge, on Windows and elsewhere."""
    return {"USERPROFILE": str(home), "HOME": str(home), "HOMEDRIVE": "", "HOMEPATH": ""}


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


def test_bridge_finds_the_token_under_a_home_folder_with_a_space(hub, tmp_path):
    """The panel saves the token to ~/.webcoder/token.txt and the tunnel command names it with ~, so no user name
    or quoting (a home folder with a space) has to be right in the tunnel profile."""
    home = tmp_path / "John Smith"
    (home / ".webcoder").mkdir(parents=True)
    (home / ".webcoder" / "token.txt").write_text(hub.create_token() + "\n", encoding="utf-8")
    replies = run_bridge(hub.url + "/mcp", [INITIALIZE, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}],
                         env_extra=home_env(home))
    assert len(next(reply for reply in replies if reply["id"] == 2)["result"]["tools"]) == 35
    # --token-file accepts ~ too (tunnel-client starts the command without a shell to expand it), and a named file
    # wins over a WEBCODER_TOKEN set for Codex, which tunnel-client would pass on
    replies = run_bridge(hub.url + "/mcp", [INITIALIZE], token="wc_pat_set_for_codex", env_extra=home_env(home),
                         extra_args=["--token-file", "~/.webcoder/token.txt"])
    assert replies[0]["result"]["serverInfo"]["name"] == "webcoder"


def test_bridge_explains_a_missing_token(tmp_path):
    seen = {}
    assert run_bridge("http://127.0.0.1:9/mcp", [INITIALIZE], env_extra=home_env(tmp_path), result=seen) == []
    assert seen["code"] == 2 and "~/.webcoder/token.txt" in seen["stderr"]
    seen = {}
    run_bridge("http://127.0.0.1:9/mcp", [INITIALIZE], extra_args=["--token-file", str(tmp_path / "nope.txt")],
               result=seen)
    assert seen["code"] == 2 and "cannot read the token file" in seen["stderr"] and "Traceback" not in seen["stderr"]


def test_bridge_never_sends_a_local_hub_request_or_its_token_to_a_proxy(hub, tmp_path):
    """tunnel-client usually runs with HTTP(S)_PROXY set (to reach OpenAI); the bridge inherits them."""
    import http.server

    proxied = []

    class Proxy(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            proxied.append(self.headers.get("Authorization"))
            self.send_response(502)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    proxy = http.server.HTTPServer(("127.0.0.1", 0), Proxy)
    threading.Thread(target=proxy.serve_forever, daemon=True).start()
    address = f"http://127.0.0.1:{proxy.server_address[1]}"
    try:
        local = hub.url.replace("127.0.0.1", "localhost") + "/mcp"
        for url in (hub.url + "/mcp", local):
            replies = run_bridge(url, [INITIALIZE], token=hub.create_token(), no_proxy=False,
                                 env_extra={"HTTP_PROXY": address, "HTTPS_PROXY": address, "NO_PROXY": "",
                                            "http_proxy": address, "https_proxy": address, "no_proxy": ""})
            assert replies[0]["result"]["serverInfo"]["name"] == "webcoder", url
        assert proxied == []
    finally:
        proxy.shutdown()
    assert bridge._is_loopback("http://[::1]:8787/mcp") and not bridge._is_loopback("https://hub.example.com/mcp")
