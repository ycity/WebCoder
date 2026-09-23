"""Compatibility check with the official MCP Python SDK as an independent client.

The SDK's default ``mode="auto"`` behaves like a modern dual-era client: it
probes ``server/discover`` first and must fall back to ``initialize`` here.
"""
import asyncio

import pytest

mcp_client = pytest.importorskip("mcp.client.client")
streamable = pytest.importorskip("mcp.client.streamable_http")
httpx2 = pytest.importorskip("httpx2")


async def _session(url: str, token: str, mode: str, work):
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx2.AsyncClient(headers=headers, trust_env=False, timeout=60) as http:
        async with mcp_client.Client(streamable.streamable_http_client(url, http_client=http), mode=mode) as client:
            return await work(client)


@pytest.mark.parametrize("mode", ["auto", "legacy"])
def test_official_sdk_client(hub, start_agent, tmp_path, mode):
    device_id, _ = start_agent("laptop", tmp_path / "work")
    (tmp_path / "work" / "hello.txt").write_text("hello from the device\n", encoding="utf-8")
    hub.add_project("app", device_id, tmp_path / "work")
    token = hub.create_token()

    async def work(client):
        tools = await client.list_tools()
        listed = await client.call_tool("list_projects", {})
        read = await client.call_tool("read_file", {"project": "app", "path": "hello.txt"})
        return tools, listed, read

    tools, listed, read = asyncio.run(_session(hub.url + "/mcp", token, mode, work))
    assert len(tools.tools) == 35
    assert "app [read-write]" in listed.content[0].text
    assert "hello from the device" in read.content[0].text and not read.is_error
