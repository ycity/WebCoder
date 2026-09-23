"""Errors and small trust-boundary helpers shared by the collaboration modules."""
from __future__ import annotations

import logging
import math

from webcoder.common.errors import WebCoderError

log = logging.getLogger("webcoder.hub.collaboration")

# Why a callback challenge failed: fixed categories only, never URLs, bodies or exception text.
CALLBACK_REASONS = frozenset({'connection_refused', 'timeout', 'tls_error', 'http_4xx', 'http_5xx',
                              'challenge_failed'})


class DevError(WebCoderError):
    """A collaboration error: stable code, message, and the HTTP status the panel answers with.

    Being a WebCoderError, a tool call reports it like any other tool error."""

    def __init__(self, code: str, message: str, status: int = 400, **details):
        super().__init__(code, message, **details)
        self.status = status


def mark(stage, **fields):
    """Diagnostics for MCP Events stages (subscription challenge failures, catalog size). Only scalar fields
    with fixed meanings are passed in; nothing here may carry callback URLs, bodies or secrets."""
    safe = {key: value for key, value in fields.items() if value is None or type(value) in (str, int, bool)}
    log.info("mcp_events %s %s", stage, safe)


def valid_json_value(value) -> bool:
    """Validate JSON at trust boundaries before UTF-8, SQLite or URL handling.

    Python's JSON decoder accepts lone surrogates and non-finite floats. Neither
    can safely round-trip through our database and HTTP responses. Iterate so
    hostile nesting cannot overflow the validator's own Python call stack.
    """
    # Keep only one iterator per nesting level; a wide request must not
    # allocate millions of additional (child, depth) tuples during validation.
    pending = [(iter((value,)), 0)]
    while pending:
        iterator, depth = pending[-1]
        try:
            item = next(iterator)
        except StopIteration:
            pending.pop()
            continue
        if depth > 100:
            return False
        if item is None or type(item) in (bool, int):
            continue
        if isinstance(item, str):
            try:
                item.encode("utf-8")
            except UnicodeError:
                return False
        elif type(item) is float:
            if not math.isfinite(item):
                return False
        elif isinstance(item, list):
            pending.append((iter(item), depth + 1))
        elif isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                return False
            pending.append(((child for pair in item.items() for child in pair), depth + 1))
        else:
            return False
    return True
