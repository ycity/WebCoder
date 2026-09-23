"""MCP 2026-07-28 ("modern") request rules: per-request metadata mirrored in HTTP headers.

Only used while MCP Events are switched on (WEBCODER_MCP_EVENTS_ENABLED): event subscriptions need the modern
protocol. Otherwise modern-shaped requests are refused so dual-era clients fall back to initialize.
Ported from CodePier (shared/mcp_protocol.py, MIT License, Copyright (c) 2026 Remote Dev MCP Agent contributors).
"""
from __future__ import annotations

import base64
import binascii
import re

MODERN = '2026-07-28'
LEGACY = frozenset({'2025-03-26', '2025-06-18', '2025-11-25'})
PREFIX = 'io.modelcontextprotocol/'
NAMED = {'tools/call': 'name', 'prompts/get': 'name', 'events/subscribe': 'name', 'events/unsubscribe': 'name',
         'resources/read': 'uri'}
LOG_LEVELS = frozenset({'debug', 'info', 'notice', 'warning', 'error', 'critical', 'alert', 'emergency'})


class ProtocolError(Exception):
    def __init__(self, code: int, message: str, data=None):
        super().__init__(message)
        self.code, self.message, self.data = code, message, data


def decode_header(value):
    if (not isinstance(value, str) or len(value) > 16384 or value != value.strip()
            or any(c != '\t' and not 32 <= ord(c) <= 126 for c in value)):
        raise ProtocolError(-32020, 'Invalid request metadata header')
    if value.startswith('=?base64?') and value.endswith('?='):
        try:
            return base64.b64decode(value[9:-2], validate=True).decode('utf-8')
        except (ValueError, UnicodeError, binascii.Error) as exc:
            raise ProtocolError(-32020, 'Malformed encoded request header') from exc
    return value


def is_modern(body, headers) -> bool:
    params = body.get('params') if isinstance(body, dict) else None
    meta = params.get('_meta') if isinstance(params, dict) else None
    if isinstance(meta, dict) and PREFIX + 'protocolVersion' in meta:
        return True
    version = headers.get('mcp-protocol-version')
    return (version is not None and version not in LEGACY) or (
        isinstance(body, dict) and body.get('method') == 'server/discover')


def validate_modern(body: dict, headers) -> dict:
    """Check one modern request's metadata and its HTTP mirror headers; returns the metadata."""
    for header in ('mcp-protocol-version', 'mcp-method', 'mcp-name'):
        if len(headers.getlist(header)) > 1:
            raise ProtocolError(-32020, 'Duplicate request metadata headers')
    params = body.get('params', {})
    meta = params.get('_meta') if isinstance(params, dict) else None
    if not isinstance(meta, dict):
        raise ProtocolError(-32602, 'Per-request _meta is required')
    version, caps = meta.get(PREFIX + 'protocolVersion'), meta.get(PREFIX + 'clientCapabilities')
    if not isinstance(version, str) or not version or not isinstance(caps, dict):
        raise ProtocolError(-32602, 'protocolVersion and clientCapabilities are required on every request')
    if version != MODERN:
        raise ProtocolError(-32022, 'Unsupported protocol version for per-request metadata',
                            {'supported': [MODERN, *sorted(LEGACY, reverse=True)], 'requested': version})
    info = meta.get(PREFIX + 'clientInfo')
    if info is not None and (not isinstance(info, dict) or not isinstance(info.get('name'), str)
                             or not isinstance(info.get('version'), str)):
        raise ProtocolError(-32602, 'Invalid clientInfo')
    level = meta.get(PREFIX + 'logLevel')
    if level is not None and level not in LOG_LEVELS:
        raise ProtocolError(-32602, 'Invalid logLevel')
    extensions = caps.get('extensions', {})
    if not isinstance(extensions, dict):
        raise ProtocolError(-32602, 'Invalid extension capabilities')
    for name, value in extensions.items():
        if not isinstance(value, dict) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9.-]*/[A-Za-z0-9_.-]+', name):
            raise ProtocolError(-32602, 'Invalid extension declaration')
    if headers.get('mcp-protocol-version') != version or headers.get('mcp-method') != body['method']:
        raise ProtocolError(-32020, 'MCP-Protocol-Version and Mcp-Method must match the request body')
    field = NAMED.get(body['method'])
    if field:
        value = params.get(field)
        if not isinstance(value, str):
            raise ProtocolError(-32602, 'Required request name/uri must be a string')
        # Events do not require Mcp-Name in the HTTP transport; a supplied mirror must still agree.
        optional = body['method'] in {'events/subscribe', 'events/unsubscribe'}
        mirrored = headers.get('mcp-name')
        if (not optional or mirrored is not None) and decode_header(mirrored) != value:
            raise ProtocolError(-32020, 'Mcp-Name must match the request body')
    if 'requestState' in params or 'inputResponses' in params:
        raise ProtocolError(-32602, 'This server does not issue input requests')
    return meta


def complete(result: dict, server_info: dict) -> dict:
    meta = {**result.get('_meta', {}), PREFIX + 'serverInfo': server_info}
    return {**result, 'resultType': 'complete', '_meta': meta}
