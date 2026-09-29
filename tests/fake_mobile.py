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
    serial = args[1] if args[:1] == ["-s"] else ""
    if args[:1] == ["-s"]:
        args = args[2:]
    command = " ".join(args)
    if command == "shell cat /proc/net/unix":
        out("Num RefCount Protocol Flags Type St Inode Path\n" + "".join(
            f"00000000: 00000002 00000000 00010000 0001 01 200 @{name}\n"
            for name in scenario().get("cdp_sockets", [])))
        return 0
    if args[:1] == ["forward"] and "--list" not in args and "--remove" not in args and scenario().get(
            "adb_fail_forward"):
        sys.stderr.write("adb: error: cannot bind listener: Address already in use\n")
        return 1
    if args[:1] == ["forward"]:
        table = STATE / "forwards.json"
        mappings = json.loads(table.read_text()) if table.exists() else {}
        if "--list" in args:
            out("".join(f"{owner} {local} {remote}\n" for local, (owner, remote) in mappings.items()))
        elif "--remove" in args:
            mappings.pop(args[-1], None)
            table.write_text(json.dumps(mappings))
        else:
            mappings[args[-2]] = [serial, args[-1]]
            table.write_text(json.dumps(mappings))
        return 0
    if args[:1] == ["reverse"]:
        table = STATE / "reverse.json"
        mappings = json.loads(table.read_text()) if table.exists() else {}
        if "--list" in args:
            out("".join(f"UsbFfs {phone} {computer}\n" for phone, computer in mappings.items()))
        elif "--remove" in args:
            mappings.pop(args[-1], None)
            table.write_text(json.dumps(mappings))
        else:
            mappings[args[-2]] = args[-1]
            table.write_text(json.dumps(mappings))
        return 0
    if args[:1] == ["disconnect"] and args[1].startswith("192.168.1.99"):
        out(f"error: no such device '{args[1]}'\n")
        return 1
    if args[:1] in (["connect"], ["pair"], ["disconnect"]):
        out({"connect": f"connected to {args[1]}\n", "pair": f"Successfully paired to {args[1]} [guid=x]\n",
             "disconnect": f"disconnected {args[1]}\n"}[args[0]])
        return 0
    if command == "shell ps -A":
        out("USER PID PPID VSZ RSS WCHAN ADDR S NAME\nu0_a7 4321 1 1 1 0 0 S com.demo\n"
            "u0_a7 4322 1 1 1 0 0 S com.demo:remote\nu0_a8 999 1 1 1 0 0 S com.other\n")
        return 0
    if args[:2] == ["shell", "pidof"]:
        if args[2] == "com.demo":
            out("4321\n")
            return 0
        return 1
    if args[:2] == ["logcat", "-d"] and "crash" in args:
        out("10-01 10:00:00.000  4321  4321 E AndroidRuntime: FATAL EXCEPTION: main\n"
            "10-01 10:00:00.000  4321  4321 E AndroidRuntime: Process: com.demo, PID: 4321\n"
            "10-01 10:00:00.000  4321  4321 E AndroidRuntime: java.lang.NullPointerException\n"
            "10-01 10:00:01.000   999   999 E AndroidRuntime: Process: com.other, PID: 999\n"
            # native: crash_dump (7777) writes the report about the app's process 5555
            "10-01 10:00:02.000  7777  7777 F DEBUG   : *** *** *** *** *** *** *** *** *** *** *** ***\n"
            "10-01 10:00:02.000  7777  7777 F DEBUG   : pid: 5555, tid: 5555, name: Render  >>> com.demo:gpu <<<\n"
            "10-01 10:00:02.000  7777  7777 F DEBUG   : signal 11 (SIGSEGV), fault addr 0x0\n"
            "10-01 10:00:03.000  8888  8888 F DEBUG   : pid: 6666, tid: 6666, name: main  >>> com.other <<<\n"
            "10-01 10:00:03.000  8888  8888 F DEBUG   : signal 6 (SIGABRT) in com.other\n"
            # an old resident debuggerd (9999) prints every native crash: one of com.other, then one of com.demo
            "10-01 10:00:04.000  9999  9999 F DEBUG   : *** *** *** *** *** *** *** *** *** *** *** ***\n"
            "10-01 10:00:04.000  9999  9999 F DEBUG   : pid: 1212, tid: 1212, name: x  >>> com.other <<<\n"
            "10-01 10:00:04.000  9999  9999 F DEBUG   : backtrace of com.other OTHER_FRAME\n"
            "10-01 10:00:05.000  9999  9999 F DEBUG   : *** *** *** *** *** *** *** *** *** *** *** ***\n"
            "10-01 10:00:05.000  9999  9999 F DEBUG   : pid: 1313, tid: 1313, name: y  >>> com.demo <<<\n"
            "10-01 10:00:05.000  9999  9999 F DEBUG   : backtrace of com.demo DEMO_FRAME\n")
        return 0
    if command == "shell pm clear com.demo":
        out("Success\n")
        return 0
    if command.startswith("shell am start -W -a android.intent.action.VIEW"):
        crashing = "crash.example" in command  # am waits, the target crashes: no "Error", Status is not ok
        out("Starting: Intent { act=android.intent.action.VIEW }\n"
            + ("Status: crash\nLaunchState: UNKNOWN (-1)\n" if crashing else "Status: ok\n"))
        return 0
    if args[:3] == ["shell", "dumpsys", "package"]:
        out("Packages:\n  Package [com.demo] (abc):\n    versionCode=42 minSdk=24 targetSdk=35\n    "
            "versionName=1.2.3\n    flags=[ DEBUGGABLE HAS_CODE ]\n    firstInstallTime=2026-09-01 10:00:00\n"
            if args[3] == "com.demo" else f"Unable to find package: {args[3]}\n")
        return 0
    if args[:3] == ["shell", "dumpsys", "meminfo"]:
        out(" App Summary\n   Java Heap:  1234\n  TOTAL PSS:  9999\n" if args[3] == "com.demo"
            else f"No process found for: {args[3]}\n")
        return 0
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
        count = int(args[args.index("-t") + 1]) if "-t" in args else 300  # filtered reads take the whole buffer
        out("".join(f"09-23 10:00:{i % 60:02d}.000  {(4321, 4322, 999)[i % 3]}  1 I Demo: line {i}\n"
                    for i in range(count)))
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
    serial = args[1] if args[:1] == ["-t"] else ""
    if args[:1] == ["-t"]:
        args = args[2:]
    command = " ".join(args)
    if args[:1] in (["fport"], ["rport"]):
        table = STATE / "hdc_ports.json"
        tasks = json.loads(table.read_text()) if table.exists() else []
        if args[1:2] == ["ls"]:
            out("".join(f"{owner} {a} {b} [{kind}]\n" for owner, a, b, kind in tasks) or "[Empty]\n")
        elif args[1:2] == ["rm"]:
            if scenario().get("hdc_rm_sticks"):  # hdc exits 0 and the task stays
                out("Remove forward ruler failed, ruler is not exist " + " ".join(args[2:4]) + "\n")
            else:
                tasks = [task for task in tasks if task[1:3] != args[2:4]]
                out("Remove forward ruler success, ruler:" + " ".join(args[2:4]) + "\n")
        else:
            tasks.append([serial, args[1], args[2], "Forward" if args[0] == "fport" else "Reverse"])
            out("Forwardport result:OK\n")
        table.write_text(json.dumps(tasks))
        return 0
    if args[:1] == ["tconn"]:
        if args[1].startswith("10.0.0.99"):
            out("Connect failed\n")
            return 0
        out("Remove OK\n" if "-remove" in args else "Connect OK\n")
        return 0
    if command == "shell ps -ef":
        out("UID PID PPID C STIME TTY TIME CMD\n20010043 777 1 0 10:00 ? 00:00:01 com.example.demo\n")
        return 0
    if args[:2] == ["shell", "pidof"]:
        if args[2] == "com.example.demo":
            out("777\n")
            return 0
        return 1
    if command.startswith("shell hilog -x"):
        out("".join(f"09-23 10:00:{i:02d} I A00001/Demo: line {i}\n" for i in range(50)))
        return 0
    if command == "shell hilog -r":
        out("Log type core,app buffer clear successfully\n")
        return 0
    if args[:1] == ["shell"] and len(args) == 2 and "/data/log/faultlog/faultlogger" in args[1]:
        state = scenario().get("faultlog", "listed")  # the phone's shell running the agent's marker script
        out({"missing": "WEBCODER_NOFAULTLOG\n", "no_parent": "WEBCODER_NOFAULTLOG\n",
             "denied": "ls: /data/log/faultlog/faultlogger: Permission denied\nWEBCODER_LSFAILED\n",
             "listed": "jscrash-com.example.demo-20010043-20261001100000.log\n"
                       "cppcrash-com.other-1-20261001090000.log\nWEBCODER_LISTED\n"}[state])
        return 0
    if args[:2] == ["shell", "cat"] and args[2].startswith("/data/log/faultlog/faultlogger/"):
        out(f"Reason:TypeError\nModule name:{args[2].rsplit('/', 1)[-1]}\nStacktrace:\n  at onClick\n")
        return 0
    if command.startswith("shell bm clean -n"):
        out("clean bundle data files successfully.\n" if args[4] == "com.example.demo"
            else "clean bundle data files unsuccessfully.\n")
        return 0
    if command.startswith("shell bm dump -n"):
        out('com.example.demo:\n{"applicationInfo":{"versionName":"2.0.1","versionCode":2000001,"debug":true,'
            '"codePath":"/data/app/el1/bundle/public/com.example.demo"}}\n' if args[4] == "com.example.demo"
            else "error: failed to get information and the parameters may be wrong.\n")
        return 0
    if command.startswith("shell hidumper --mem"):
        out("Total Pss: 12345 kB\n")
        return 0
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
