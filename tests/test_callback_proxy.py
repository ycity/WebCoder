"""MCP Events callbacks through WEBCODER_CALLBACK_PROXY (a SOCKS5 proxy), e.g. from a region the host refuses."""
from __future__ import annotations

import asyncio
import socket

import httpx
import pytest

from webcoder.hub.collaboration import network
from webcoder.hub.config import Settings
from webcoder.hub.runtime import Runtime


def test_the_callback_proxy_must_be_socks5(tmp_path, monkeypatch):
    monkeypatch.setenv("WEBCODER_CALLBACK_PROXY", " socks5://127.0.0.1:1080 ")
    assert Settings.from_env(data_dir=tmp_path).callback_proxy == "socks5://127.0.0.1:1080"
    monkeypatch.setenv("WEBCODER_CALLBACK_PROXY", "")
    assert Settings.from_env(data_dir=tmp_path).callback_proxy is None
    for value in ("http://127.0.0.1:3128", "socks5://127.0.0.1", "socks5://:1080", "socks5://127.0.0.1:1080/x",
                  "socks5://127.0.0.1:99999", "127.0.0.1:1080"):
        with pytest.raises(ValueError, match="WEBCODER_CALLBACK_PROXY"):
            Settings(data_dir=tmp_path, callback_proxy=value)


def test_the_hub_sends_callbacks_through_the_configured_proxy(tmp_path):
    proxied = Runtime(Settings(data_dir=tmp_path / "a", collaboration_enabled=True, mcp_events_enabled=True,
                               callback_proxy="socks5://127.0.0.1:1080"))
    direct = Runtime(Settings(data_dir=tmp_path / "b", collaboration_enabled=True, mcp_events_enabled=True))
    try:
        sender = proxied.collaboration.events.sender
        assert sender.func is network.webhook and sender.keywords == {"proxy": "socks5://127.0.0.1:1080"}
        assert direct.collaboration.events.sender is network.webhook
    finally:
        proxied.close()
        direct.close()


@pytest.mark.anyio
async def test_the_proxy_is_given_the_checked_address_not_the_name():
    seen = []

    async def socks5(reader, writer):
        greeting = await reader.readexactly(2)
        await reader.readexactly(greeting[1])
        writer.write(b"\x05\x00")  # no authentication
        await writer.drain()
        command = await reader.readexactly(4)
        if command[3] == 1:
            address = socket.inet_ntoa(await reader.readexactly(4))
        else:
            address = (await reader.readexactly((await reader.readexactly(1))[0])).decode()
        seen.append((command[1], command[3], address, int.from_bytes(await reader.readexactly(2), "big")))
        writer.write(b"\x05\x02\x00\x01" + bytes(6))  # connection not allowed by ruleset
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(socks5, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    destination = (httpx.URL("https://203.0.113.7/webhook/x"), "hooks.example.com", "hooks.example.com")
    try:
        with pytest.raises(httpx.ProxyError):
            await network.request("POST", destination, {}, b"{}", proxy=f"socks5://127.0.0.1:{port}")
    finally:
        server.close()
        await server.wait_closed()
    assert seen == [(1, 1, "203.0.113.7", 443)]  # CONNECT to the IPv4 address, port 443
