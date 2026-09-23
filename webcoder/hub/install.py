"""Commands the panel shows for setting up or reconfiguring an agent.

Which folders a device exposes is decided in the panel but applied *on the device*:
the owner runs the generated command there. The hub can never widen a device's
folders by itself, which keeps the agent's allowlist a real boundary.
"""
from __future__ import annotations

import re
from pathlib import PurePosixPath, PureWindowsPath

from webcoder.agent.commands import check_name
from webcoder.agent.remote import HOST_RE
from webcoder.common.errors import WebCoderError
from webcoder.hub.auth import ApiError

PLATFORMS = ("windows", "macos", "linux")
_ENV_REFERENCE = re.compile(r"%[^%\\/]+%|\$\{?[A-Za-z_][A-Za-z0-9_]*\}?")


def ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def sh_quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def validate_roots(roots, platform: str) -> list[str]:
    """Absolute folder paths for the device's OS, without ~ or environment variables."""
    if platform not in PLATFORMS:
        raise ApiError(400, "INVALID_PLATFORM", "系统只能是 windows、macos 或 linux")
    cleaned: list[str] = []
    for raw in roots or []:
        value = str(raw).strip().strip('"').strip("'").strip()
        if not value:
            continue
        if len(value) > 1024 or any(ch in value for ch in "\x00\r\n"):
            raise ApiError(400, "INVALID_ROOT", "文件夹路径过长或包含非法字符")
        if value.startswith("~") or _ENV_REFERENCE.search(value):
            raise ApiError(400, "INVALID_ROOT", f"请填写完整路径，不要用 ~ 或环境变量：{value}")
        if platform == "windows":
            absolute = PureWindowsPath(value).is_absolute() and not value.startswith(("\\\\.\\", "\\\\?\\"))
        else:
            absolute = PurePosixPath(value).is_absolute()
        if not absolute:
            example = "C:\\Users\\你\\项目" if platform == "windows" else "/home/你/项目"
            raise ApiError(400, "INVALID_ROOT", f"“{value}”不是绝对路径，例如 {example}")
        if value not in cleaned:
            cleaned.append(value)
    if len(cleaned) > 10:
        raise ApiError(400, "INVALID_ROOT", "一台设备最多开放 10 个文件夹")
    return cleaned


def _manual(python: str, hub: str, code: str, roots: list[str], quote, read_only: bool, mobile: bool) -> list[str]:
    """The installer's steps by hand. Folders, read-only and phone control are always set explicitly, as the
    installer does: settings left from an earlier pairing must not survive a reinstall."""
    agent = f"{python} -m webcoder.agent"
    return [f'{python} -m pip install "{hub}/downloads/webcoder-agent.zip"',
            f"{agent} pair --hub {hub} --code {code}",
            (f"{agent} roots set --create " + " ".join(quote(root) for root in roots)).rstrip(),
            f"{agent} read-only {'on' if read_only else 'off'}",
            f"{agent} mobile {'on' if mobile else 'off'}",
            f"{agent} run"]


def install_commands(hub: str, code: str, platform: str, roots: list[str], read_only: bool, autostart: bool,
                     mobile: bool = False) -> dict:
    """One-click installer (venv + pairing + folders + optional autostart) plus a manual fallback."""
    if platform == "windows":
        parts = [f"-Hub {ps_quote(hub)}", f"-Code {ps_quote(code)}"]
        if roots:
            parts.append("-Root " + ",".join(ps_quote(root) for root in roots))
        if read_only:
            parts.append("-ReadOnly")
        if not autostart:
            parts.append("-NoAutostart")
        if mobile:
            parts.append("-Mobile")
        script = ps_quote(hub + "/downloads/install-agent.ps1")
        one_click = f"& ([scriptblock]::Create((irm {script}))) " + " ".join(parts)
        manual = _manual("python", hub, code, roots, ps_quote, read_only, mobile)
        return {"shell": "Windows PowerShell", "one_click": one_click, "manual": manual}
    parts = ["--hub", sh_quote(hub), "--code", sh_quote(code)]
    for root in roots:
        parts += ["--root", sh_quote(root)]
    if read_only:
        parts.append("--read-only")
    if not autostart:
        parts.append("--no-autostart")
    if mobile:
        parts.append("--mobile")
    # Not `curl ... | bash`: when the download fails, bash reads nothing and the command still ends "successfully".
    url = sh_quote(hub + "/downloads/install-agent.sh")
    one_click = f"bash -c \"$(curl -fsSL {url} || echo 'exit 1')\" install-agent.sh " + " ".join(parts)
    manual = _manual("python3", hub, code, roots, sh_quote, read_only, mobile)
    return {"shell": "终端（Terminal）", "one_click": one_click, "manual": manual}


# Only this installation's agent is stopped: one run with its config (or with none: the default); an agent run on
# purpose with another --config is another device and keeps running.
_POSIX_STOP = ("for pid in $(pgrep -f -- '-m webcoder[.]agent'); do "
               "case \"$(ps -o args= -p \"$pid\" 2>/dev/null)\" in "
               "*\"--config $HOME/.webcoder-agent/config.json\"*) kill \"$pid\";; *--config*) ;; *) kill \"$pid\";; "
               "esac; done")
UNINSTALL = {
    "windows": "Stop-ScheduledTask -TaskName 'WebCoder Agent' -ErrorAction SilentlyContinue; "
               "Unregister-ScheduledTask -TaskName 'WebCoder Agent' -Confirm:$false -ErrorAction SilentlyContinue; "
               "$cfg = Join-Path $env:USERPROFILE '.webcoder-agent\\config.json'; "
               "Get-CimInstance Win32_Process -Filter \"Name like 'python%'\" | "
               "Where-Object { $_.CommandLine -like '*-m webcoder.agent*' -and "
               "($_.CommandLine.Contains($cfg) -or $_.CommandLine -notlike '*--config*') } | "
               "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }; "
               "Start-Sleep -Seconds 1; Remove-Item -Recurse -Force \"$env:USERPROFILE\\.webcoder-agent\" "
               "-ErrorAction SilentlyContinue",
    "macos": "launchctl bootout \"gui/$(id -u)/com.webcoder.agent\" 2>/dev/null; "
             f"rm -f ~/Library/LaunchAgents/com.webcoder.agent.plist; {_POSIX_STOP}; rm -rf ~/.webcoder-agent",
    "linux": "systemctl --user disable --now webcoder-agent 2>/dev/null; "
             "rm -f ~/.config/systemd/user/webcoder-agent.service; systemctl --user daemon-reload 2>/dev/null; "
             f"{_POSIX_STOP}; rm -rf ~/.webcoder-agent",
}


def uninstall_command(platform: str) -> str:
    """Remove the agent completely: autostart entry, running process, program, config and edit backups.
    Self-contained, so it works even when the hub is gone."""
    return UNINSTALL[platform]


AGENT_PYTHON = {"windows": r'& "$env:USERPROFILE\.webcoder-agent\venv\Scripts\python.exe"',
                "posix": "~/.webcoder-agent/venv/bin/python"}


def validate_hosts(hosts) -> list[str]:
    """SSH host names as the device's ssh config knows them; never starting with '-' (no option injection)."""
    cleaned: list[str] = []
    for raw in hosts or []:
        value = str(raw).strip()
        if not value:
            continue
        if not HOST_RE.match(value):
            raise ApiError(400, "INVALID_HOST",
                           f"“{value}”不是有效的 SSH 主机名（字母、数字、. _ - @ :，不能以 - 开头）")
        if value not in cleaned:
            cleaned.append(value)
    if len(cleaned) > 50:
        raise ApiError(400, "INVALID_HOST", "最多 50 台服务器")
    return cleaned


def validate_programs(programs) -> list[str]:
    """Command-line programs (by command name) the owner allows; never shells, interpreters or launchers."""
    cleaned: list[str] = []
    for raw in programs or []:
        value = str(raw).strip()
        if not value:
            continue
        try:
            check_name(value)
        except WebCoderError as exc:
            raise ApiError(400, "INVALID_PROGRAM", f"“{value}”不能加入：{exc.message}") from None
        if value not in cleaned:
            cleaned.append(value)
    if len(cleaned) > 30:
        raise ApiError(400, "INVALID_PROGRAM", "最多 30 个程序")
    return cleaned


def roots_command(platform: str, roots: list[str], mobile: bool | None = None,
                  ssh_hosts: list[str] | None = None, skills: bool | None = None,
                  programs: list[str] | None = None) -> str:
    """Replace the device's allowed folders (and set phone control / SSH servers); a running agent applies
    it within seconds."""
    windows = platform == "windows"
    python = AGENT_PYTHON["windows" if windows else "posix"]
    quote = ps_quote if windows else sh_quote
    commands = [(f"{python} -m webcoder.agent roots set --create " + " ".join(quote(root) for root in roots)).rstrip()]
    if mobile is not None:
        commands.append(f"{python} -m webcoder.agent mobile {'on' if mobile else 'off'}")
    if ssh_hosts is not None:
        commands.append((f"{python} -m webcoder.agent ssh-hosts set " + " ".join(quote(h) for h in ssh_hosts)).rstrip())
    if skills is not None:
        commands.append(f"{python} -m webcoder.agent skills {'on' if skills else 'off'}")
    if programs is not None:
        commands.append((f"{python} -m webcoder.agent commands set " + " ".join(quote(p) for p in programs)).rstrip())
    if not windows:
        return " && ".join(commands)
    # PowerShell 5.1 has no &&: after a failed step (e.g. a folder that cannot be created) stop, so that the
    # settings are not applied half.
    return commands[0] + "".join(f"; if ($LASTEXITCODE -eq 0) {{ {command} }}" for command in commands[1:])
