"""The MCP clients this hub supports (ChatGPT web and Codex): OAuth redirect rules
and ready-to-paste setup snippets. Adding a client later means adding an entry
to SUPPORTED_CLIENTS and its snippets below."""
from __future__ import annotations

import re
from urllib.parse import urlsplit

# ChatGPT (web, developer-mode apps) finishes OAuth on chatgpt.com. It uses the stable
# callback when the authorization server supports RFC 9207 `iss` (this hub does), and
# otherwise a per-connection callback under /connector/oauth/<callback_id>.
CHATGPT_STABLE_CALLBACK = "https://chatgpt.com/connector_platform_oauth_redirect"
CHATGPT_CALLBACK_PREFIX = "https://chatgpt.com/connector/oauth/"
_CALLBACK_ID = re.compile(r"[A-Za-z0-9_-]{1,200}")
# Codex is a native app: RFC 8252 loopback redirect on a random port, e.g.
# http://127.0.0.1:<port>/callback.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

SUPPORTED_CLIENTS = {
    "chatgpt": {"title": "ChatGPT", "surfaces": "ChatGPT 网页版（开发者模式应用）",
                "hosted_callbacks": (CHATGPT_STABLE_CALLBACK,), "hosted_prefixes": (CHATGPT_CALLBACK_PREFIX,),
                "loopback": False},
    "codex": {"title": "Codex", "surfaces": "Codex CLI / IDE 插件 / Codex App",
              "hosted_callbacks": (), "hosted_prefixes": (), "loopback": True},
}


def is_chatgpt_callback(uri: str) -> bool:
    return uri == CHATGPT_STABLE_CALLBACK or (
        uri.startswith(CHATGPT_CALLBACK_PREFIX) and bool(_CALLBACK_ID.fullmatch(uri[len(CHATGPT_CALLBACK_PREFIX):])))


class RedirectPolicy:
    """Decides which OAuth redirect URIs may be registered and used."""

    def __init__(self, clients, extra_uris=()):
        unknown = [name for name in clients if name not in SUPPORTED_CLIENTS]
        if unknown:
            raise ValueError(f"Unsupported client(s): {', '.join(unknown)}")
        self.clients = tuple(clients)
        self.exact = {uri for name in self.clients for uri in SUPPORTED_CLIENTS[name]["hosted_callbacks"]}
        self.prefixes = tuple(prefix for name in self.clients for prefix in SUPPORTED_CLIENTS[name]["hosted_prefixes"])
        self.extra = set(extra_uris)
        self.loopback = any(SUPPORTED_CLIENTS[name]["loopback"] for name in self.clients)

    def check(self, uri) -> str:
        """Return 'hosted', 'loopback' or 'extra' for an acceptable redirect URI, else raise ValueError."""
        if not isinstance(uri, str) or not uri or len(uri) > 2000 or any(ch.isspace() for ch in uri) or "#" in uri:
            raise ValueError("redirect URI is malformed")
        if uri in self.exact:
            return "hosted"
        for prefix in self.prefixes:
            if uri.startswith(prefix) and _CALLBACK_ID.fullmatch(uri[len(prefix):]):
                return "hosted"
        if uri in self.extra:
            return "extra"
        parts = urlsplit(uri)
        if (self.loopback and parts.scheme == "http" and parts.hostname in LOOPBACK_HOSTS
                and not parts.username and not parts.password):
            return "loopback"
        raise ValueError("redirect URI is not allowed for the supported clients (ChatGPT, Codex)")

    @staticmethod
    def matches(registered: str, presented: str) -> bool:
        """Exact match, or loopback match ignoring the port (RFC 8252 section 7.3)."""
        if registered == presented:
            return True
        a, b = urlsplit(registered), urlsplit(presented)
        return (a.scheme == b.scheme == "http" and a.hostname in LOOPBACK_HOSTS and a.hostname == b.hostname
                and a.path == b.path and a.query == b.query)


def describe_client(client_name: str, redirect_uri: str) -> dict:
    """Best-effort label for the consent page. Client names are self-asserted."""
    if is_chatgpt_callback(redirect_uri):
        return {"client": "chatgpt", "surface": "ChatGPT 网页版", "loopback": False}
    loopback = urlsplit(redirect_uri).hostname in LOOPBACK_HOSTS
    if "codex" in (client_name or "").lower():
        return {"client": "codex", "surface": "Codex", "loopback": loopback}
    return {"client": "unknown", "surface": "本机应用" if loopback else "其他应用", "loopback": loopback}


def setup_snippets(public_url: str, token: str | None = None) -> dict:
    """Copy-paste instructions for each supported client."""
    mcp = public_url.rstrip("/") + "/mcp"
    tok = token or "<你的访问令牌>"
    # The file is named explicitly: a WEBCODER_TOKEN set for Codex would otherwise win. The bridge expands ~ itself,
    # so the command has no user name or space to get right.
    bridge = f"webcoder-bridge --url {mcp} --token-file ~/.webcoder/token.txt"
    return {
        "mcp_url": mcp,
        # ChatGPT, route 1: the hub itself is on public HTTPS; ChatGPT signs in with OAuth.
        "chatgpt_url": mcp,
        # ChatGPT, route 2: OpenAI Secure MCP Tunnel runs the stdio bridge next to the hub.
        "tunnel_token_file_windows": ('New-Item -ItemType Directory -Force "$env:USERPROFILE\\.webcoder" | Out-Null; '
                                      f'Set-Content -NoNewline "$env:USERPROFILE\\.webcoder\\token.txt" "{tok}"'),
        "tunnel_token_file_unix": f"mkdir -p ~/.webcoder && printf '%s' '{tok}' > ~/.webcoder/token.txt && "
                                  "chmod 600 ~/.webcoder/token.txt",
        "tunnel_key_env_windows": '$env:CONTROL_PLANE_API_KEY = "<Platform 里创建的 Runtime API key>"',
        "tunnel_init": ("tunnel-client init --sample sample_mcp_stdio_local --profile webcoder "
                        f'--tunnel-id <你的 tunnel_id> --mcp-command "{bridge}"'),
        "tunnel_check": "tunnel-client doctor --profile webcoder --explain",
        "tunnel_run": "tunnel-client run --profile webcoder",
        "bridge_command": bridge,
        # Codex
        "codex_token_cli": f"codex mcp add webcoder --url {mcp} --bearer-token-env-var WEBCODER_TOKEN",
        "codex_token_env_windows": f'[Environment]::SetEnvironmentVariable("WEBCODER_TOKEN", "{tok}", "User")',
        "codex_token_env_unix": f"echo 'export WEBCODER_TOKEN=\"{tok}\"' >> ~/.bashrc   # zsh 用户改成 ~/.zshrc",
        "codex_config_toml": f'[mcp_servers.webcoder]\nurl = "{mcp}"\nbearer_token_env_var = "WEBCODER_TOKEN"\n'
                             "tool_timeout_sec = 120",
        "codex_oauth": f"codex mcp add webcoder --url {mcp}\ncodex mcp login webcoder --scopes read,write",
    }
