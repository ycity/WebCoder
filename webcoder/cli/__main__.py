"""webcoder: send a coding task for this folder to ChatGPT on the web, and follow it from the terminal.

    webcoder "把登录页的报错修好"          the task goes to this folder's ChatGPT conversation
    webcoder -n -m gpt-5-thinking "..."   a new conversation, with that model
    webcoder link                        link a browser that has the WebCoder extension
    webcoder dot "看看这个项目的测试"       a request for your ChatGPT dot chat, about this folder

The folder becomes a WebCoder project on this computer's agent (the same identity: no token needed). The hub
hands the task to your browser's WebCoder extension, which types it into chatgpt.com; ChatGPT reads and edits
the files through the WebCoder app (MCP). The reply streams to stdout, progress and tool calls to stderr.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import socket
import stat
import sys
import time
import uuid
from pathlib import Path

from webcoder import VERSION
from webcoder.cli.client import HubClient, HubError

COMMANDS = ("link", "status", "browsers", "forget", "config", "dot", "run")
DEFAULTS = {"app": "WebCoder", "model": None, "auto_approve": True, "browser": None}
WRITE_TOOLS = {"write_file", "edit_file", "delete_path", "move_path", "make_dir", "copy_file", "restore_backup"}
NEW_CONVERSATION = """请使用「{app}」应用（WebCoder 的 MCP 工具）完成下面的编码任务。

项目：{alias}（{device} 上的 {root}）。调用工具时传 project="{alias}"，路径相对于项目根目录。
- 先用 list_dir / find_files / search / read_file 了解相关代码，再动手。
- 直接用 edit_file / write_file 修改文件（带上最近一次读取得到的 expected_sha256），不要只在回复里贴代码。
- 是 git 仓库时可以用 git_status / git_diff 检查改动；除非任务要求，不要提交或推送。
- 完成后简要说明改了哪些文件、为什么。

任务：
{prompt}"""
FOLLOW_UP = """（项目 {alias}，继续用「{app}」应用的工具操作文件）
{prompt}"""
PLAIN = "{prompt}"  # --no-app: the task as typed, no app attached


def home() -> Path:
    return Path(os.environ.get("WEBCODER_HOME") or (Path.home() / ".webcoder"))


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def load_settings() -> dict:
    return {**DEFAULTS, **_read_json(home() / "config.json")}


@contextlib.contextmanager
def locked(path: Path, timeout: float = 15.0):
    """A lock between webcoder processes (several projects can run tasks at once): a file created exclusively;
    one left by a crashed run counts as free after 30 s."""
    lock = path.with_name(path.name + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    while True:
        try:
            os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            break
        except FileExistsError:
            with contextlib.suppress(OSError):
                if time.time() - lock.stat().st_mtime > 30:
                    lock.unlink()
                    continue
            if time.monotonic() > deadline:
                raise SystemExit(f"{lock} 一直被占用；没有别的 webcoder 在运行时，删掉它再试") from None
            time.sleep(0.05)
    try:
        yield
    finally:
        with contextlib.suppress(OSError):
            lock.unlink()


def update_sessions(change) -> None:
    """Change sessions.json as it is now (re-read under the lock), so tasks finishing at the same time in
    different folders keep each other's conversations."""
    path = home() / "sessions.json"
    with locked(path):
        sessions = _read_json(path)
        change(sessions)
        _write_json(path, sessions)


def piped_input() -> str:
    """What was piped in (`git diff | webcoder "..."`): only from a pipe or a file, never a console, so a
    terminal (or an empty, never-closed stdin of some launcher) does not make webcoder wait."""
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return ""
        mode = os.fstat(sys.stdin.fileno()).st_mode
    except (OSError, ValueError, AttributeError):  # e.g. a replaced stdin without a file descriptor
        return ""
    if not (stat.S_ISFIFO(mode) or stat.S_ISREG(mode)):
        return ""
    return sys.stdin.read()


def with_context(prompt: str, piped: str) -> str:
    """The task, with what was piped in attached as a fenced block (a fence longer than any inside it)."""
    piped = piped.strip("\n")
    if not piped.strip():
        return prompt
    if not prompt:
        return piped
    longest = max((len(run) for run in re.findall(r"`+", piped)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{prompt}\n\n通过管道传入的内容：\n{fence}\n{piped}\n{fence}"


# --------------------------------------------------------------------------- folders
def project_root(start: Path, here: bool) -> Path:
    """The folder a task is about: the top of the git repository it is in (git tools need that), else itself."""
    start = start.resolve()
    if here:
        return start
    for folder in (start, *start.parents):
        if (folder / ".git").exists():
            return folder
    return start


def too_broad(root: Path) -> bool:
    """A whole drive or the home folder: far more than one coding project, and full of secrets."""
    resolved = root.resolve()
    return resolved.parent == resolved or os.path.normcase(str(resolved)) == os.path.normcase(str(Path.home()))


def ensure_allowed(config, root: Path, out) -> bool:
    """Add `root` to the agent's allowed folders when none covers it. True when the list changed. The config is
    read again under a lock, so webcoder runs starting at once in different folders keep each other's folder."""
    from webcoder.agent.config import AgentConfig
    from webcoder.agent.fileops import is_within

    real = os.path.realpath(str(root))

    def covered(roots) -> bool:
        return any(is_within(real, os.path.realpath(os.path.expanduser(allowed))) for allowed in roots)

    if covered(config.allowed_roots):
        return False
    with locked(Path(config.path)):
        fresh = AgentConfig.load(config.path)
        if covered(fresh.allowed_roots):
            config.allowed_roots = fresh.allowed_roots
            return False
        fresh.allowed_roots = [*fresh.allowed_roots, real]
        fresh.save()
    config.allowed_roots = fresh.allowed_roots
    out.note(f"已把 {real} 加入本机 Agent 允许访问的文件夹（运行中的 Agent 几秒内生效；"
             f"收回：webcoder-agent roots remove \"{real}\"）")
    return True


def session_key(config, root: Path) -> str:
    return f"{config.hub_url}|{config.device_id}|{os.path.normcase(str(root))}"


# --------------------------------------------------------------------------- output
class Output:
    """Reply text to stdout as it streams; everything else to stderr."""

    def __init__(self, quiet: bool = False):
        self.quiet = quiet
        self.printed = ""       # reply text already on stdout
        self.diverged = False   # the reply was rewritten (not just extended): the final text is printed whole
        self.mid_line = False   # stdout's last character was not a newline
        tty = sys.stderr.isatty() and not os.environ.get("NO_COLOR")
        self.color = tty and _enable_vt()
        self.shared = sys.stdout.isatty() and sys.stderr.isatty()  # one terminal: keep lines apart

    def _paint(self, text: str, code: str) -> str:
        return f"\x1b[{code}m{text}\x1b[0m" if self.color else text

    def line(self, text: str, code: str = "2") -> None:
        if self.quiet:
            return
        if self.mid_line and self.shared:
            sys.stdout.write("\n")
            sys.stdout.flush()
            self.mid_line = False
        sys.stderr.write(self._paint(text, code) + "\n")
        sys.stderr.flush()

    def note(self, text: str) -> None:
        self.line(text, "2")

    def warn(self, text: str) -> None:
        if self.mid_line and self.shared:
            sys.stdout.write("\n")
            self.mid_line = False
        sys.stderr.write(self._paint("! " + text, "33") + "\n")
        sys.stderr.flush()

    def error(self, text: str) -> None:
        if self.mid_line and self.shared:
            sys.stdout.write("\n")
            self.mid_line = False
        sys.stderr.write(self._paint("✗ " + text, "31") + "\n")
        sys.stderr.flush()

    def write(self, text: str) -> None:
        if not text:
            return
        sys.stdout.write(text)
        sys.stdout.flush()
        self.printed += text
        self.mid_line = not text.endswith("\n")

    def reply(self, text: str) -> None:
        """The whole reply so far: print what is new, if it only grew."""
        if self.quiet or self.diverged:
            return
        if text.startswith(self.printed):
            self.write(text[len(self.printed):])
        else:
            self.diverged = True

    def final(self, text: str) -> None:
        if self.quiet:
            if text:
                sys.stdout.write(text.rstrip("\n") + "\n")
            return
        if not self.diverged and text.startswith(self.printed):
            self.write(text[len(self.printed):])
        elif text:
            self.line("—— 完整回复 ——")
            self.printed = ""
            self.write(text)
        if self.mid_line:
            self.write("\n")


def _enable_vt() -> bool:
    if os.name != "nt":
        return True
    try:  # let the Windows console interpret ANSI colors
        import ctypes

        kernel = ctypes.windll.kernel32
        handle = kernel.GetStdHandle(-12)  # stderr
        mode = ctypes.c_uint32()
        if not kernel.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(kernel.SetConsoleMode(handle, mode.value | 0x0004))
    except Exception:  # noqa: BLE001 - no colors then
        return False


def show_event(event: dict, out: Output, state: dict) -> None:
    kind = event.get("type")
    if kind == "browser":
        out.note(f"→ 浏览器：{event.get('name')}")
    elif kind == "status":
        out.note(f"· {event.get('text', '')}")
    elif kind == "model":
        out.note(f"· 模型：{event.get('text', '')}")
    elif kind == "conversation":
        if event.get("url") and event["url"] != state.get("conversation"):
            state["conversation"] = event["url"]
            out.note(f"· 会话：{event['url']}")
    elif kind == "approval":
        decision = event.get("decision")
        what = event.get("tool") or event.get("text") or "工具调用"
        out.line(f"  ✔ 已自动批准：{what}" if decision != "pending" else f"  ⏸ 等你在浏览器里确认：{what}",
                 "36" if decision != "pending" else "33")
    elif kind == "tool":
        status = event.get("status") or "ok"
        name, target = event.get("name", ""), event.get("target", "")
        mark = "✓" if status == "ok" else f"✗ {status}"
        out.line(f"  ⚙ {name}  {target}  {mark}  {event.get('ms', 0)} ms", "36" if status == "ok" else "33")
        if status == "ok" and name in WRITE_TOOLS:
            state.setdefault("changed", []).append(target)
    elif kind == "warning":
        out.warn(event.get("text", ""))
    elif kind == "delta":
        if not out.quiet and not out.diverged:
            out.write(event.get("text", ""))
    elif kind == "text":
        out.reply(event.get("text", ""))


# --------------------------------------------------------------------------- commands
def load_agent(path: Path | None):
    from webcoder.agent.config import AgentConfig, default_config_path

    path = path or default_config_path()
    if not path.exists():
        raise SystemExit(f"这台电脑还没有配对 WebCoder Agent（找不到 {path}）。先在 Hub 面板「设备」里添加这台电脑，"
                         "运行它给出的安装命令。")
    config = AgentConfig.load(path)
    if config.retired or not config.device_secret:
        raise SystemExit("这台电脑的 Agent 已在 Hub 面板里删除，请重新配对")
    return config


def ensure_project(client: HubClient, root: Path, alias: str | None, changed: bool) -> dict:
    deadline = time.monotonic() + (15 if changed else 0)
    while True:
        try:
            return client.post("/api/cli/project", {"root": str(root), "alias": alias}, timeout=30)
        except HubError as exc:
            if exc.code == "ROOT_NOT_ALLOWED" and time.monotonic() < deadline:
                time.sleep(1.5)  # the running agent has not picked up the new folder yet
                continue
            if exc.code == "DEVICE_OFFLINE":
                raise SystemExit("这台电脑的 Agent 没有连上 Hub：确认 webcoder-agent 正在运行"
                                 "（Windows 计划任务「WebCoder Agent」）") from None
            raise


def run_task(args, settings: dict) -> int:
    out = Output(quiet=args.quiet)
    prompt = " ".join(args.prompt).strip()
    prompt = with_context("" if prompt == "-" else prompt, "" if args.no_stdin else piped_input()).strip()
    if not prompt:
        raise SystemExit("缺少任务内容：webcoder \"要做的事\"（或从标准输入传入）")
    config = load_agent(args.agent_config)
    root = project_root(Path.cwd(), args.here)
    if too_broad(root) and not args.allow_broad:
        raise SystemExit(f"{root} 是整个磁盘或用户目录，范围太大。进入具体的项目文件夹再运行，或加 --allow-broad")
    client = HubClient.from_config(config)
    changed = ensure_allowed(config, root, out)
    project = ensure_project(client, root, args.alias, changed)
    out.note(f"WebCoder · 项目 {project['alias']} · {project['root']}"
             + ("（新建项目）" if project.get("created") else ""))
    for warning in project.get("warnings") or []:
        out.warn(warning)

    key = session_key(config, root)
    session = None if args.new else _read_json(home() / "sessions.json").get(key)
    model = args.model or (session or {}).get("model") or settings.get("model")
    if session and args.model and session.get("model") != args.model:
        out.note(f"· 指定了不同的模型（{args.model}），开一个新会话")
        session = None
    app = None if args.no_app else args.app or settings.get("app") or DEFAULTS["app"]
    template = PLAIN if app is None else FOLLOW_UP if session else NEW_CONVERSATION
    message = template.format(app=app, alias=project["alias"], root=project["root"],
                              device=socket.gethostname() or "这台电脑", prompt=prompt)
    payload = {"project_id": project["id"], "prompt": message, "conversation": (session or {}).get("conversation"),
               "model": model if not session else None, "app": app,
               "auto_approve": not args.confirm and bool(settings.get("auto_approve", True)),
               "timeout": args.timeout, "browser": args.browser or settings.get("browser")}
    submitted = client.post("/api/cli/jobs", payload, timeout=30)
    job_id = submitted["job_id"]
    out.note("→ 已提交" + ("（继续上次的会话）" if session else "（新会话）"))

    state: dict = {"conversation": (session or {}).get("conversation")}
    after, failures, result = 0, 0, None
    try:
        while result is None:
            try:
                reply = client.post(f"/api/cli/jobs/{job_id}/events", {"after": after, "wait": 25}, timeout=45)
                failures = 0
            except HubError as exc:
                failures += 1
                if exc.code in ("NOT_FOUND",) or failures > 5:
                    raise
                time.sleep(min(2 * failures, 10))
                continue
            for event in reply.get("events") or []:
                after = max(after, int(event.get("seq", after)))
                if event.get("type") in ("done", "error", "cancelled"):
                    result = event
                    break
                show_event(event, out, state)
            if result is None and reply.get("state") not in (None, "running"):
                result = {"type": reply["state"]}
    except KeyboardInterrupt:
        with contextlib.suppress(HubError):
            client.post(f"/api/cli/jobs/{job_id}/cancel", {}, timeout=10)
        out.error("已取消（浏览器里的生成也会停止）")
        return 130

    conversation = result.get("url") or state.get("conversation")
    if conversation:
        record = {"conversation": conversation, "model": model, "project": project["alias"], "updated": time.time()}
        update_sessions(lambda sessions: sessions.__setitem__(key, record))
    if result["type"] == "done":
        out.final(result.get("text", ""))
        files = list(dict.fromkeys(state.get("changed") or []))
        summary = f"✓ 完成 · 改动 {len(files)} 处" if files else "✓ 完成"
        out.line(summary + (f" · {conversation}" if conversation else ""), "32")
        for target in files:
            out.line(f"    {target}", "32")
        return 0
    if result["type"] == "cancelled":
        out.error("任务已取消")
        return 130
    out.final("")
    out.error(f"{result.get('code') or 'ERROR'}: {result.get('text') or '任务失败'}")
    return 1


def cmd_link(args) -> int:
    config = load_agent(args.agent_config)
    client = HubClient.from_config(config)
    before = {row["id"] for row in client.post("/api/cli/status")["browsers"]}
    code = client.post("/api/cli/browser-code")
    print("在浏览器的 WebCoder 扩展里（点工具栏上的扩展图标）填写：")
    print(f"  Hub 地址：{config.hub_url}")
    print(f"  连接码：  {code['code']}    （15 分钟内有效，只能用一次）")
    print("等待扩展连上……（Ctrl+C 退出）")
    deadline = time.time() + 900
    try:
        while time.time() < deadline:
            time.sleep(3)
            for row in client.post("/api/cli/status")["browsers"]:
                if row["id"] not in before and row["online"]:
                    print(f"✓ 已连接：{row['name']}（{row['id']}）")
                    return 0
    except KeyboardInterrupt:
        return 130
    print("连接码已过期，重新运行 webcoder link")
    return 1


def cmd_status(args) -> int:
    config = load_agent(args.agent_config)
    client = HubClient.from_config(config)
    status = client.post("/api/cli/status")
    print(f"Hub      {config.hub_url}（{status['hub_version']}）· 账号 {status['user']}")
    print(f"设备     {status['device']['name']}（{'在线' if status['device']['online'] else '离线'}）")
    browsers = status["browsers"]
    print("浏览器   " + ("、".join(f"{b['name']}（{'在线' if b['online'] else '离线'} · {b['id']}）" for b in browsers)
                       if browsers else "还没有连接扩展：运行 webcoder link"))
    root = project_root(Path.cwd(), args.here)
    from webcoder.agent.fileops import is_within

    match = [p for p in status["projects"] if os.path.normcase(p["root"]) == os.path.normcase(str(root))]
    print(f"本文件夹 {root} → " + (f"项目 {match[0]['alias']}（{'读写' if match[0]['mode'] == 'rw' else '只读'}）"
                                 if match else "还不是项目（第一次运行任务时自动创建）"))
    if not any(is_within(os.path.realpath(str(root)), os.path.realpath(r)) for r in config.allowed_roots):
        print("         （不在 Agent 允许的文件夹里，第一次运行任务时自动加入）")
    session = _read_json(home() / "sessions.json").get(session_key(config, root))
    if session:
        print(f"会话     {session['conversation']}" + (f"（{session['model']}）" if session.get("model") else ""))
    return 0


def cmd_browsers(args) -> int:
    config = load_agent(args.agent_config)
    client = HubClient.from_config(config)
    if args.revoke:
        client.post("/api/cli/browsers/revoke", {"id": args.revoke})
        print(f"已撤销 {args.revoke}")
        return 0
    for row in client.post("/api/cli/status")["browsers"]:
        print(f"{row['id']}  {row['name']}  {'在线' if row['online'] else '离线'}")
    return 0


def cmd_forget(args) -> int:
    config = load_agent(args.agent_config)
    removed = []
    key = session_key(config, project_root(Path.cwd(), args.here))
    update_sessions(lambda sessions: removed.append(sessions.pop(key, None)))
    print("已忘记这个文件夹的 ChatGPT 会话，下次运行开新会话" if removed[0] else "这个文件夹没有记住的会话")
    return 0


def cmd_config(args) -> int:
    path = home() / "config.json"
    stored = _read_json(path)
    if args.key is None:
        print(json.dumps({**DEFAULTS, **stored}, ensure_ascii=False, indent=2))
        return 0
    if args.key not in DEFAULTS:
        raise SystemExit(f"未知设置 {args.key}（可用：{', '.join(DEFAULTS)}）")
    if args.value is None:
        print(json.dumps({**DEFAULTS, **stored}.get(args.key), ensure_ascii=False))
        return 0
    value = args.value
    if args.key == "auto_approve":
        value = value.lower() in ("1", "true", "yes", "on")
    elif value in ("", "none", "null"):
        value = None
    stored[args.key] = value
    _write_json(path, stored)
    print(f"{args.key} = {json.dumps(value, ensure_ascii=False)}")
    return 0


# --------------------------------------------------------------------------- dot
DOT_ACTIONS = ("setup", "status", "test")
DOT_MAX_TEXT = 6000
DOORBELL_FOLDER = "dot-doorbell"
DOORBELL_README = """# WebCoder 门铃

`webcoder dot` 用的固定项目：Hub 里 dot 收提醒的讨论室挂在这个项目上。不用往这里放东西。
"""
SLOT_STATUS = {"invited": "等 dot 用加入码登记", "code_expired": "加入码已过期：重新运行 webcoder dot setup 换一个",
               "expired": "已到期：运行 webcoder dot setup，让 dot 重新加入", "revoked": "已撤销",
               "authorization_unavailable": "dot 加入时用的 WebCoder 授权已失效：运行 webcoder dot setup --new",
               "room_paused": "讨论室在 Hub 面板里被暂停了"}
NOTICE_STATE = {"delivering": "· 正在通知 dot", "accepted": "· dot 的宿主已收到通知（不代表 dot 已读）"}
NOTICE_FAILED = {"not_subscribed": "dot 没有订阅提醒：运行 webcoder dot setup，把指令再发给 dot",
                 "recipient_unavailable": "dot 的位置已到期或授权失效：运行 webcoder dot setup",
                 "rate_limited": "一分钟内的提醒太多，这条没有发给 dot，稍后再试",
                 "notifications_disabled": "Hub 没有开启 MCP Events",
                 "failed": "多次重试后仍然没能通知到 dot（宿主的回调不可用）"}


def _when(stamp) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(stamp)) if stamp else "—"


def _test_state(test: dict | None) -> str:
    if not test:
        return "还没发过测试事件"
    if test["state"] == "accepted":
        return f"测试事件 {test['event_id']} 已被宿主接收"
    if test["state"] in ("dead_letter", "abandoned"):
        return f"测试事件 {test['event_id']} 投递失败"
    return f"测试事件 {test['event_id']} 投递中"


def show_doorbell(state: dict) -> None:
    if not state.get("setup"):
        print("还没有设置 dot 门铃：运行 webcoder dot setup")
        return
    slot, project = state["slot"], state.get("project") or {}
    print(f"门铃     项目 {project.get('alias', '?')}（{project.get('root', '')}）")
    status = SLOT_STATUS.get(slot["status"], "已登记" if slot["state"] == "registered" else slot["status"])
    print(f"dot 位置 {status} · {_when(slot['expires_at'])} 到期")
    if slot["state"] != "registered":
        return
    mentions = state["mentions"]
    print("提醒订阅 " + ("已订阅" if mentions["subscribed"]
                         else "还没订阅：把 webcoder dot setup 给出的指令发到 dot 聊天"))
    tests = [route["test"] for route in mentions["routes"] if route.get("test")]
    if mentions["subscribed"]:
        print(f"测试     {_test_state(tests[-1] if tests else None)}")
        if mentions.get("test_reply"):
            print(f"         dot 已回复：{mentions['test_reply']['text'][:80]}")
    speech = state["speech"]
    print("发言权限 " + (f"已允许 dot 回复，{_when(speech['expires_at'])} 到期（自动续期）" if speech["enabled"]
                         else "还没允许（测试或第一次发请求时自动允许 dot 回复）"))
    reach = state.get("connection")
    if reach:
        print(f"dot 授权 {reach['label']} · {'全部项目' if reach['all_projects'] else '部分项目'} · "
              f"{'可读写' if reach['write'] else '只读'}" + ("（已撤销）" if reach["revoked"] else ""))


def dot_setup(args, client: HubClient, config) -> int:
    out = Output()
    folder = home() / DOORBELL_FOLDER
    folder.mkdir(parents=True, exist_ok=True)
    readme = folder / "README.md"
    if not readme.exists():
        readme.write_text(DOORBELL_README, encoding="utf-8")
    project = ensure_project(client, folder, None, ensure_allowed(config, folder, out))
    state = client.post("/api/cli/dot/setup", {"project_id": project["id"], "new": args.new}, timeout=30)
    show_doorbell(state)
    if state.get("instruction"):
        print("\n把下面这段话完整发到 dot 聊天里（dot 要能用 WebCoder 应用，授权时选「全部项目」）：")
        print("-" * 60)
        print(state["instruction"])
        print("-" * 60)
        print("dot 登记并订阅后，运行 webcoder dot status 确认，再用 webcoder dot test 发一个测试事件。")
    elif state["mentions"]["subscribed"]:
        print("\n门铃已就绪：在项目文件夹里运行 webcoder dot \"要说的话\"")
    return 0


def dot_test(client: HubClient) -> int:
    state = client.post("/api/cli/dot/test", timeout=30)
    print("测试事件已发出，等 dot 的宿主接收……")
    deadline = time.monotonic() + 40
    while True:
        tests = [route["test"] for route in state["mentions"]["routes"] if route.get("test")]
        test = tests[-1] if tests else None
        if test and test["state"] in ("accepted", "dead_letter", "abandoned") or time.monotonic() > deadline:
            break
        time.sleep(2)
        state = client.post("/api/cli/dot/status", timeout=30)
    print(_test_state(test))
    if not test or test["state"] != "accepted":
        return 1
    print("等 dot 在门铃讨论室回复（最多 3 分钟）……")
    deadline = time.monotonic() + 180
    while not state["mentions"].get("test_reply") and time.monotonic() < deadline:
        time.sleep(3)
        state = client.post("/api/cli/dot/status", timeout=30)
    reply = state["mentions"].get("test_reply")
    if reply:
        print(f"✓ dot 回复了：{reply['text']}")
        print("门铃通了：在项目文件夹里运行 webcoder dot \"要说的话\"")
        return 0
    print("dot 的宿主收下了测试事件，但 3 分钟内 dot 没有在讨论室回复。稍后可以用 webcoder dot status 再看，"
          "或者在 dot 聊天里问它收到测试事件后做了什么。")
    return 1


def dot_ask(args, client: HubClient, config, words: list[str]) -> int:
    out = Output(quiet=args.quiet)
    prompt = " ".join(words).strip()
    prompt = with_context("" if prompt == "-" else prompt, "" if args.no_stdin else piped_input()).strip()
    if not prompt:
        raise SystemExit("缺少要对 dot 说的话：webcoder dot \"要说的话\"（或从标准输入传入）")
    if len(prompt) > DOT_MAX_TEXT:
        raise SystemExit(f"内容太长（{len(prompt)} 字）：发给 dot 的请求最多 {DOT_MAX_TEXT} 字")
    root = project_root(Path.cwd(), args.here)
    if too_broad(root) and not args.allow_broad:
        raise SystemExit(f"{root} 是整个磁盘或用户目录，范围太大。进入具体的项目文件夹再运行，或加 --allow-broad")
    project = ensure_project(client, root, args.alias, ensure_allowed(config, root, out))
    out.note(f"dot · 项目 {project['alias']} · {project['root']}" + ("（新建项目）" if project.get("created") else ""))
    sent = client.post("/api/cli/dot/messages", {"project_id": project["id"], "text": prompt,
                                                 "client_message_id": uuid.uuid4().hex}, timeout=30)
    for warning in sent.get("warnings") or []:
        out.warn(warning)
    message_id, state = sent["message_id"], sent["notification"].get("state")
    if state != "queued":
        out.error(NOTICE_FAILED.get(state, f"没能通知 dot（{state}）"))
        return 1
    if args.no_wait:
        out.note(f"→ 已在门铃讨论室 @dot（消息 {message_id}）；它的回复会出现在 Hub 面板的讨论室里")
        return 0
    out.note("→ 已在门铃讨论室 @dot，等它回复（Ctrl+C 停止等待）")
    deadline = time.monotonic() + args.timeout
    failures = 0
    try:
        while True:
            try:
                reply = client.post(f"/api/cli/dot/messages/{message_id}", {}, timeout=30)
                failures = 0
            except HubError as exc:
                failures += 1
                if exc.code == "NOT_FOUND" or failures > 5:
                    raise
                time.sleep(min(2 * failures, 10))
                continue
            current = reply["notification"].get("state")
            if current != state:
                state = current
                if state in NOTICE_STATE:
                    out.note(NOTICE_STATE[state])
                elif state in NOTICE_FAILED:
                    out.error(NOTICE_FAILED[state])
                    return 1
            if reply["replies"]:
                out.final("\n\n".join(item["text"] for item in reply["replies"]))
                out.line("✓ dot 已回复", "32")
                return 0
            if time.monotonic() > deadline:
                out.error(f"等了 {args.timeout} 秒还没有回复；之后的回复会出现在 Hub 面板的讨论室里")
                return 1
            time.sleep(5)
    except KeyboardInterrupt:
        out.error("已停止等待；dot 之后的回复会出现在 Hub 面板的讨论室里")
        return 130


def cmd_dot(args) -> int:
    config = load_agent(args.agent_config)
    client = HubClient.from_config(config)
    words = list(args.words)
    action = words[0] if len(words) == 1 and words[0] in DOT_ACTIONS else None
    if action == "setup":
        return dot_setup(args, client, config)
    if action == "status":
        show_doorbell(client.post("/api/cli/dot/status", timeout=30))
        return 0
    if action == "test":
        return dot_test(client)
    return dot_ask(args, client, config, words)


def common_options(parser: argparse.ArgumentParser, suppress: bool = False) -> None:
    """Options every command takes, before or after a subcommand (a subcommand's copies keep the main value
    when they are not given)."""
    extra = {"default": argparse.SUPPRESS} if suppress else {}
    parser.add_argument("--agent-config", type=Path, help="Agent 配置文件（默认 ~/.webcoder-agent/config.json）",
                        **({"default": None} | extra))
    parser.add_argument("--here", action="store_true", help="就用当前文件夹，不上溯到 git 仓库的根目录", **extra)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="webcoder", description="在当前文件夹里，通过 ChatGPT 网页版（WebCoder 应用）"
                                     "完成编码任务。子命令：link、status、browsers、forget、config、dot。")
    parser.add_argument("--version", action="version", version=VERSION)
    common_options(parser)
    return parser


def first_word(argv: list[str]) -> str | None:
    """The first argument that is not an option (or an option's value): a subcommand, or the task's start."""
    skip = False
    for item in argv:
        if skip:
            skip = False
        elif item in ("--agent-config", "-m", "--model", "--app", "--alias", "--browser", "--timeout"):
            skip = True
        elif item == "--":
            return None
        elif not item.startswith("-"):
            return item
    return None


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, ValueError):
            stream.reconfigure(errors="replace")
    command = first_word(argv)
    parser = build_parser()
    if command in COMMANDS and command != "run":
        sub = parser.add_subparsers(dest="command")
        common = argparse.ArgumentParser(add_help=False)
        common_options(common, suppress=True)
        sub.add_parser("link", parents=[common], help="连接一个装了 WebCoder 扩展的浏览器")
        sub.add_parser("status", parents=[common], help="查看 Hub、浏览器和本文件夹的状态")
        browsers = sub.add_parser("browsers", parents=[common], help="列出已连接的浏览器")
        browsers.add_argument("--revoke", metavar="ID", help="撤销一个浏览器的连接")
        sub.add_parser("forget", parents=[common], help="忘记本文件夹的 ChatGPT 会话（下次开新会话）")
        config = sub.add_parser("config", parents=[common], help="查看或修改设置（app、model、auto_approve、browser）")
        config.add_argument("key", nargs="?")
        config.add_argument("value", nargs="?")
        dot = sub.add_parser("dot", parents=[common], help="把请求交给 ChatGPT 里的 dot（setup、status、test，"
                                                           "或直接写要说的话）")
        dot.add_argument("words", nargs="*",
                         help="setup / status / test，或要对 dot 说的话（管道传入的内容会附在后面）")
        dot.add_argument("--new", action="store_true", help="setup：换一个新的 dot 位置（dot 要重新加入）")
        dot.add_argument("--timeout", type=int, default=1800, help="最长等待 dot 回复的秒数（默认 1800）")
        dot.add_argument("--no-wait", action="store_true", help="发出请求后不等回复")
        dot.add_argument("--no-stdin", action="store_true", help="不读取管道传入的内容")
        dot.add_argument("--alias", help="本文件夹第一次成为项目时用的项目名")
        dot.add_argument("--allow-broad", action="store_true", help="允许把整个磁盘或用户目录当作项目")
        dot.add_argument("-q", "--quiet", action="store_true", help="只输出 dot 的回复")
        args = parser.parse_args(argv)
        handler = {"link": cmd_link, "status": cmd_status, "browsers": cmd_browsers, "forget": cmd_forget,
                   "config": cmd_config, "dot": cmd_dot}[args.command]
    else:
        if command == "run":
            argv.remove("run")
        parser.add_argument("prompt", nargs="*", help="要做的事；通过管道传入的内容会附在后面（只有管道输入时就用它）")
        parser.add_argument("--no-stdin", action="store_true", help="不读取管道传入的内容")
        parser.add_argument("-n", "--new", action="store_true", help="开一个新的 ChatGPT 会话")
        parser.add_argument("-m", "--model", help="新会话用的模型（如 gpt-5-thinking）；和上次不同时自动开新会话")
        parser.add_argument("--confirm", action="store_true", help="不自动批准工具调用，在浏览器里逐个确认")
        parser.add_argument("--app", help="ChatGPT 里 WebCoder 应用的名字（默认见 webcoder config app）")
        parser.add_argument("--no-app", action="store_true", help="不 @ 应用，原样发送（只聊天，ChatGPT 不能动文件）")
        parser.add_argument("--alias", help="本文件夹第一次成为项目时用的项目名")
        parser.add_argument("--browser", help="用哪个已连接的浏览器（名字或 ID）")
        parser.add_argument("--timeout", type=int, default=3600, help="最长等待秒数（默认 3600）")
        parser.add_argument("--allow-broad", action="store_true", help="允许把整个磁盘或用户目录当作项目")
        parser.add_argument("-q", "--quiet", action="store_true", help="只输出最终回复")
        args = parser.parse_args(argv)
        settings = load_settings()

        def handler(parsed):
            return run_task(parsed, settings)
    try:
        return handler(args)
    except HubError as exc:
        print(f"✗ {exc.code}: {exc.message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
