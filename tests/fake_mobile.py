"""Stand-in for adb / hdc in tests: `python fake_mobile.py adb|hdc <args>`.

Logs every call to $WEBCODER_FAKE_MOBILE/calls.jsonl, keeps the "phone's" files under
$WEBCODER_FAKE_MOBILE/phone, and answers the commands the agent uses the way the real tools do.
"""
import json
import os
import re
import shlex
import shutil
import sys
from pathlib import Path

STATE = Path(os.environ["WEBCODER_FAKE_MOBILE"])
PHONE = STATE / "phone"
ANDROID_UI = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?><hierarchy rotation="0">
<node index="0" text="" resource-id="" class="android.widget.FrameLayout" content-desc="" checkable="false"
 checked="false" clickable="false" enabled="true" focused="false" scrollable="false" bounds="[0,0][1080,2400]">
<node index="0" text="登录" resource-id="com.demo:id/login" class="android.widget.Button" content-desc=""
 checkable="false" checked="false" clickable="true" enabled="true" focused="false" scrollable="false"
 bounds="[340,1600][740,1700]" />
<node index="1" text="" resource-id="com.demo:id/remember" class="android.widget.CheckBox" content-desc="记住我"
 checkable="true" checked="true" clickable="true" enabled="true" focused="false" scrollable="false"
 bounds="[100,1800][200,1900]" />
</node></hierarchy>"""
HARMONY_UI = {"attributes": {"bounds": "[0,0][1260,2720]", "type": "root"}, "children": [
    {"attributes": {"bounds": "[100,200][600,300]", "type": "Text", "text": "设置", "clickable": "false"},
     "children": []},
    {"attributes": {"bounds": "[100,400][1160,520]", "type": "Button", "text": "蓝牙", "id": "bt",
                    "clickable": "true", "enabled": "true"}, "children": []}]}
PNG = b"\x89PNG\r\n\x1a\n" + b"fake-png" * 8
JPEG = b"\xff\xd8\xff\xe0" + b"fake-jpeg" * 8


def scenario() -> dict:
    path = STATE / "scenario.json"
    defaults = {"adb": [["emulator-5554", "device", "Pixel_7"]], "hdc": [["FAKEHM001", "Connected"]]}
    return {**defaults, **(json.loads(path.read_text(encoding="utf-8")) if path.exists() else {})}


def on_phone(remote: str) -> Path:
    return PHONE / remote.lstrip("/")


def out(text: str) -> None:
    sys.stdout.buffer.write(text.encode("utf-8"))


CHECK = r"if \[ -e (?P<q>'.*') \] \|\| \[ -L (?P=q) \]; then echo WEBCODER_EXISTS; "
EXISTS = re.compile(CHECK + r"else echo WEBCODER_MISSING; fi")
MOVE = re.compile(CHECK + r"else mv (?P<t>'.*') (?P=q) && echo WEBCODER_MOVED; fi")
REMOVE = re.compile(r"rm -f (?P<t>'.*')")
TEMP = re.compile(r"\.webcoder-[0-9a-f]+\.tmp$")


def unquote(word: str) -> str:
    return shlex.split(word)[0]


def agent_script(shell_command: str) -> bool:
    """The agent's own steps (exists check, move into place, clean up), as the phone's shell would run them."""
    if match := EXISTS.fullmatch(shell_command):
        out("WEBCODER_EXISTS\n" if os.path.lexists(on_phone(unquote(match["q"]))) else "WEBCODER_MISSING\n")
    elif match := MOVE.fullmatch(shell_command):
        if os.path.lexists(on_phone(unquote(match["q"]))):
            out("WEBCODER_EXISTS\n")
        else:
            os.replace(on_phone(unquote(match["t"])), on_phone(unquote(match["q"])))
            out("WEBCODER_MOVED\n")
    elif match := REMOVE.fullmatch(shell_command):
        on_phone(unquote(match["t"])).unlink(missing_ok=True)
    else:
        return False
    return True


def phone_shell(command: str) -> int | None:
    """A few commands in the phone's own shell (mobile_shell): echo ..., fail <code>."""
    if command.startswith("echo "):
        out(command[5:] + "\n")
        return 0
    if command.startswith("fail "):
        sys.stderr.write("boom\n")
        return int(command.split()[1])
    return None


def receive(local: str, remote: str) -> None:
    """A push / file send landing on the phone."""
    on_phone(remote).parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(local, on_phone(remote))
    if scenario().get("racy_push") and TEMP.search(remote):  # meanwhile, someone creates the real file
        on_phone(TEMP.sub("", remote)).write_bytes(b"theirs\n")


def adb(args: list[str]) -> int:
    if args[:1] == ["start-server"]:
        return 1 if scenario().get("server_fails") else 0
    if args[:2] == ["devices", "-l"]:
        out("List of devices attached\n" + "".join(f"{serial}\t{state} product:sdk model:{model} transport_id:1\n"
                                                   for serial, state, model in scenario()["adb"]) + "\n")
        return 0
    if args[:1] == ["-s"]:
        args = args[2:]
    command = " ".join(args)
    if args[:1] == ["shell"] and len(args) == 2 and agent_script(args[1]):
        return 0
    if args[:1] == ["shell"] and len(args) == 2 and (code := phone_shell(args[1])) is not None:
        return code
    if command.startswith("shell uiautomator dump"):
        out("UI hierchary dumped to: /data/local/tmp/webcoder_ui.xml\n")
    elif command.startswith("exec-out cat"):
        out(ANDROID_UI)
    elif command == "exec-out screencap -p":
        sys.stdout.buffer.write(PNG)
    elif args[:2] == ["logcat", "-d"]:
        out("".join(f"09-23 10:00:{i:02d} I/Demo: line {i}\n" for i in range(int(args[3]))))
    elif command.startswith("shell monkey"):
        out("Events injected: 1\n")
    elif args[:1] == ["install"]:
        out("Performing Streamed Install\nSuccess\n")
    elif args[:1] == ["uninstall"]:
        out("Success\n")
    elif args[:1] == ["push"]:
        receive(args[1], args[2])
        out(f"{args[1]}: 1 file pushed.\n")
    elif args[:1] == ["pull"]:
        if not on_phone(args[1]).exists():
            sys.stderr.write(f"adb: error: failed to stat remote object '{args[1]}': No such file or directory\n")
            return 1
        shutil.copyfile(on_phone(args[1]), args[2])
        out(f"{args[1]}: 1 file pulled.\n")
    elif command == "shell pm list packages":
        out("package:com.demo\npackage:com.android.settings\n")
    elif command == "shell dumpsys activity activities":
        out("  mResumedActivity: ActivityRecord{1 u0 com.demo/.MainActivity t12}\n")
    elif command == "shell getprop":
        out("[ro.product.model]: [Pixel 7]\n[ro.build.version.release]: [15]\n[ro.build.version.sdk]: [35]\n")
    elif command == "shell wm size":
        out("Physical size: 1080x2400\n")
    return 0


def hdc(args: list[str]) -> int:
    if args[:1] == ["start"]:
        return 0
    if args[:3] == ["list", "targets", "-v"]:
        out("".join(f"{key}\tUSB\t{state}\tlocalhost\thdc\n" for key, state in scenario()["hdc"]) or "[Empty]\n")
        return 0
    if args[:1] == ["-t"]:
        args = args[2:]
    command = " ".join(args)
    if args[:1] == ["shell"] and len(args) == 2 and agent_script(args[1]):
        return 0
    if args[:1] == ["shell"] and len(args) == 2 and (code := phone_shell(args[1])) is not None:
        return code
    if command.startswith("shell param get"):
        out({"const.product.model": "Mate 60", "const.ohos.apiversion": "12"}.get(args[3], "") + "\n")
    elif command.startswith("shell uitest dumpLayout -p"):
        on_phone(args[4]).parent.mkdir(parents=True, exist_ok=True)
        on_phone(args[4]).write_text(json.dumps(HARMONY_UI, ensure_ascii=False), encoding="utf-8")
        out(f"DumpLayout saved to:{args[4]}\n")
    elif command.startswith("shell snapshot_display -f"):
        on_phone(args[3]).parent.mkdir(parents=True, exist_ok=True)
        on_phone(args[3]).write_bytes(JPEG)
    elif args[:2] == ["file", "recv"]:
        if on_phone(args[2]).exists():
            shutil.copyfile(on_phone(args[2]), args[3])
            out("FileTransfer finish, Size:1, File count = 1, time:1ms rate:1.00kB/s\n")
        else:
            out("[Fail]Error opening file: no such file or directory, path:" + args[2] + "\n")  # hdc exits 0 anyway
    elif args[:2] == ["file", "send"]:
        receive(args[2], args[3])
        out("FileTransfer finish\n")
    elif command == "shell hilog -x":
        out("".join(f"09-23 10:00:{i:02d} I A00001/Demo: line {i}\n" for i in range(50)))
    elif command.startswith("shell aa start"):
        out("start ability successfully.\n")
    elif command.startswith("shell aa force-stop"):
        out("force stop process successfully.\n")
    elif args[:1] == ["install"]:
        out("[Info]App install path:x msg:install bundle successfully.\n")
    elif args[:1] == ["uninstall"]:
        out("[Info]App uninstall path: msg:uninstall bundle successfully.\n")
    elif command == "shell bm dump -a":
        out("ID: 100:\n\tcom.example.demo\n\tcom.huawei.hmos.settings\n")
    elif command == "shell aa dump -l":
        out("  bundle name [com.example.demo]\n  ability name [EntryAbility]\n  state #FOREGROUND\n")
    return 0


if __name__ == "__main__":
    tool, rest = sys.argv[1], sys.argv[2:]
    STATE.mkdir(parents=True, exist_ok=True)
    with open(STATE / "calls.jsonl", "a", encoding="utf-8") as log:
        log.write(json.dumps([tool, *rest], ensure_ascii=False) + "\n")
    sys.exit(adb(rest) if tool == "adb" else hdc(rest))
