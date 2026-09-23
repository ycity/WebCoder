"""UI components that show a tool's result inside the conversation (MCP Apps; ChatGPT renders them).

A tool opts in with Tool.ui; its result then carries the data for the component in _meta, which the host
passes to the component but not to the model (so an image does not fill the model's context).
"""
from __future__ import annotations

IMAGE_VIEWER = "ui://webcoder/image-viewer.html"
MIME = "text/html;profile=mcp-app"
IMAGE_META = "webcoder/image"  # key in a tool result's _meta: a data:image/...;base64 URL

# Self-contained: no external scripts, styles or images, so the default sandbox CSP (img-src 'self' data:)
# is all it needs. The image arrives as a data URL and is checked before use; text goes in via textContent.
_IMAGE_VIEWER_HTML = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  html, body { margin: 0; background: transparent; }
  body { font: 13px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; color: #6b7280; }
  figure { margin: 0; padding: 8px; }
  img { display: block; max-width: 100%; max-height: 720px; margin: 0 auto; border-radius: 10px; }
  img:not([src]) { display: none; }
  figcaption { margin-top: 6px; text-align: center; word-break: break-all; }
</style>
</head>
<body>
<figure><img id="image" alt=""><figcaption id="caption"></figcaption></figure>
<script>
(() => {
  const image = document.getElementById("image");
  const caption = document.getElementById("caption");
  const DATA_URL = /^data:image\\/(png|jpeg|gif|webp);base64,[A-Za-z0-9+\\/]+=*$/;
  const post = (message) => window.parent.postMessage(Object.assign({ jsonrpc: "2.0" }, message), "*");
  const resize = () => post({ method: "ui/notifications/size-changed", params: {
    width: document.documentElement.scrollWidth, height: document.documentElement.scrollHeight } });
  const show = (result) => {
    const data = result && result._meta && result._meta["webcoder/image"];
    const info = (result && result.structuredContent) || {};
    if (typeof data !== "string" || !DATA_URL.test(data)) return;
    image.onload = resize;
    image.alt = String(info.caption || "");
    image.src = data;
    caption.textContent = String(info.caption || "");
  };
  // MCP Apps: the View starts the handshake, then the host sends the tool result.
  window.addEventListener("message", (event) => {
    if (event.source !== window.parent) return;
    const message = event.data;
    if (!message || message.jsonrpc !== "2.0") return;
    if (message.id === 1 && "result" in message) post({ method: "ui/notifications/initialized", params: {} });
    else if (message.method === "ui/notifications/tool-result") show(message.params);
  });
  post({ id: 1, method: "ui/initialize", params: { protocolVersion: "2026-01-26",
    appInfo: { name: "webcoder-image-viewer", version: "1" },
    appCapabilities: { availableDisplayModes: ["inline"] } } });
  // ChatGPT's own bridge, for hosts that fill window.openai instead.
  const fromOpenAI = () => {
    const openai = window.openai;
    if (openai && (openai.toolResponseMetadata || openai.toolOutput)) {
      show({ _meta: openai.toolResponseMetadata || {}, structuredContent: openai.toolOutput || {} });
    }
  };
  fromOpenAI();
  window.addEventListener("openai:set_globals", fromOpenAI);
})();
</script>
</body>
</html>
"""

_RESOURCES = {
    IMAGE_VIEWER: {
        "name": "image-viewer", "title": "Image",
        "description": "Shows a picture from a tool result: a phone screenshot or an image in a project.",
        "html": _IMAGE_VIEWER_HTML,
        "_meta": {"ui": {"prefersBorder": True}, "openai/widgetPrefersBorder": True,
                  "openai/widgetDescription": "Shows the picture to the person; the image data is not in the "
                                              "model's context."},
    },
}


def resource_list() -> list[dict]:
    return [{"uri": uri, "name": item["name"], "title": item["title"], "description": item["description"],
             "mimeType": MIME, "_meta": item["_meta"]} for uri, item in _RESOURCES.items()]


def resource_read(uri) -> dict | None:
    item = _RESOURCES.get(uri) if isinstance(uri, str) else None
    if item is None:
        return None
    return {"contents": [{"uri": uri, "mimeType": MIME, "text": item["html"], "_meta": item["_meta"]}]}
