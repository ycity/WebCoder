"""Agent lifecycle on the device: one instance per config folder, and retiring when the device is deleted.

The installers register autostart under fixed names (Windows task "WebCoder Agent", launchd label
com.webcoder.agent, systemd user unit webcoder-agent), so installing again replaces the previous setup
instead of adding services. When the hub proves that this device was deleted in its panel, the agent
drops its credentials, removes the autostart entry that launches its config, and exits. The program
itself and the edit backups stay in the agent folder until the user deletes it.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

log = logging.getLogger("webcoder.agent")

TASK_NAME = "WebCoder Agent"
LAUNCHD_LABEL = "com.webcoder.agent"
SYSTEMD_UNIT = "webcoder-agent.service"


def acquire_instance_lock(home: Path):
    """Lock the agent folder for this process's lifetime; None if another agent already runs from it."""
    home.mkdir(parents=True, exist_ok=True)
    handle = open(home / "agent.lock", "a+")  # stays open: the OS releases the lock when the process ends
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def _powershell(script: str, run) -> str:
    encoded = base64.b64encode(script.encode("utf-16-le")).decode()
    result = run(["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                 capture_output=True, timeout=120)
    return result.stdout.decode("ascii", "replace").strip()


def _remove_windows_task(marker: str, run) -> str | None:
    # The task's arguments come back as base64(UTF-8), so any folder name survives the console code page.
    encoded = _powershell(
        f"$t = Get-ScheduledTask -TaskName '{TASK_NAME}' -ErrorAction SilentlyContinue\n"
        "if ($t) { [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes("
        "(($t.Actions | ForEach-Object { $_.Arguments }) -join \"`n\"))) }", run)
    try:
        arguments = base64.b64decode(encoded).decode("utf-8", "replace") if encoded else ""
    except ValueError:
        arguments = ""
    if marker.lower() not in arguments.lower():
        return None
    log.info("removing the scheduled task %r", TASK_NAME)
    _powershell(f"Unregister-ScheduledTask -TaskName '{TASK_NAME}' -Confirm:$false", run)
    return f"scheduled task {TASK_NAME!r}"


def remove_autostart(config_path, *, platform: str = sys.platform, home: Path | None = None,
                     run=subprocess.run) -> str | None:
    """Remove the autostart entry that launches `config_path` (never one that launches another config)."""
    marker = str(config_path)
    home = home or Path.home()
    try:
        if platform == "win32":
            return _remove_windows_task(marker, run)
        if platform == "darwin":
            plist = home / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
            if not (plist.is_file() and marker in plist.read_text(encoding="utf-8", errors="replace")):
                return None
            log.info("removing the launchd agent %s", plist)
            plist.unlink()
            uid = os.getuid() if hasattr(os, "getuid") else 0
            run(["launchctl", "bootout", f"gui/{uid}/{LAUNCHD_LABEL}"], capture_output=True)  # ends us
            return str(plist)
        unit = home / ".config" / "systemd" / "user" / SYSTEMD_UNIT
        if not (unit.is_file() and marker in unit.read_text(encoding="utf-8", errors="replace")):
            return None
        log.info("removing the systemd user service %s", unit)
        run(["systemctl", "--user", "disable", SYSTEMD_UNIT], capture_output=True)
        unit.unlink()
        run(["systemctl", "--user", "daemon-reload"], capture_output=True)
        run(["systemctl", "--user", "stop", SYSTEMD_UNIT], capture_output=True)  # Restart=always: ends us
        return str(unit)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("could not remove the autostart entry: %s", exc)
        return None


def retire(config, remove=remove_autostart) -> bool:
    """The hub deleted this device: forget its credentials and stop starting at logon.

    Does nothing when the config file was paired again in the meantime (it belongs to a new device,
    and so does the autostart entry). Returns True when this agent retired.
    """
    path = Path(config.path) if config.path is not None else None
    if path is None:
        return False
    try:
        on_disk = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        on_disk = None
    if not isinstance(on_disk, dict) or on_disk.get("device_id") != config.device_id:
        log.info("%s now belongs to another pairing; leaving it and the autostart entry alone", path)
        return False
    # Keep a marker instead of deleting the file, so an autostart entry that survives (e.g. removal
    # failed) starts an agent that exits quietly rather than one that fails and gets restarted.
    on_disk.update(device_secret="", retired=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # private, as save() writes it (proxy URLs ...)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(on_disk, handle, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    log.info("device credentials removed from %s; to remove the program and its backups as well, delete %s",
             path, path.parent)
    remove(path)
    return True
