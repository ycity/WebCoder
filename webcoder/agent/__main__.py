"""webcoder-agent command line: pair, manage allowed folders, run."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import platform
import socket
import sys
import urllib.error
import urllib.request
from logging.handlers import RotatingFileHandler
from pathlib import Path

from webcoder import VERSION
from webcoder.agent.commands import CommandOps, check_name
from webcoder.agent.config import AgentConfig, default_config_path
from webcoder.agent.lifecycle import acquire_instance_lock
from webcoder.agent.mobile import MobileOps
from webcoder.agent.remote import HOST_RE
from webcoder.agent.skills import SkillOps
from webcoder.common.channel import ChannelError, open_secret, pairing_key
from webcoder.common.errors import WebCoderError


def pair_device(hub_url: str, code: str, roots: list[str], config_path: Path, proxy: str = "system") -> AgentConfig:
    """Exchange a one-time pairing code for device credentials and save the config."""
    hub = hub_url.strip().rstrip("/")
    if not hub.startswith(("http://", "https://")):
        raise SystemExit("--hub must start with http:// or https://")
    # Check everything local first: pairing uses up the one-time code and replaces the device secret.
    config_path = Path(config_path)
    previous = AgentConfig.load(config_path) if config_path.exists() else None
    wanted = []
    for root in roots:
        resolved = str(Path(root).expanduser().resolve())
        if not os.path.isdir(resolved):
            raise SystemExit(f"--root {root!r} is not an existing folder")
        wanted.append(resolved)
    private, public = pairing_key()  # the device secret comes back sealed for this one-time key
    payload = json.dumps({"code": code, "hostname": socket.gethostname(), "platform": platform.system(),
                          "agent_version": VERSION, "key": public}).encode("utf-8")
    headers = {"Content-Type": "application/json", "User-Agent": f"webcoder-agent/{VERSION}"}
    request = urllib.request.Request(hub + "/api/agent/pair", data=payload, method="POST", headers=headers)
    handlers = [] if proxy == "system" else [urllib.request.ProxyHandler({} if proxy in ("none", "") else
                                                                         {"http": proxy, "https": proxy})]
    opener = urllib.request.build_opener(*handlers)
    try:
        with opener.open(request, timeout=20) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            message = json.loads(body)["error"]["message"]
        except (ValueError, KeyError, TypeError):
            message = body[:300]
        raise SystemExit(f"Pairing failed (HTTP {exc.code}): {message}") from None
    except urllib.error.URLError as exc:
        raise SystemExit(f"Cannot reach the hub at {hub}: {exc.reason}") from None
    try:  # a hub from before sealed pairing still sends it as it is
        secret = open_secret(private, data, data["device_id"]) if "sealed_secret" in data else data["device_secret"]
    except (ChannelError, KeyError, ValueError) as exc:
        raise SystemExit(f"Pairing failed: the hub's reply could not be read ({exc})") from None

    # Re-pairing the same device keeps what it may access. A different device (e.g. added again after the old
    # one was deleted) starts with no folders, phones or servers; local preferences carry over either way.
    same = previous if previous is not None and previous.device_id == data["device_id"] else None
    allowed = list(same.allowed_roots) if same else []
    allowed += [root for root in dict.fromkeys(wanted) if root not in allowed]
    config = AgentConfig(hub_url=hub, device_id=data["device_id"], device_secret=secret,
                         allowed_roots=allowed, read_only=previous.read_only if previous else False,
                         proxy=proxy if proxy != "system" or not previous else previous.proxy,
                         deny=previous.deny if previous else None,
                         max_write_mb=previous.max_write_mb if previous else 16,
                         backup_keep=previous.backup_keep if previous else 20,
                         mobile=same.mobile if same else False,
                         mobile_tools=previous.mobile_tools if previous else None,
                         ssh_hosts=list(same.ssh_hosts) if same else [],
                         ssh_command=previous.ssh_command if previous else None,
                         git_command=previous.git_command if previous else None,
                         skills=same.skills if same else False,
                         skill_dirs=previous.skill_dirs if previous else None,
                         commands=list(same.commands) if same else [],
                         command_paths=previous.command_paths if previous else None, path=config_path)
    config.save()
    return config


def _folder(folder: str, create: bool) -> str:
    path = Path(folder).expanduser()
    if not path.is_absolute():
        raise SystemExit(f"{folder}: use an absolute path")
    if not path.exists():
        if not create:
            raise SystemExit(f"{folder} does not exist (add --create to create it)")
        path.mkdir(parents=True, exist_ok=True)
    if not path.is_dir():
        raise SystemExit(f"{folder} is not a folder")
    return str(path.resolve())


def _load(path: Path) -> AgentConfig:
    if not path.exists():
        raise SystemExit(f"No agent config at {path}. Pair first: webcoder-agent pair --hub <url> --code <code> "
                         "--root <folder>")
    return AgentConfig.load(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="webcoder-agent", description="WebCoder agent: lets your hub read and "
                                     "write files inside the folders you allow on this device.")
    parser.add_argument("--config", type=Path, default=default_config_path(), help="config file (default: %(default)s)")
    parser.add_argument("--version", action="version", version=VERSION)
    sub = parser.add_subparsers(dest="command", required=True)

    pair = sub.add_parser("pair", help="pair this device using a one-time code from the hub panel")
    pair.add_argument("--hub", required=True, help="hub URL, e.g. https://hub.example.com")
    pair.add_argument("--code", required=True, help="pairing code shown in the panel (XXXX-XXXX)")
    pair.add_argument("--root", action="append", default=[], help="folder the hub may access (repeatable)")
    pair.add_argument("--proxy", default="system", help="'system' (default), 'none', or a proxy URL")

    roots = sub.add_parser("roots", help="manage the folders the hub may access (a running agent applies changes)")
    roots_sub = roots.add_subparsers(dest="action", required=True)
    roots_sub.add_parser("list")
    add = roots_sub.add_parser("add")
    add.add_argument("folder")
    add.add_argument("--create", action="store_true", help="create the folder if it does not exist")
    remove = roots_sub.add_parser("remove")
    remove.add_argument("folder")
    replace = roots_sub.add_parser("set", help="replace the whole list of allowed folders")
    replace.add_argument("folders", nargs="*")
    replace.add_argument("--create", action="store_true", help="create folders that do not exist")

    read_only = sub.add_parser("read-only", help="turn the device-wide read-only switch on or off")
    read_only.add_argument("state", choices=["on", "off"])
    mobile = sub.add_parser("mobile", help="let the hub control phones / emulators attached here (adb, hdc)")
    mobile.add_argument("state", choices=["on", "off"])
    skills = sub.add_parser("skills", help="share this computer's skills (~/.codex/skills ...) read-only")
    skills.add_argument("state", choices=["on", "off"])
    commands = sub.add_parser("commands", help="command-line programs the hub may run here (e.g. lark-cli)")
    commands_sub = commands.add_subparsers(dest="action", required=True)
    commands_sub.add_parser("list")
    commands_set = commands_sub.add_parser("set", help="replace the list (no names: allow none)")
    commands_set.add_argument("programs", nargs="*")
    commands_add = commands_sub.add_parser("add")
    commands_add.add_argument("program")
    commands_remove = commands_sub.add_parser("remove")
    commands_remove.add_argument("program")
    ssh_hosts = sub.add_parser("ssh-hosts", help="servers the hub may run commands on over SSH (from here)")
    ssh_sub = ssh_hosts.add_subparsers(dest="action", required=True)
    ssh_sub.add_parser("list")
    ssh_set = ssh_sub.add_parser("set", help="replace the list (no names: allow none)")
    ssh_set.add_argument("hosts", nargs="*")
    ssh_add = ssh_sub.add_parser("add")
    ssh_add.add_argument("host")
    ssh_remove = ssh_sub.add_parser("remove")
    ssh_remove.add_argument("host")

    run = sub.add_parser("run", help="connect to the hub and serve requests")
    run.add_argument("--log-level", default="INFO")
    run.add_argument("--log-file", type=Path, help="append logs to this file (useful when running as a service)")
    sub.add_parser("show", help="print the current configuration (without the secret)")

    args = parser.parse_args(argv)
    if args.command == "pair":
        config = pair_device(args.hub, args.code, args.root, args.config, args.proxy)
        print(f"Paired as device {config.device_id}. Config saved to {config.path}")
        if not config.allowed_roots:
            print("No folders are allowed yet. Add one with: webcoder-agent roots add <folder>")
        print("Start the agent with: webcoder-agent run   (or: python -m webcoder.agent run)")
        return 0

    config = _load(args.config)
    if args.command == "roots":
        if args.action == "list":
            for root in config.allowed_roots:
                print(root)
            if not config.allowed_roots:
                print("(no allowed folders)")
            return 0
        if args.action == "set":
            config.allowed_roots = []
            for folder in args.folders:
                resolved = _folder(folder, args.create)
                if not any(os.path.normcase(resolved) == os.path.normcase(root) for root in config.allowed_roots):
                    config.allowed_roots.append(resolved)
        elif args.action == "add":
            resolved = _folder(args.folder, args.create)
            if not any(os.path.normcase(resolved) == os.path.normcase(root) for root in config.allowed_roots):
                config.allowed_roots.append(resolved)
        else:
            folder = str(Path(args.folder).expanduser().resolve())
            config.allowed_roots = [root for root in config.allowed_roots
                                    if os.path.normcase(root) != os.path.normcase(folder)]
        config.save()
        print("Allowed folders:", *(config.allowed_roots or ["(none)"]), sep="\n  ")
        print("A running agent applies this within a few seconds.")
        return 0
    if args.command == "read-only":
        config.read_only = args.state == "on"
        config.save()
        print(f"Read-only is now {args.state}. A running agent applies this within a few seconds.")
        return 0
    if args.command == "ssh-hosts":
        if args.action != "list":
            wanted = args.hosts if args.action == "set" else [args.host]
            for host in wanted:
                if not HOST_RE.match(host):
                    raise SystemExit(f"{host!r} is not a valid SSH host name (it must not start with '-')")
            if args.action == "set":
                config.ssh_hosts = list(dict.fromkeys(wanted))
            elif args.action == "add":
                config.ssh_hosts = list(dict.fromkeys([*config.ssh_hosts, args.host]))
            else:
                config.ssh_hosts = [host for host in config.ssh_hosts if host != args.host]
            config.save()
        print("Servers the hub may run commands on:", *(config.ssh_hosts or ["(none)"]), sep="\n  ")
        if args.action != "list":
            print("A running agent applies this within a few seconds. Each host must work non-interactively: "
                  "`ssh <host> true` should succeed without a password prompt.")
        return 0
    if args.command == "mobile":
        config.mobile = args.state == "on"
        config.save()
        found = [name for name, ok in MobileOps(None, tools=config.mobile_tools).status()["tools"].items() if ok]
        print(f"Phone control is now {args.state}. A running agent applies this within a few seconds.")
        if config.mobile:
            print("Found: " + (", ".join(found) if found else "neither adb nor hdc (install Android platform-tools "
                               "or DevEco Studio, or set mobile_tools in the config)"))
        return 0
    if args.command == "commands":
        if args.action != "list":
            wanted = args.programs if args.action == "set" else [args.program]
            for program in wanted:
                try:
                    check_name(program)
                except WebCoderError as exc:
                    raise SystemExit(f"{program!r}: {exc.message}") from None
            if args.action == "set":
                config.commands = list(dict.fromkeys(wanted))
            elif args.action == "add":
                config.commands = list(dict.fromkeys([*config.commands, args.program]))
            else:
                config.commands = [name for name in config.commands if name != args.program]
            config.save()
        listed = CommandOps(None, programs=config.commands, paths=config.command_paths).status()["programs"]
        print("Programs the hub may run:", *([f"{item['name']}{'' if item['found'] else '  (not found)'}"
                                             for item in listed] or ["(none)"]), sep="\n  ")
        if args.action != "list":
            print("A running agent applies this within a few seconds. Each program runs with your account and "
                  "logins; allow only tools you would let the AI drive.")
        return 0
    if args.command == "skills":
        config.skills = args.state == "on"
        config.save()
        shared = SkillOps(None, enabled=config.skills, dirs=config.skill_dirs).status()["dirs"]
        print(f"Sharing skills is now {args.state}. A running agent applies this within a few seconds.")
        if config.skills:
            print("Skill folders: " + (", ".join(shared) if shared else "none found (~/.codex/skills, "
                                       "~/.claude/skills, ~/.agents/skills; or set skill_dirs in the config)"))
        return 0
    if args.command == "show":
        shown = {key: value for key, value in config.__dict__.items() if key not in ("device_secret", "path")}
        print(json.dumps({**shown, "config": str(config.path)}, ensure_ascii=False, indent=2))
        return 0

    handlers = None
    if args.log_file:  # a background agent runs for months: keep at most ~15 MB of logs
        log_file = args.log_file.expanduser()
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers = [RotatingFileHandler(log_file, maxBytes=5 * 1024 * 1024, backupCount=2, encoding="utf-8")]
    logging.basicConfig(level=getattr(logging, str(args.log_level).upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(message)s", handlers=handlers)
    from webcoder.agent.client import run_agent  # needs websockets; keep other commands dependency-free

    if config.retired:  # exit 0: an autostart entry must not keep restarting a deleted device
        logging.warning("this device was deleted in the hub panel; pair it again to use it (config: %s)",
                        config.path)
        return 0
    lock = acquire_instance_lock(config.home)
    if lock is None:
        logging.warning("another agent is already running from %s; exiting", config.home)
        return 0
    if not config.allowed_roots:
        logging.warning("no allowed folders configured; every project request will be refused")
    try:
        asyncio.run(run_agent(config))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
