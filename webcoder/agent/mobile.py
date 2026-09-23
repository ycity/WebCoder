"""Control phones and emulators attached to this computer: Android through adb, HarmonyOS through hdc.

Off unless the device owner turns it on here (``webcoder-agent mobile on``); the hub cannot enable it.
Commands run as argument lists, never through this computer's shell. Values that reach the phone's
shell are validated (serials, package names, numbers) or quoted (typed text). Files only move between
the phone and project folders, through the same sandbox as the file operations.
"""
from __future__ import annotations

import contextlib
import inspect
import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ElementTree

from webcoder.agent.procs import Stopped, run_capped, run_quiet
from webcoder.common.errors import WebCoderError

SERIAL_RE = re.compile(r"^[A-Za-z0-9._:\-]{1,128}$")
PACKAGE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+$")
COMPONENT_RE = re.compile(r"^\.?[A-Za-z_$][A-Za-z0-9_$]*(\.[A-Za-z_$][A-Za-z0-9_$]*)*$")
BOUNDS_RE = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")
MAX_OUTPUT = 8 * 1024 * 1024
MAX_SCREENSHOT = 64 * 1024 * 1024  # a lossless capture of a large screen can pass 8 MiB
MAX_LOCAL_FILE = 2 * 1024 * 1024 * 1024  # push / install / pull stay on this computer
READY = "ready"


def _text(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


def _shell_quote(value: str) -> str:
    """Quote for the phone's POSIX shell (adb shell / hdc shell join their arguments into one command line)."""
    return "'" + value.replace("'", "'\\''") + "'"


def exists_script(path: str) -> str:
    """Prints WEBCODER_EXISTS or WEBCODER_MISSING. A dangling symlink counts: writing to it would create its target."""
    quoted = _shell_quote(path)
    return f"if [ -e {quoted} ] || [ -L {quoted} ]; then echo WEBCODER_EXISTS; else echo WEBCODER_MISSING; fi"


def move_into_place_script(temp: str, remote: str) -> str:
    """Renames `temp` to `remote` unless `remote` (a file or a symlink) exists: prints WEBCODER_MOVED or
    WEBCODER_EXISTS. adb / hdc have no write that refuses to replace, so this is the closest: one command
    on the phone, with only microseconds between the check and the rename."""
    quoted, quoted_temp = _shell_quote(remote), _shell_quote(temp)
    return (f"if [ -e {quoted} ] || [ -L {quoted} ]; then echo WEBCODER_EXISTS; "
            f"else mv {quoted_temp} {quoted} && echo WEBCODER_MOVED; fi")


def _int(value, name: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise WebCoderError("INVALID_ARGUMENT", f"{name} must be an integer between {low} and {high}")
    return value


def _device_path(value) -> str:
    if (not isinstance(value, str) or not value.startswith("/") or len(value) > 512
            or any(ch in value for ch in "\x00\r\n")):
        raise WebCoderError("INVALID_ARGUMENT", "device_path must be an absolute path on the phone, e.g. /sdcard/x.txt")
    return value


def _package(value) -> str:
    if not isinstance(value, str) or not PACKAGE_RE.match(value):
        raise WebCoderError("INVALID_ARGUMENT", "package must be an app package / bundle name such as com.example.app")
    return value


# --------------------------------------------------------------------------- locating adb / hdc
def _candidates(tool: str) -> list[str]:
    exe = tool + (".exe" if os.name == "nt" else "")
    found = shutil.which(tool)
    paths = [found] if found else []
    home = os.path.expanduser("~")
    if tool == "adb":
        for var in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
            if os.environ.get(var):
                paths.append(os.path.join(os.environ[var], "platform-tools", exe))
        if os.environ.get("LOCALAPPDATA"):
            paths.append(os.path.join(os.environ["LOCALAPPDATA"], "Android", "Sdk", "platform-tools", exe))
        paths += [os.path.join(home, "Library", "Android", "sdk", "platform-tools", exe),
                  os.path.join(home, "Android", "Sdk", "platform-tools", exe)]
    else:  # hdc ships with DevEco Studio and the OpenHarmony SDK
        if os.environ.get("DEVECO_SDK_HOME"):
            paths.append(os.path.join(os.environ["DEVECO_SDK_HOME"], "default", "openharmony", "toolchains", exe))
        paths += [os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Huawei", "DevEco Studio", "sdk",
                               "default", "openharmony", "toolchains", exe),
                  "/Applications/DevEco-Studio.app/Contents/sdk/default/openharmony/toolchains/hdc"]
    return paths


def locate(tool: str, override=None) -> list[str] | None:
    """Command prefix for adb / hdc: the configured override (a path, or an argument list), else a search."""
    if override:
        return [str(part) for part in override] if isinstance(override, list) else [str(override)]
    return next(([path] for path in _candidates(tool) if path and os.path.isfile(path)), None)


def run(argv: list[str], timeout: float, limit: int = MAX_OUTPUT, tail: bool = False) -> subprocess.CompletedProcess:
    """Run adb / hdc, keeping at most `limit` bytes of its output in memory (see run_capped)."""
    try:
        return run_capped(argv, timeout, limit, tail=tail)
    except FileNotFoundError:
        raise WebCoderError("MOBILE_TOOL_MISSING", f"{argv[0]} was not found on this computer") from None
    except subprocess.TimeoutExpired:
        raise WebCoderError("MOBILE_TIMEOUT",
                            f"{os.path.basename(argv[0])} did not finish within {timeout:.0f}s") from None
    except Stopped:
        raise WebCoderError("STOPPED", "Stopped: this device was disabled or deleted in the hub panel") from None


# --------------------------------------------------------------------------- UI trees
def _node(kind, text, ident, desc, bounds, *, clickable=False, scrollable=False, checked=None, enabled=True,
          focused=False) -> dict | None:
    match = BOUNDS_RE.search(bounds or "")
    if not match:
        return None
    x1, y1, x2, y2 = (int(value) for value in match.groups())
    if x2 <= x1 or y2 <= y1:
        return None
    if not (text or desc or ident or clickable or scrollable or checked is not None):
        return None  # pure layout containers
    node = {"type": kind, "text": text, "id": ident, "desc": desc, "center": [(x1 + x2) // 2, (y1 + y2) // 2],
            "bounds": [x1, y1, x2, y2]}
    for key, value in (("clickable", clickable), ("scrollable", scrollable), ("focused", focused)):
        if value:
            node[key] = True
    if checked is not None:
        node["checked"] = checked
    if not enabled:
        node["disabled"] = True
    return node


def parse_android_ui(data: bytes) -> tuple[list[dict], list[int] | None]:
    try:
        root = ElementTree.fromstring(data)
    except ElementTree.ParseError as exc:
        raise WebCoderError("MOBILE_UI_UNAVAILABLE", f"Could not read the screen layout: {exc}") from None
    nodes, screen = [], None
    for element in root.iter("node"):
        a = element.attrib
        if screen is None and (match := BOUNDS_RE.search(a.get("bounds", ""))):
            screen = [int(match.group(3)), int(match.group(4))]
        rid = a.get("resource-id", "")
        node = _node(a.get("class", "").rsplit(".", 1)[-1] or "View", a.get("text", ""), rid.split(":id/")[-1],
                     a.get("content-desc", ""), a.get("bounds", ""), clickable=a.get("clickable") == "true",
                     scrollable=a.get("scrollable") == "true",
                     checked=(a.get("checked") == "true") if a.get("checkable") == "true" else None,
                     enabled=a.get("enabled", "true") == "true", focused=a.get("focused") == "true")
        if node:
            nodes.append(node)
    return nodes, screen


def parse_harmony_ui(data: bytes) -> tuple[list[dict], list[int] | None]:
    try:
        tree = json.loads(_text(data))
    except ValueError as exc:
        raise WebCoderError("MOBILE_UI_UNAVAILABLE", f"Could not read the screen layout: {exc}") from None
    nodes, screen, stack = [], None, [tree]
    while stack:
        item = stack.pop()
        if not isinstance(item, dict):
            continue
        a = item.get("attributes") or {}
        if screen is None and (match := BOUNDS_RE.search(str(a.get("bounds", "")))):
            screen = [int(match.group(3)), int(match.group(4))]
        flags = {key for key, value in a.items() if str(value).lower() == "true"}
        node = _node(str(a.get("type", "")) or "Component", str(a.get("text", "")),
                     str(a.get("id", "") or a.get("key", "")), str(a.get("description", "")),
                     str(a.get("bounds", "")), clickable=bool(flags & {"clickable", "longClickable"}),
                     scrollable="scrollable" in flags, checked=("checked" in flags) if "checkable" in flags else None,
                     enabled=str(a.get("enabled", "true")).lower() != "false", focused="focused" in flags)
        if node:
            nodes.append(node)
        stack.extend(reversed(item.get("children") or []))  # keep document order
    return nodes, screen


# --------------------------------------------------------------------------- backends
class Backend:
    platform = ""
    tool = ""
    server = ()

    def __init__(self, argv: list[str]):
        self.argv = argv
        self._server_started = False

    def target(self, serial: str) -> list[str]:
        raise NotImplementedError

    def _start_server(self) -> None:
        # Started on its own with no pipes: a server that inherits our output pipe would keep run() waiting.
        try:  # only a server that did start counts: otherwise the next operation tries again
            self._server_started = run_quiet(self.argv + list(self.server), 20) == 0
        except Stopped:  # the device was revoked meanwhile: no phone command after this one
            raise WebCoderError("STOPPED", "Stopped: this device was disabled or deleted in the hub panel") from None
        except (OSError, subprocess.SubprocessError):
            pass

    def call(self, args: list[str], serial: str | None = None, *, timeout: float = 30.0, check: bool = True,
             whole: int | None = None, tail: bool = False) -> bytes:
        """Run adb / hdc. Text output is capped at MAX_OUTPUT (its start, or with tail=True its last whole
        lines); with `whole` (a byte limit) the output must arrive complete (e.g. an image), and anything
        larger is an error instead of being cut."""
        if not self._server_started:
            self._start_server()
        result = run(self.argv + (self.target(serial) if serial else []) + args, timeout,
                     MAX_OUTPUT if whole is None else whole, tail)
        if check and result.returncode != 0:
            detail = _text(result.stderr or result.stdout).strip()[-600:]
            raise WebCoderError("MOBILE_COMMAND_FAILED", f"{self.tool} {' '.join(args[:3])} failed: "
                                f"{detail or f'exit code {result.returncode}'}")
        if whole is not None and result.stdout_cut:
            raise WebCoderError("MOBILE_COMMAND_FAILED", f"{self.tool} returned {result.stdout_size:,} bytes, over "
                                f"the {whole:,} byte limit")
        if tail and result.stdout_cut:  # starts mid-line
            return result.stdout.partition(b"\n")[2]
        return result.stdout

    def exists(self, serial: str, remote: str) -> bool:
        out = self.text(["shell", exists_script(remote)], serial)
        if "WEBCODER_EXISTS" in out:
            return True
        if "WEBCODER_MISSING" in out:
            return False
        raise WebCoderError("MOBILE_COMMAND_FAILED", f"Could not check {remote} on the phone: {out.strip()[-300:]}")

    def text(self, args: list[str], serial: str | None = None, **kwargs) -> str:
        return _text(self.call(args, serial, **kwargs))

    def run_shell(self, serial: str, command: str, timeout: float) -> subprocess.CompletedProcess:
        """The phone's own shell runs `command` (adb shell / hdc shell take it as one argument)."""
        if not self._server_started:
            self._start_server()
        return run(self.argv + self.target(serial) + ["shell", command], timeout, MAX_OUTPUT, tail=True)  # its end

    def push_new(self, serial: str, local: str, remote: str) -> None:
        """Push without replacing anything. adb / hdc have no such write, so the file goes to a temporary name
        next to `remote` and is renamed into place in one step on the phone, only if `remote` (a file or a
        symlink) still does not exist: nothing can slip in during the transfer, and the file appears whole."""
        temp = f"{remote}.webcoder-{secrets.token_hex(4)}.tmp"
        try:
            self.push(serial, local, temp)
            out = self.text(["shell", move_into_place_script(temp, remote)], serial)
        except WebCoderError:
            self._discard(serial, temp)
            raise
        if "WEBCODER_MOVED" in out:
            return
        self._discard(serial, temp)
        if "WEBCODER_EXISTS" in out:
            raise WebCoderError("DESTINATION_EXISTS", f"{remote} appeared on the phone during the transfer; it was "
                                "left alone (choose another path, or pass overwrite=true to replace it)")
        raise WebCoderError("MOBILE_COMMAND_FAILED", f"Could not move the file into place: {out.strip()[-300:]}")

    def _discard(self, serial: str, remote: str) -> None:
        with contextlib.suppress(WebCoderError):
            self.call(["shell", f"rm -f {_shell_quote(remote)}"], serial, check=False)

    @staticmethod
    def _scratch(suffix: str) -> str:
        """A file on the phone for one capture: two at once never read each other's (removed afterwards)."""
        return f"/data/local/tmp/webcoder-{secrets.token_hex(4)}{suffix}"


class AdbBackend(Backend):
    platform, tool, server = "android", "adb", ("start-server",)
    KEYS = {"back": "4", "home": "3", "enter": "66", "delete": "67", "tab": "61", "space": "62", "escape": "111",
            "menu": "82", "recent_apps": "187", "power": "26", "volume_up": "24", "volume_down": "25", "search": "84"}

    def target(self, serial: str) -> list[str]:
        return ["-s", serial]

    def devices(self, details: bool = True) -> list[dict]:
        items = []
        for line in self.text(["devices", "-l"]).splitlines()[1:]:
            parts = line.split()
            if len(parts) < 2 or line.startswith("*"):
                continue
            details = dict(part.split(":", 1) for part in parts[2:] if ":" in part)
            items.append({"serial": parts[0], "platform": self.platform,
                          "state": READY if parts[1] == "device" else parts[1],
                          "model": details.get("model", "").replace("_", " ")})
        return items

    def ui(self, serial: str) -> tuple[list[dict], list[int] | None]:
        dump = self._scratch(".xml")
        try:
            self.call(["shell", "uiautomator", "dump", dump], serial, timeout=40)
            return parse_android_ui(self.call(["exec-out", "cat", dump], serial))
        finally:
            self._discard(serial, dump)

    def tap(self, serial: str, x: int, y: int, kind: str) -> None:
        if kind == "long_press":
            self.call(["shell", "input", "swipe", str(x), str(y), str(x), str(y), "800"], serial)
            return
        for _ in range(2 if kind == "double_tap" else 1):
            self.call(["shell", "input", "tap", str(x), str(y)], serial)

    def swipe(self, serial: str, points: list[int], duration_ms: int) -> None:
        self.call(["shell", "input", "swipe", *map(str, points), str(duration_ms)], serial)

    def type_text(self, serial: str, text: str) -> None:
        if not all(32 <= ord(ch) < 127 for ch in text):
            raise WebCoderError("MOBILE_UNSUPPORTED", "adb can only type printable ASCII; for other text (e.g. "
                                "Chinese) install an input-method app such as ADB Keyboard, or paste instead")
        # `input text` reads every %s as a space and has no escape for it, so a literal "%s" is typed in two
        # goes ("…%", then "s…"). Spaces become %s; the phone's shell sees one single-quoted word.
        for part in re.split(r"(?<=%)(?=s)", text):
            self.call(["shell", "input", "text", _shell_quote(part.replace(" ", "%s"))], serial)

    def key(self, serial: str, key: str) -> None:
        self.call(["shell", "input", "keyevent", key if key.isdigit() else self.KEYS[key]], serial)

    def screenshot(self, serial: str) -> tuple[bytes, str]:
        data = self.call(["exec-out", "screencap", "-p"], serial, timeout=40, whole=MAX_SCREENSHOT)
        if not data.startswith(b"\x89PNG"):
            raise WebCoderError("MOBILE_COMMAND_FAILED", "The phone did not return a PNG screenshot")
        return data, "png"

    def logs(self, serial: str, lines: int) -> str:
        return self.text(["logcat", "-d", "-t", str(lines)], serial, timeout=40)

    def launch(self, serial: str, package: str, component: str | None) -> str:
        if component:
            return self.text(["shell", "am", "start", "-n", f"{package}/{component}"], serial)
        out = self.text(["shell", "monkey", "-p", package, "-c", "android.intent.category.LAUNCHER", "1"], serial)
        if "No activities found" in out:
            raise WebCoderError("MOBILE_COMMAND_FAILED", f"{package} has no launchable activity (is it installed?)")
        return out

    def stop(self, serial: str, package: str) -> str:
        return self.text(["shell", "am", "force-stop", package], serial)

    def install(self, serial: str, local: str) -> str:
        return self._success(self.text(["install", "-r", local], serial, timeout=300, check=False), "install")

    def uninstall(self, serial: str, package: str) -> str:
        return self._success(self.text(["uninstall", package], serial, timeout=120, check=False), "uninstall")

    @staticmethod
    def _success(out: str, what: str) -> str:
        if "Success" not in out:
            raise WebCoderError("MOBILE_COMMAND_FAILED", f"adb {what} failed: {out.strip()[-600:]}")
        return out

    def push(self, serial: str, local: str, remote: str) -> None:
        self.call(["push", local, remote], serial, timeout=600)

    def pull(self, serial: str, remote: str, local: str) -> None:
        self.call(["pull", remote, local], serial, timeout=600)

    def packages(self, serial: str) -> list[str]:
        out = self.text(["shell", "pm", "list", "packages"], serial)
        return sorted(line.split(":", 1)[1].strip() for line in out.splitlines() if line.startswith("package:"))

    def current_app(self, serial: str) -> list[str]:
        out = self.text(["shell", "dumpsys", "activity", "activities"], serial)
        return list(dict.fromkeys(line.strip() for line in out.splitlines() if "ResumedActivity" in line))[:5]

    def properties(self, serial: str) -> dict:
        wanted = {"ro.product.manufacturer": "manufacturer", "ro.product.model": "model",
                  "ro.build.version.release": "android_version", "ro.build.version.sdk": "sdk",
                  "ro.product.cpu.abi": "abi"}
        props = {}
        for line in self.text(["shell", "getprop"], serial).splitlines():
            match = re.match(r"^\[([^\]]+)\]: \[(.*)\]$", line.strip())
            if match and match.group(1) in wanted:
                props[wanted[match.group(1)]] = match.group(2)
        props["screen"] = self.text(["shell", "wm", "size"], serial).strip().rsplit(":", 1)[-1].strip()
        return props


class HdcBackend(Backend):
    platform, tool, server = "harmony", "hdc", ("start",)
    KEYS = {"back": "Back", "home": "Home", "enter": "2054", "delete": "2055", "tab": "2049", "space": "2050",
            "escape": "2070", "power": "18", "volume_up": "16", "volume_down": "17"}

    def target(self, serial: str) -> list[str]:
        return ["-t", serial]

    def devices(self, details: bool = True) -> list[dict]:
        items = []
        for line in self.text(["list", "targets", "-v"]).splitlines():
            parts = line.split()
            if not parts or parts[0].startswith("["):  # "[Empty]" when nothing is attached
                continue
            # -v adds "USB/TCP  Connected/Offline ..." columns; a bare key is a connected target
            state = READY if "Connected" in parts or len(parts) == 1 else (
                parts[2].lower() if len(parts) > 2 else "unknown")
            model = ""
            if details and state == READY and SERIAL_RE.match(parts[0]):
                with contextlib.suppress(WebCoderError):
                    model = self.text(["shell", "param", "get", "const.product.model"], parts[0], timeout=10).strip()
            items.append({"serial": parts[0], "platform": self.platform, "state": state, "model": model})
        return items

    def _fetch(self, serial: str, remote: str, limit: int = MAX_OUTPUT) -> bytes:
        with tempfile.TemporaryDirectory(prefix="webcoder-") as folder:
            local = os.path.join(folder, os.path.basename(remote))
            self._transfer(["recv", remote, local], serial, 60)
            if not os.path.isfile(local):
                raise WebCoderError("MOBILE_COMMAND_FAILED", f"hdc could not copy {remote} from the phone")
            if os.path.getsize(local) > limit:  # never hand back a cut-off file
                raise WebCoderError("MOBILE_COMMAND_FAILED", f"{remote} is over the {limit:,} byte limit")
            with open(local, "rb") as handle:
                return handle.read()

    def ui(self, serial: str) -> tuple[list[dict], list[int] | None]:
        dump = self._scratch(".json")
        try:
            self.call(["shell", "uitest", "dumpLayout", "-p", dump], serial, timeout=40)
            return parse_harmony_ui(self._fetch(serial, dump))
        finally:
            self._discard(serial, dump)

    def tap(self, serial: str, x: int, y: int, kind: str) -> None:
        action = {"tap": "click", "double_tap": "doubleClick", "long_press": "longClick"}[kind]
        self.call(["shell", "uitest", "uiInput", action, str(x), str(y)], serial)

    def swipe(self, serial: str, points: list[int], duration_ms: int) -> None:
        distance = ((points[2] - points[0]) ** 2 + (points[3] - points[1]) ** 2) ** 0.5
        speed = max(200, min(40000, int(distance * 1000 / duration_ms)))  # uitest wants pixels per second
        self.call(["shell", "uitest", "uiInput", "swipe", *map(str, points), str(speed)], serial)

    def type_text(self, serial: str, text: str) -> None:
        self.call(["shell", "uitest", "uiInput", "text", _shell_quote(text)], serial)  # types at the focused field

    def key(self, serial: str, key: str) -> None:
        self.call(["shell", "uitest", "uiInput", "keyEvent", key if key.isdigit() else self.KEYS[key]], serial)

    def screenshot(self, serial: str) -> tuple[bytes, str]:
        remote = self._scratch(".jpeg")
        try:
            self.call(["shell", "snapshot_display", "-f", remote], serial, timeout=40)
            data = self._fetch(serial, remote, MAX_SCREENSHOT)
        finally:
            self._discard(serial, remote)
        if not data.startswith(b"\xff\xd8"):
            raise WebCoderError("MOBILE_COMMAND_FAILED", "The phone did not return a JPEG screenshot")
        return data, "jpeg"

    def logs(self, serial: str, lines: int) -> str:
        # -x: print the buffer and exit. The newest lines are at the end: keep the end if it is too long.
        out = self.text(["shell", "hilog", "-x"], serial, timeout=40, tail=True)
        return "\n".join(out.splitlines()[-lines:])

    def launch(self, serial: str, package: str, component: str | None) -> str:
        return self._success(self.text(["shell", "aa", "start", "-a", component or "EntryAbility", "-b", package],
                                       serial), "start")

    def stop(self, serial: str, package: str) -> str:
        return self.text(["shell", "aa", "force-stop", package], serial)

    def install(self, serial: str, local: str) -> str:
        return self._success(self.text(["install", "-r", local], serial, timeout=300, check=False), "install")

    def uninstall(self, serial: str, package: str) -> str:
        return self._success(self.text(["uninstall", package], serial, timeout=120, check=False), "uninstall")

    @staticmethod
    def _success(out: str, what: str) -> str:
        if "success" not in out.lower():  # hdc exits 0 even when the phone refuses
            raise WebCoderError("MOBILE_COMMAND_FAILED", f"hdc {what} failed: {out.strip()[-600:]}")
        return out

    def _transfer(self, args: list[str], serial: str, timeout: float) -> None:
        out = self.text(["file", *args], serial, timeout=timeout)
        if "[fail]" in out.lower():  # hdc exits 0 even when the transfer failed
            raise WebCoderError("MOBILE_COMMAND_FAILED", f"hdc file {args[0]} failed: {out.strip()[-600:]}")

    def push(self, serial: str, local: str, remote: str) -> None:
        self._transfer(["send", local, remote], serial, 600)

    def pull(self, serial: str, remote: str, local: str) -> None:
        self._transfer(["recv", remote, local], serial, 600)

    def packages(self, serial: str) -> list[str]:
        out = self.text(["shell", "bm", "dump", "-a"], serial)
        return sorted({token for token in re.split(r"\s+", out) if PACKAGE_RE.match(token)})

    def current_app(self, serial: str) -> list[str]:
        out = self.text(["shell", "aa", "dump", "-l"], serial)
        keep = ("bundle name", "ability name", "state")
        return [line.strip() for line in out.splitlines() if any(key in line.lower() for key in keep)][:30]

    def properties(self, serial: str) -> dict:
        wanted = {"const.product.brand": "brand", "const.product.model": "model",
                  "const.product.software.version": "software_version", "const.ohos.apiversion": "api_version"}
        props = {}
        for key, name in wanted.items():
            with contextlib.suppress(WebCoderError):
                props[name] = self.text(["shell", "param", "get", key], serial, timeout=10).strip()
        return props


BACKENDS = (AdbBackend, HdcBackend)
KEY_NAMES = sorted(set(AdbBackend.KEYS) | set(HdcBackend.KEYS))


# --------------------------------------------------------------------------- operations
class MobileOps:
    """The mobile.* operations the hub may request (only when the owner enabled them on this computer)."""

    OPERATIONS = {"mobile.devices": "devices", "mobile.ui": "ui", "mobile.tap": "tap", "mobile.swipe": "swipe",
                  "mobile.type": "type_text", "mobile.key": "key", "mobile.screenshot": "screenshot",
                  "mobile.logs": "logs", "mobile.app": "app", "mobile.push": "push", "mobile.pull": "pull",
                  "mobile.info": "device_info", "mobile.shell": "shell"}
    READS = {"mobile.devices", "mobile.ui", "mobile.logs", "mobile.info"}  # what a read-only device still does

    def __init__(self, files, *, enabled: bool = False, tools: dict | None = None):
        self.files = files  # FileOps: the project sandbox for screenshots, pushes, pulls and installs
        self.enabled = enabled
        self.tools = tools or {}
        self._backends: list[Backend] | None = None

    def status(self) -> dict:
        return {"enabled": self.enabled,
                "tools": {cls.tool: locate(cls.tool, self.tools.get(cls.tool)) is not None for cls in BACKENDS}}

    def dispatch(self, op: str, args: dict) -> dict:
        if not self.enabled:
            raise WebCoderError("MOBILE_DISABLED", "Controlling phones is turned off on this computer; its owner can "
                                "turn it on in the WebCoder panel (设备 → 修改设置) or run: webcoder-agent mobile on")
        if op not in self.READS and self.files.sb.read_only:  # "the AI can look, not change": phones too
            raise WebCoderError("AGENT_READ_ONLY", "The agent on this device is configured read-only: phones can be "
                                "looked at (devices, screen layout, logs, info) but not operated, and nothing is "
                                "saved into projects")
        method = getattr(self, self.OPERATIONS[op])
        try:
            inspect.signature(method).bind(**args)
        except TypeError as exc:
            raise WebCoderError("INVALID_ARGUMENT", f"Invalid arguments for {op}: {exc}") from None
        return method(**args)

    def backends(self) -> list[Backend]:
        if self._backends is None:
            self._backends = [cls(argv) for cls in BACKENDS if (argv := locate(cls.tool, self.tools.get(cls.tool)))]
        if not self._backends:
            raise WebCoderError("MOBILE_TOOL_MISSING", "Neither adb (Android SDK platform-tools) nor hdc (DevEco "
                                "Studio / OpenHarmony SDK) was found on this computer")
        return self._backends

    def _target(self, serial=None) -> tuple[Backend, str]:
        if serial is not None and (not isinstance(serial, str) or not SERIAL_RE.match(serial)):
            raise WebCoderError("INVALID_ARGUMENT", "serial is not a valid device serial")
        ready = [(backend, item["serial"]) for backend in self.backends() for item in backend.devices(details=False)
                 if item["state"] == READY and serial in (None, item["serial"])]
        if not ready:
            which = f"with serial {serial}" if serial else "is ready"
            raise WebCoderError("MOBILE_NO_DEVICE", f"No connected phone or emulator {which} (check the USB cable, USB "
                                "debugging and the authorization prompt on the phone)")
        if len(ready) > 1:
            listing = ", ".join(f"{found} ({backend.platform})" for backend, found in ready)
            raise WebCoderError("MOBILE_DEVICE_AMBIGUOUS", f"Several phones are connected: {listing}; pass serial")
        return ready[0]

    def devices(self) -> dict:
        items, errors = [], []
        for backend in self.backends():
            try:
                items.extend(backend.devices())
            except WebCoderError as exc:
                errors.append(f"{backend.tool}: {exc.message}")
        return {"devices": items, "errors": errors, "tools": self.status()["tools"]}

    def ui(self, serial=None, max_nodes=150) -> dict:
        backend, found = self._target(serial)
        nodes, screen = backend.ui(found)
        limit = _int(max_nodes, "max_nodes", 10, 500)
        return {"serial": found, "platform": backend.platform, "screen": screen, "nodes": nodes[:limit],
                "total": len(nodes)}

    def tap(self, x, y, serial=None, kind="tap") -> dict:
        if kind not in ("tap", "double_tap", "long_press"):
            raise WebCoderError("INVALID_ARGUMENT", "kind must be tap, double_tap or long_press")
        backend, found = self._target(serial)
        backend.tap(found, _int(x, "x", 0, 20000), _int(y, "y", 0, 20000), kind)
        return {"serial": found, "platform": backend.platform}

    def swipe(self, x1, y1, x2, y2, serial=None, duration_ms=300) -> dict:
        points = [_int(value, name, 0, 20000) for value, name in ((x1, "x1"), (y1, "y1"), (x2, "x2"), (y2, "y2"))]
        backend, found = self._target(serial)
        backend.swipe(found, points, _int(duration_ms, "duration_ms", 50, 10000))
        return {"serial": found, "platform": backend.platform}

    def type_text(self, text, serial=None) -> dict:
        if not isinstance(text, str) or not text or len(text) > 2000 or any(ch in text for ch in "\x00\r\n"):
            raise WebCoderError("INVALID_ARGUMENT", "text must be 1-2000 characters on one line (press enter with "
                                "mobile_key)")
        backend, found = self._target(serial)
        backend.type_text(found, text)
        return {"serial": found, "platform": backend.platform}

    def key(self, key, serial=None) -> dict:
        if not isinstance(key, str) or not (key.isdigit() and len(key) <= 5 or key in KEY_NAMES):
            raise WebCoderError("INVALID_ARGUMENT", f"key must be one of {', '.join(KEY_NAMES)} or a numeric key code")
        backend, found = self._target(serial)
        if not key.isdigit() and key not in backend.KEYS:
            raise WebCoderError("MOBILE_UNSUPPORTED", f"'{key}' is not available on {backend.platform}")
        backend.key(found, key)
        return {"serial": found, "platform": backend.platform}

    def screenshot(self, root, path, serial=None) -> dict:
        backend, found = self._target(serial)
        extensions = {"png": (".png",), "jpeg": (".jpg", ".jpeg")}
        wanted = "png" if backend.platform == "android" else "jpeg"
        if not str(path).lower().endswith(extensions[wanted]):
            raise WebCoderError("INVALID_ARGUMENT", f"{backend.platform} screenshots are {wanted.upper()}: use a path "
                                f"ending in {' or '.join(extensions[wanted])}")
        data, _ = backend.screenshot(found)  # at most MAX_SCREENSHOT, which the file may then take
        return {"serial": found, "platform": backend.platform,
                **self.files.create_file(root, path, data, max_bytes=MAX_SCREENSHOT)}

    def logs(self, serial=None, lines=200, contains=None) -> dict:
        backend, found = self._target(serial)
        text = backend.logs(found, _int(lines, "lines", 1, 5000))
        if contains:
            needle = str(contains).lower()
            text = "\n".join(line for line in text.splitlines() if needle in line.lower())
        return {"serial": found, "platform": backend.platform, "text": text[-200_000:]}

    def app(self, action, package=None, component=None, root=None, path=None, serial=None) -> dict:
        backend, found = self._target(serial)
        if action == "install":
            if root is None or path is None:
                raise WebCoderError("INVALID_ARGUMENT", "install needs project and path of the package file")
            suffix = ".apk" if backend.platform == "android" else ".hap"
            local, size, display = self.files.local_file(root, path, MAX_LOCAL_FILE)
            if not local.lower().endswith(suffix):
                raise WebCoderError("INVALID_ARGUMENT", f"{backend.platform} installs {suffix} files")
            output = backend.install(found, local)
            return {"serial": found, "platform": backend.platform, "action": action, "path": display, "size": size,
                    "output": output.strip()[-2000:]}
        package = _package(package)
        if action == "launch":
            if component is not None and (not isinstance(component, str) or not COMPONENT_RE.match(component)):
                raise WebCoderError("INVALID_ARGUMENT", "component must be an activity / ability name")
            output = backend.launch(found, package, component)
        elif action == "stop":
            output = backend.stop(found, package)
        elif action == "uninstall":
            output = backend.uninstall(found, package)
        else:
            raise WebCoderError("INVALID_ARGUMENT", "action must be launch, stop, install or uninstall")
        return {"serial": found, "platform": backend.platform, "action": action, "package": package,
                "output": output.strip()[-2000:]}

    def push(self, root, path, device_path, serial=None, overwrite=False) -> dict:
        remote = _device_path(device_path)
        local, size, display = self.files.local_file(root, path, MAX_LOCAL_FILE)
        backend, found = self._target(serial)
        if overwrite is True:
            backend.push(found, local, remote)
        else:  # the phone's file has no backup here
            if backend.exists(found, remote):  # checked first so a large file is not sent for nothing
                raise WebCoderError("DESTINATION_EXISTS", f"{remote} already exists on the phone; choose another path, "
                                    "or pass overwrite=true to replace it")
            backend.push_new(found, local, remote)
        return {"serial": found, "platform": backend.platform, "path": display, "device_path": remote, "size": size,
                "overwrote": overwrite is True}

    def pull(self, root, path, device_path, serial=None) -> dict:
        remote = _device_path(device_path)
        backend, found = self._target(serial)
        with self.files.incoming_file(root, path, MAX_LOCAL_FILE) as incoming:
            backend.pull(found, remote, incoming.temp)
        return {"serial": found, "platform": backend.platform, "device_path": remote, **incoming.result}

    def shell(self, command, serial=None, timeout=60) -> dict:
        """Any command in the phone's shell (adb shell / hdc shell). The command goes to adb / hdc as one argument,
        never through this computer's shell; it is the phone's shell that runs it, as the owner asked."""
        if not isinstance(command, str) or not command.strip() or len(command) > 20_000 or "\x00" in command:
            raise WebCoderError("INVALID_ARGUMENT", "command must be a non-empty shell command for the phone")
        seconds = _int(timeout, "timeout", 1, 600)
        backend, found = self._target(serial)
        started = time.monotonic()
        done = backend.run_shell(found, command, seconds)
        stdout, stderr = _text(done.stdout), _text(done.stderr)
        return {"serial": found, "platform": backend.platform, "exit_code": done.returncode,
                "stdout": stdout[-200_000:], "stderr": stderr[-20_000:],
                "truncated": done.stdout_cut or len(stdout) > 200_000,
                "duration_ms": int((time.monotonic() - started) * 1000)}

    def device_info(self, kind, serial=None) -> dict:
        backend, found = self._target(serial)
        if kind == "packages":
            value = backend.packages(found)
        elif kind == "current_app":
            value = backend.current_app(found)
        elif kind == "properties":
            value = backend.properties(found)
        else:
            raise WebCoderError("INVALID_ARGUMENT", "kind must be packages, current_app or properties")
        return {"serial": found, "platform": backend.platform, "kind": kind, "value": value}
