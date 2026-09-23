"""Error type shared by the hub and the agent."""
from __future__ import annotations


class WebCoderError(Exception):
    """An error with a stable machine-readable code.

    Code and message reach the MCP client (and therefore the model), so the
    message should say what to do next.
    """

    def __init__(self, code: str, message: str, **details):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, **self.details}
