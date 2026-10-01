"""Relay newline-delimited JSON-RPC between stdin/stdout and the hub's /mcp endpoint.

Used where only a local stdio MCP server can be started, most importantly as the
server behind OpenAI's Secure MCP Tunnel (tunnel-client stdio profile), which lets
ChatGPT reach a hub that is not publicly reachable. Standard library only. The
token comes from an environment variable or a file, never from the command line.

    webcoder-bridge --url http://127.0.0.1:8787/mcp      # token from WEBCODER_TOKEN, else ~/.webcoder/token.txt
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_TOKEN_FILE = "~/.webcoder/token.txt"
_write_lock = threading.Lock()
_state = {"protocol_version": None}
MAX_IN_FLIGHT = 16  # requests forwarded at once (each can wait minutes for a slow tool)


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """A redirect would carry the Authorization header (the token) to wherever it points: none is followed."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # urllib then raises HTTPError with the 3xx status, reported like any other


def _emit(message) -> None:
    data = (json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    with _write_lock:
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()


def _log(text: str) -> None:
    sys.stderr.write(f"[webcoder-bridge] {text}\n")
    sys.stderr.flush()


def _error(request_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _parse_sse(text: str) -> list:
    messages, data_lines = [], []
    for line in text.splitlines() + [""]:
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
        elif not line and data_lines:
            try:
                messages.append(json.loads("\n".join(data_lines)))
            except ValueError:
                pass
            data_lines = []
    return messages


def _forward(opener, url: str, token: str, message: dict) -> None:
    request_id = message.get("id") if isinstance(message, dict) else None
    is_request = isinstance(message, dict) and "method" in message and "id" in message
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
               "Authorization": f"Bearer {token}", "User-Agent": "webcoder-bridge"}
    if _state["protocol_version"] and not (isinstance(message, dict) and message.get("method") == "initialize"):
        headers["MCP-Protocol-Version"] = _state["protocol_version"]
    request = urllib.request.Request(url, data=json.dumps(message).encode("utf-8"), headers=headers, method="POST")
    try:
        with opener.open(request, timeout=720) as response:  # the hub waits up to 660 s (SSH transfers)
            status, content_type, body = response.status, response.headers.get("Content-Type", ""), response.read()
    except urllib.error.HTTPError as exc:
        status, content_type, body = exc.code, exc.headers.get("Content-Type", ""), exc.read()
        if 300 <= status < 400:  # not followed (see _NoRedirects)
            if is_request:
                _emit(_error(request_id, -32000, f"The hub answered with a redirect (HTTP {status} to "
                                                 f"{exc.headers.get('Location', '?')}), which is not followed: the "
                                                 "token would go along. Check --url"))
            return
    except (urllib.error.URLError, OSError) as exc:
        if is_request:
            _emit(_error(request_id, -32000, f"Cannot reach the WebCoder hub: {getattr(exc, 'reason', exc)}"))
        return
    if status == 202 or not body.strip():
        if is_request and status >= 400:
            _emit(_error(request_id, -32000, f"Hub returned HTTP {status}"))
        return
    text = body.decode("utf-8", errors="replace")
    replies = _parse_sse(text) if "text/event-stream" in content_type else None
    if replies is None:
        try:
            parsed = json.loads(text)
            replies = parsed if isinstance(parsed, list) else [parsed]
        except ValueError:
            replies = []
    if not replies:
        if is_request:
            _emit(_error(request_id, -32000, f"Hub returned HTTP {status} without a JSON-RPC reply"))
        return
    for reply in replies:
        if isinstance(reply, dict) and reply.get("id") is None and is_request and "error" in reply:
            reply = {**reply, "id": request_id}  # auth errors (401/403) come back without the request id
            if status == 401:
                reply["error"]["message"] += " (check the WEBCODER_TOKEN given to the bridge)"
        result = reply.get("result") if isinstance(reply, dict) else None
        if isinstance(message, dict) and message.get("method") == "initialize" and isinstance(result, dict):
            _state["protocol_version"] = result.get("protocolVersion")
        _emit(reply)


def _is_loopback(url: str) -> bool:
    try:
        host = urllib.parse.urlsplit(url).hostname or ""
    except ValueError:
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="webcoder-bridge", description=__doc__.splitlines()[0])
    parser.add_argument("--url", default=os.environ.get("WEBCODER_URL"), help="hub MCP URL, e.g. https://hub/mcp")
    parser.add_argument("--token-env", default="WEBCODER_TOKEN", help="environment variable holding the token")
    parser.add_argument("--token-file", type=Path, help="file holding the token (alternative to --token-env; "
                                                        f"default when neither is set: {DEFAULT_TOKEN_FILE})")
    parser.add_argument("--no-proxy", action="store_true", help="ignore HTTP(S)_PROXY settings")
    args = parser.parse_args(argv)
    if not args.url:
        parser.error("--url (or WEBCODER_URL) is required")
    token_file = args.token_file.expanduser() if args.token_file else None
    token = os.environ.get(args.token_env, "").strip() if token_file is None else ""
    if token_file is None and not token:
        token_file = Path(DEFAULT_TOKEN_FILE).expanduser()  # where the panel tells people to save the token
        if not token_file.is_file():
            parser.error(f"no token: set {args.token_env}, pass --token-file, or save it to {DEFAULT_TOKEN_FILE}")
    if token_file is not None:
        try:
            token = token_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            parser.error(f"cannot read the token file {token_file}: {exc.strerror or exc}")
    if not token:
        parser.error(f"no token: set {args.token_env} or pass --token-file")
    # A hub on this computer is never reached through a proxy: tunnel-client often runs with HTTP(S)_PROXY set
    # (to reach OpenAI), which the bridge inherits; through it the request would fail and the token leave.
    direct = args.no_proxy or _is_loopback(args.url)
    opener = urllib.request.build_opener(_NoRedirects, *([urllib.request.ProxyHandler({})] if direct else []))
    _log(f"relaying stdio to {args.url}")
    workers: list[threading.Thread] = []
    slots = threading.BoundedSemaphore(MAX_IN_FLIGHT)  # full: stop reading stdin until a request finishes

    def forward(message: dict) -> None:
        try:
            _forward(opener, args.url, token, message)
        finally:
            slots.release()

    for raw in sys.stdin.buffer:
        line = raw.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            _emit(_error(None, -32700, "Parse error"))
            continue
        # Each request runs in its own thread so a slow tool call does not block pings or other calls.
        slots.acquire()
        worker = threading.Thread(target=forward, args=(message,), daemon=True)
        worker.start()
        workers = [w for w in workers if w.is_alive()] + [worker]
    deadline = time.monotonic() + 750  # stdin closed: let in-flight requests finish (each waits up to 720 s)
    for worker in workers:
        worker.join(timeout=max(0.0, deadline - time.monotonic()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
