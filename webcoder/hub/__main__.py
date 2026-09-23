"""webcoder-hub command line: create the admin account and run the hub."""
from __future__ import annotations

import argparse
import getpass
import logging
import os
import sys
from pathlib import Path

from webcoder import VERSION


def _password(prompt_twice: bool = True) -> str:
    password = os.environ.get("WEBCODER_ADMIN_PASSWORD")
    if password:
        return password
    password = getpass.getpass("管理员密码（至少 10 位）: ")
    if prompt_twice and password != getpass.getpass("再次输入密码: "):
        raise SystemExit("两次输入的密码不一致")
    return password


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="webcoder-hub", description="WebCoder hub")
    parser.add_argument("--data-dir", type=Path, default=None,
                        help="data folder (env WEBCODER_DATA_DIR, default ./data)")
    parser.add_argument("--public-url", default=None, help="URL clients use (env WEBCODER_PUBLIC_URL)")
    parser.add_argument("--version", action="version", version=VERSION)
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init-admin", help="create the panel administrator")
    init.add_argument("--username", default="admin")
    reset = sub.add_parser("reset-password", help="reset an administrator password (signs out all sessions)")
    reset.add_argument("--username", default="admin")
    run = sub.add_parser("run", help="start the hub")
    run.add_argument("--host", default=os.environ.get("WEBCODER_HOST", "127.0.0.1"))
    run.add_argument("--port", type=int, default=int(os.environ.get("WEBCODER_PORT", "8787")))
    run.add_argument("--log-level", default="info")
    plugin = sub.add_parser("chatgpt-plugin", help="build the uploadable ChatGPT/Codex plugin for --public-url")
    plugin.add_argument("--output", "-o", type=Path, default=None, help="zip file to write")
    plugin.add_argument("--app-id", default=None, help="optional ChatGPT app id (plugin_asdk_app_...)")
    plugin.add_argument("--skill-only", action="store_true", help="build only the skill (for Skills upload)")
    args = parser.parse_args(argv)

    from webcoder.hub.auth import ApiError, Auth
    from webcoder.hub.config import Settings
    from webcoder.hub.store import Store

    settings = Settings.from_env(data_dir=args.data_dir, public_url=args.public_url)
    if args.command == "chatgpt-plugin":
        from webcoder.hub.plugin import PLUGIN_NAME, SKILL_NAME, build_plugin_zip, build_skill_zip

        if args.skill_only:
            data, default_name = build_skill_zip(), f"{SKILL_NAME}-skill.zip"
        else:
            if not settings.public_url.startswith("https://"):
                print(f"警告：{settings.public_url} 不是 HTTPS 地址，ChatGPT 无法连接。"
                      "请用 --public-url https://你的域名 重新生成。")
            try:
                data = build_plugin_zip(settings.public_url, args.app_id)
            except ValueError as exc:
                raise SystemExit(str(exc)) from None
            default_name = f"{PLUGIN_NAME}-{VERSION}.zip"
        output = args.output or Path(default_name)
        output.write_bytes(data)
        where = "（仅技能，不含 MCP 地址）" if args.skill_only else f"，MCP 地址：{settings.mcp_url}"
        print(f"已生成 {output}（{len(data):,} 字节）{where}")
        return 0
    if args.command == "run":
        import uvicorn

        from webcoder.hub.app import WS_MAX_QUEUE, create_app, ws_frame_limit

        logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO),
                            format="%(asctime)s %(levelname)s %(name)s %(message)s")
        app = create_app(settings)
        if not app.state.runtime.auth.has_admin():
            logging.warning("no administrator yet: run `webcoder-hub init-admin` before opening the panel")
        print(f"WebCoder hub {VERSION}: panel {settings.public_url}/  ·  MCP endpoint {settings.mcp_url}")
        uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level,
                    ws_max_size=ws_frame_limit(settings), ws_max_queue=WS_MAX_QUEUE,
                    proxy_headers=True, forwarded_allow_ips=os.environ.get("WEBCODER_TRUSTED_PROXIES", "127.0.0.1"))
        return 0

    auth = Auth(Store(settings.data_dir), settings)
    try:
        if args.command == "init-admin":
            auth.create_admin(args.username, _password())
            print(f"管理员 {args.username} 已创建。启动：webcoder-hub run")
        else:
            auth.set_password(args.username, _password())
            print(f"已重置 {args.username} 的密码，所有已登录会话已退出。")
    except ApiError as exc:
        raise SystemExit(exc.message) from None
    return 0


if __name__ == "__main__":
    sys.exit(main())
