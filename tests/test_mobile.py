"""Phone control (adb / hdc) against a fake adb / hdc: agent operations, boundaries, and the MCP tools."""
import base64
import json
import sys
from pathlib import Path

import pytest

from tests.conftest import AgentThread
from webcoder.agent import mobile as mobile_module
from webcoder.agent.__main__ import main as agent_main
from webcoder.agent.__main__ import pair_device
from webcoder.agent.config import AgentConfig
from webcoder.agent.fileops import BackupStore, FileOps, Limits, Sandbox
from webcoder.agent.mobile import MobileOps
from webcoder.common.errors import WebCoderError
from webcoder.hub.install import install_commands, roots_command

FAKE = str(Path(__file__).with_name("fake_mobile.py"))
FAKE_TOOLS = {"adb": [sys.executable, FAKE, "adb"], "hdc": [sys.executable, FAKE, "hdc"]}


@pytest.fixture
def phones(tmp_path, monkeypatch):
    state = tmp_path / "fake-mobile"
    state.mkdir()
    monkeypatch.setenv("WEBCODER_FAKE_MOBILE", str(state))

    def calls() -> list[list[str]]:
        path = state / "calls.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []

    def only(**lists):
        (state / "scenario.json").write_text(json.dumps(lists), encoding="utf-8")

    return type("Phones", (), {"state": state, "calls": staticmethod(calls), "only": staticmethod(only),
                               "phone": state / "phone"})


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    return root


@pytest.fixture
def ops(project, tmp_path):
    files = FileOps(Sandbox([project]), BackupStore(tmp_path / "backups"))
    files.mobile = MobileOps(files, enabled=True, tools=FAKE_TOOLS)
    return files


def code(excinfo) -> str:
    return excinfo.value.code


def test_off_unless_the_owner_turned_it_on(project, tmp_path, phones):
    files = FileOps(Sandbox([project]), BackupStore(tmp_path / "backups"))
    files.mobile = MobileOps(files, enabled=False, tools=FAKE_TOOLS)
    with pytest.raises(WebCoderError) as err:
        files.dispatch("mobile.devices", {})
    assert code(err) == "MOBILE_DISABLED" and phones.calls() == []


def test_a_read_only_device_only_looks_at_phones(project, tmp_path, phones):
    """"The AI can look, not change": on a read-only device a phone is listed and read, never operated."""
    files = FileOps(Sandbox([project], read_only=True), BackupStore(tmp_path / "backups"))
    files.mobile = MobileOps(files, enabled=True, tools=FAKE_TOOLS)
    for op, args in (("mobile.tap", {"x": 1, "y": 2, "serial": "emulator-5554"}),
                     ("mobile.type", {"text": "hi", "serial": "emulator-5554"}),
                     ("mobile.key", {"key": "home", "serial": "emulator-5554"}),
                     ("mobile.shell", {"command": "rm -rf /sdcard/x", "serial": "emulator-5554"}),
                     ("mobile.app", {"action": "uninstall", "package": "com.example", "serial": "emulator-5554"}),
                     ("mobile.push", {"root": str(project), "path": "a.txt", "device_path": "/sdcard/a.txt",
                                      "serial": "emulator-5554"}),
                     ("mobile.screenshot", {"root": str(project), "path": "s.png", "serial": "emulator-5554"})):
        with pytest.raises(WebCoderError) as err:
            files.dispatch(op, args)
        assert code(err) == "AGENT_READ_ONLY", op
    assert phones.calls() == []  # refused before anything ran
    assert {d["serial"] for d in files.dispatch("mobile.devices", {})["devices"]} >= {"emulator-5554"}
    assert "com.demo" in files.dispatch("mobile.info", {"kind": "packages", "serial": "emulator-5554"})["value"]


def test_missing_tools_are_reported(project, tmp_path, monkeypatch):
    monkeypatch.setattr(mobile_module, "_candidates", lambda tool: [])
    files = FileOps(Sandbox([project]), BackupStore(tmp_path / "backups"))
    files.mobile = MobileOps(files, enabled=True)
    assert files.info()["mobile"] == {"enabled": True, "tools": {"adb": False, "hdc": False}}
    with pytest.raises(WebCoderError) as err:
        files.dispatch("mobile.devices", {})
    assert code(err) == "MOBILE_TOOL_MISSING"


def test_devices_and_picking_a_phone(ops, phones):
    listed = ops.dispatch("mobile.devices", {})["devices"]
    assert {(d["serial"], d["platform"], d["state"], d["model"]) for d in listed} == {
        ("emulator-5554", "android", "ready", "Pixel 7"), ("FAKEHM001", "harmony", "ready", "Mate 60")}
    with pytest.raises(WebCoderError) as err:
        ops.dispatch("mobile.tap", {"x": 1, "y": 2})  # two phones, no serial
    assert code(err) == "MOBILE_DEVICE_AMBIGUOUS"
    for bad in ("emulator-5554; reboot", "../x", ""):
        with pytest.raises(WebCoderError) as err:
            ops.dispatch("mobile.tap", {"x": 1, "y": 2, "serial": bad})
        assert code(err) == "INVALID_ARGUMENT"
    phones.only(adb=[["emulator-5554", "unauthorized", ""]], hdc=[])
    with pytest.raises(WebCoderError) as err:
        ops.dispatch("mobile.tap", {"x": 1, "y": 2})
    assert code(err) == "MOBILE_NO_DEVICE"


def test_android_screen_input_and_keys(ops, phones):
    phones.only(hdc=[])
    ui = ops.dispatch("mobile.ui", {})
    assert ui["platform"] == "android" and ui["screen"] == [1080, 2400]
    by_id = {node["id"]: node for node in ui["nodes"]}
    assert by_id["login"]["text"] == "登录" and by_id["login"]["center"] == [540, 1650] and by_id["login"]["clickable"]
    assert by_id["remember"]["checked"] is True and by_id["remember"]["desc"] == "记住我"
    ops.dispatch("mobile.tap", {"x": 540, "y": 1650})
    ops.dispatch("mobile.tap", {"x": 5, "y": 6, "kind": "long_press"})
    ops.dispatch("mobile.swipe", {"x1": 500, "y1": 2000, "x2": 500, "y2": 600, "duration_ms": 250})
    ops.dispatch("mobile.type", {"text": "it's fine; reboot"})
    ops.dispatch("mobile.type", {"text": "50%s off %"})
    ops.dispatch("mobile.key", {"key": "back"})
    sent = [call[3:] for call in phones.calls() if call[:4] == ["adb", "-s", "emulator-5554", "shell"]]
    assert ["shell", "input", "tap", "540", "1650"] in sent
    assert ["shell", "input", "swipe", "5", "6", "5", "6", "800"] in sent
    assert ["shell", "input", "swipe", "500", "2000", "500", "600", "250"] in sent
    # one single-quoted word for the phone's shell: the ";" cannot start a second command
    assert ["shell", "input", "text", "'it'\\''s%sfine;%sreboot'"] in sent
    # `input text` would turn a literal "%s" into a space: it goes in two pieces, split between % and s
    typed = [call[3] for call in sent if call[1:3] == ["input", "text"]][-2:]
    assert typed == ["'50%'", "'s%soff%s%'"]
    assert ["shell", "input", "keyevent", "4"] in sent
    with pytest.raises(WebCoderError) as err:
        ops.dispatch("mobile.type", {"text": "你好"})
    assert code(err) == "MOBILE_UNSUPPORTED"
    for bad in ({"key": "reboot"}, {"key": "1; reboot"}):
        with pytest.raises(WebCoderError) as err:
            ops.dispatch("mobile.key", bad)
        assert code(err) == "INVALID_ARGUMENT"


def test_harmony_screen_input_and_keys(ops, phones):
    phones.only(adb=[])
    ui = ops.dispatch("mobile.ui", {})
    assert ui["platform"] == "harmony" and ui["screen"] == [1260, 2720]
    assert [(n["text"], n["center"]) for n in ui["nodes"]] == [("设置", [350, 250]), ("蓝牙", [630, 460])]
    ops.dispatch("mobile.tap", {"x": 630, "y": 460, "kind": "double_tap"})
    ops.dispatch("mobile.type", {"text": "你好 'x'"})
    ops.dispatch("mobile.key", {"key": "home"})
    ops.dispatch("mobile.swipe", {"x1": 0, "y1": 1000, "x2": 0, "y2": 0, "duration_ms": 500})
    sent = [call[3:] for call in phones.calls() if call[:4] == ["hdc", "-t", "FAKEHM001", "shell"]]
    assert ["shell", "uitest", "uiInput", "doubleClick", "630", "460"] in sent
    assert ["shell", "uitest", "uiInput", "text", "'你好 '\\''x'\\'''"] in sent
    assert ["shell", "uitest", "uiInput", "keyEvent", "Home"] in sent
    assert ["shell", "uitest", "uiInput", "swipe", "0", "1000", "0", "0", "2000"] in sent  # 1000 px in 0.5 s
    with pytest.raises(WebCoderError) as err:
        ops.dispatch("mobile.key", {"key": "recent_apps"})
    assert code(err) == "MOBILE_UNSUPPORTED"


def test_files_only_move_between_phone_and_project(ops, project, phones):
    phones.only(hdc=[])
    (project / "notes.txt").write_text("hello phone", encoding="utf-8")
    (project / ".env").write_text("SECRET=1", encoding="utf-8")
    ops.dispatch("mobile.push", {"root": str(project), "path": "notes.txt", "device_path": "/sdcard/notes.txt"})
    assert (phones.phone / "sdcard" / "notes.txt").read_text(encoding="utf-8") == "hello phone"
    (project / "notes.txt").write_text("newer", encoding="utf-8")
    with pytest.raises(WebCoderError) as err:  # the phone's file has no backup: never replaced by accident
        ops.dispatch("mobile.push", {"root": str(project), "path": "notes.txt", "device_path": "/sdcard/notes.txt"})
    assert code(err) == "DESTINATION_EXISTS"
    assert (phones.phone / "sdcard" / "notes.txt").read_text(encoding="utf-8") == "hello phone"
    replaced = ops.dispatch("mobile.push", {"root": str(project), "path": "notes.txt",
                                            "device_path": "/sdcard/notes.txt", "overwrite": True})
    assert replaced["overwrote"] and (phones.phone / "sdcard" / "notes.txt").read_text(encoding="utf-8") == "newer"
    (phones.phone / "sdcard" / "notes.txt").write_text("hello phone", encoding="utf-8")  # back to the phone's text
    with pytest.raises(WebCoderError) as err:  # the deny list still applies
        ops.dispatch("mobile.push", {"root": str(project), "path": ".env", "device_path": "/sdcard/x"})
    assert code(err) == "PATH_DENIED"
    with pytest.raises(WebCoderError) as err:
        ops.dispatch("mobile.push", {"root": str(project), "path": "../outside.txt", "device_path": "/sdcard/x"})
    assert code(err) == "INVALID_PATH"
    with pytest.raises(WebCoderError) as err:
        ops.dispatch("mobile.push", {"root": str(project), "path": "notes.txt", "device_path": "sdcard/x"})
    assert code(err) == "INVALID_ARGUMENT"

    pulled = ops.dispatch("mobile.pull", {"root": str(project), "path": "from-phone/notes.txt",
                                          "device_path": "/sdcard/notes.txt"})
    assert pulled["size"] == 11 and (project / "from-phone" / "notes.txt").read_text(encoding="utf-8") == "hello phone"
    with pytest.raises(WebCoderError) as err:  # never overwrites
        ops.dispatch("mobile.pull", {"root": str(project), "path": "notes.txt", "device_path": "/sdcard/notes.txt"})
    assert code(err) == "DESTINATION_EXISTS"
    with pytest.raises(WebCoderError) as err:
        ops.dispatch("mobile.pull", {"root": str(project), "path": "gone.txt", "device_path": "/sdcard/missing"})
    assert code(err) == "MOBILE_COMMAND_FAILED" and not (project / "gone.txt").exists()
    assert not list(project.rglob(".webcoder-*.tmp"))  # no leftovers from the failed pull


def test_oversized_screenshots_fail_instead_of_saving_a_cut_image(ops, project, phones, monkeypatch):
    monkeypatch.setattr(mobile_module, "MAX_SCREENSHOT", 20)
    for platform, path in (("android", "big.png"), ("harmony", "big.jpeg")):
        phones.only(**({"hdc": []} if platform == "android" else {"adb": []}))
        with pytest.raises(WebCoderError) as err:
            ops.dispatch("mobile.screenshot", {"root": str(project), "path": path})
        assert code(err) == "MOBILE_COMMAND_FAILED" and "limit" in err.value.message
        assert not (project / path).exists()


def test_push_never_replaces_a_file_that_appears_during_the_transfer(ops, project, phones):
    (project / "app.conf").write_text("ours", encoding="utf-8")
    for platform, other in (("android", "hdc"), ("harmony", "adb")):
        phones.only(**{other: []}, racy_push=True)  # someone creates the file while ours is on its way
        with pytest.raises(WebCoderError) as err:
            ops.dispatch("mobile.push", {"root": str(project), "path": "app.conf",
                                         "device_path": f"/data/local/tmp/{platform}.conf"})
        assert code(err) == "DESTINATION_EXISTS", platform
        assert (phones.phone / "data/local/tmp" / f"{platform}.conf").read_bytes() == b"theirs\n"
        phones.only(**{other: []})
        ops.dispatch("mobile.push", {"root": str(project), "path": "app.conf",
                                     "device_path": f"/data/local/tmp/{platform}-new.conf"})
        assert (phones.phone / "data/local/tmp" / f"{platform}-new.conf").read_text(encoding="utf-8") == "ours"
    assert not list(phones.phone.rglob("*.webcoder-*.tmp"))  # temporary copies are always cleaned up


def test_phone_shell_runs_what_it_is_given(ops, phones):
    for platform, other in (("android", "hdc"), ("harmony", "adb")):
        phones.only(**{other: []})
        done = ops.dispatch("mobile.shell", {"command": "echo hello; from the phone"})
        assert done["exit_code"] == 0 and done["stdout"] == "hello; from the phone\n" and done["platform"] == platform
        failed = ops.dispatch("mobile.shell", {"command": "fail 5"})
        assert failed["exit_code"] == 5 and "boom" in failed["stderr"]
        assert phones.calls()[-1][-2:] == ["shell", "fail 5"]  # one argument, for the phone's own shell
    with pytest.raises(WebCoderError) as err:
        ops.dispatch("mobile.shell", {"command": "  "})
    assert code(err) == "INVALID_ARGUMENT"


def test_a_server_that_did_not_start_is_started_again(ops, phones):
    phones.only(hdc=[], server_fails=True)
    ops.dispatch("mobile.devices", {})
    phones.only(hdc=[])
    ops.dispatch("mobile.devices", {})
    ops.dispatch("mobile.devices", {})  # started now: not again
    assert [call[1:] for call in phones.calls() if call[1:] == ["start-server"]] == [["start-server"]] * 2


def test_each_capture_uses_its_own_file_on_the_phone(ops, project, phones):
    """Requests run at the same time: two screen reads must never read each other's dump, and none is left over."""
    for other in ("hdc", "adb"):
        phones.only(**{other: []})
        ops.dispatch("mobile.ui", {})
        ops.dispatch("mobile.ui", {})
    ops.dispatch("mobile.screenshot", {"root": str(project), "path": "shot.jpeg"})  # HarmonyOS: via a file too
    used = [call[-1] for call in phones.calls() if {"dump", "dumpLayout", "snapshot_display"} & set(call)]
    assert len(used) == 5 and len(set(used)) == 5 and all(path.startswith("/data/local/tmp/webcoder-") for path in used)
    assert not list(phones.phone.rglob("webcoder-*"))


def test_long_phone_shell_output_keeps_its_end(ops, phones, monkeypatch):
    phones.only(hdc=[])
    monkeypatch.setattr(mobile_module, "MAX_OUTPUT", 100)
    done = ops.dispatch("mobile.shell", {"command": "echo " + "x" * 500 + " THE END"})
    assert done["truncated"] and done["stdout"].endswith("x THE END\n") and len(done["stdout"]) <= 100


def test_screenshots_are_saved_whatever_the_write_limit(project, tmp_path, phones):
    files = FileOps(Sandbox([project]), BackupStore(tmp_path / "backups"), Limits(max_write_bytes=10))
    files.mobile = MobileOps(files, enabled=True, tools=FAKE_TOOLS)
    phones.only(hdc=[])
    shot = files.dispatch("mobile.screenshot", {"root": str(project), "path": "shot.png"})
    assert shot["size"] > 10 and (project / "shot.png").stat().st_size == shot["size"]


def test_harmony_logs_keep_the_newest_lines(ops, phones, monkeypatch):
    phones.only(adb=[])
    monkeypatch.setattr(mobile_module, "MAX_OUTPUT", 300)  # hilog -x printed more than the cap
    lines = ops.dispatch("mobile.logs", {"lines": 50})["text"].splitlines()
    assert lines[-1].endswith("line 49") and all(line.startswith("09-23 10:00:") for line in lines)
    assert len(lines) < 50  # only whole lines from the end, not the start of the buffer


def test_harmony_push_also_refuses_to_overwrite(ops, project, phones):
    phones.only(adb=[])
    (project / "a.txt").write_text("a", encoding="utf-8")
    ops.dispatch("mobile.push", {"root": str(project), "path": "a.txt", "device_path": "/data/local/tmp/a.txt"})
    with pytest.raises(WebCoderError) as err:
        ops.dispatch("mobile.push", {"root": str(project), "path": "a.txt", "device_path": "/data/local/tmp/a.txt"})
    assert code(err) == "DESTINATION_EXISTS"


def test_harmony_failed_pull_leaves_nothing(ops, project, phones):
    phones.only(adb=[])
    with pytest.raises(WebCoderError) as err:  # hdc says [Fail] but exits 0
        ops.dispatch("mobile.pull", {"root": str(project), "path": "x.txt", "device_path": "/data/missing"})
    assert code(err) == "MOBILE_COMMAND_FAILED" and not (project / "x.txt").exists()


def test_screenshots_logs_apps_and_info(ops, project, phones):
    phones.only(hdc=[])
    shot = ops.dispatch("mobile.screenshot", {"root": str(project), "path": "shots/home.png"})
    assert (project / "shots" / "home.png").read_bytes().startswith(b"\x89PNG") and shot["size"] > 8
    with pytest.raises(WebCoderError) as err:
        ops.dispatch("mobile.screenshot", {"root": str(project), "path": "shots/home.png"})
    assert code(err) == "DESTINATION_EXISTS"
    with pytest.raises(WebCoderError) as err:
        ops.dispatch("mobile.screenshot", {"root": str(project), "path": "shots/home.jpeg"})  # Android gives PNG
    assert code(err) == "INVALID_ARGUMENT"

    logs = ops.dispatch("mobile.logs", {"lines": 20, "contains": "line 1"})["text"].splitlines()
    assert logs and all("line 1" in line for line in logs)

    (project / "app.apk").write_bytes(b"PK fake apk")
    installed = ops.dispatch("mobile.app", {"action": "install", "root": str(project), "path": "app.apk"})
    assert "Success" in installed["output"]
    (project / "app.hap").write_bytes(b"PK fake hap")
    with pytest.raises(WebCoderError) as err:
        ops.dispatch("mobile.app", {"action": "install", "root": str(project), "path": "app.hap"})
    assert code(err) == "INVALID_ARGUMENT"
    ops.dispatch("mobile.app", {"action": "launch", "package": "com.demo"})
    ops.dispatch("mobile.app", {"action": "launch", "package": "com.demo", "component": ".MainActivity"})
    ops.dispatch("mobile.app", {"action": "stop", "package": "com.demo"})
    for bad in ("com.demo; reboot", "demo", "com.demo && rm -rf /"):
        with pytest.raises(WebCoderError) as err:
            ops.dispatch("mobile.app", {"action": "stop", "package": bad})
        assert code(err) == "INVALID_ARGUMENT"
    sent = [call[3:] for call in phones.calls() if call[:3] == ["adb", "-s", "emulator-5554"]]
    assert ["shell", "am", "start", "-n", "com.demo/.MainActivity"] in sent
    assert ["shell", "am", "force-stop", "com.demo"] in sent

    assert ops.dispatch("mobile.info", {"kind": "packages"})["value"] == ["com.android.settings", "com.demo"]
    assert "com.demo/.MainActivity" in ops.dispatch("mobile.info", {"kind": "current_app"})["value"][0]
    props = ops.dispatch("mobile.info", {"kind": "properties"})["value"]
    assert props["model"] == "Pixel 7" and props["android_version"] == "15" and props["screen"] == "1080x2400"


def test_harmony_apps_and_screenshot(ops, project, phones):
    phones.only(adb=[])
    ops.dispatch("mobile.screenshot", {"root": str(project), "path": "shot.jpeg"})
    assert (project / "shot.jpeg").read_bytes().startswith(b"\xff\xd8")
    (project / "app.hap").write_bytes(b"PK fake hap")
    assert "successfully" in ops.dispatch("mobile.app", {"action": "install", "root": str(project),
                                                         "path": "app.hap"})["output"]
    ops.dispatch("mobile.app", {"action": "launch", "package": "com.example.demo"})
    sent = [call[3:] for call in phones.calls() if call[:3] == ["hdc", "-t", "FAKEHM001"]]
    assert ["shell", "aa", "start", "-a", "EntryAbility", "-b", "com.example.demo"] in sent
    assert "com.example.demo" in ops.dispatch("mobile.info", {"kind": "packages"})["value"]


def test_install_and_settings_commands_carry_the_switch():
    assert install_commands("https://hub", "ABCD-EFGH", "windows", ["C:\\x"], False, True, True)["one_click"] \
        .endswith("-Mobile")
    unix = install_commands("https://hub", "ABCD-EFGH", "linux", ["/x"], False, True, True)
    assert unix["one_click"].endswith("--mobile") and "python3 -m webcoder.agent mobile on" in unix["manual"]
    assert "-Mobile" not in install_commands("https://hub", "C", "windows", ["C:\\x"], False, True)["one_click"]
    assert roots_command("windows", ["C:\\x"], True).endswith(
        "; if ($LASTEXITCODE -eq 0) { & \"$env:USERPROFILE\\.webcoder-agent\\venv\\Scripts\\python.exe\" "
        "-m webcoder.agent mobile on }")
    assert roots_command("linux", ["/x"], False).endswith(" && ~/.webcoder-agent/venv/bin/python -m webcoder.agent "
                                                         "mobile off")
    assert "mobile" not in roots_command("linux", ["/x"])


def test_pairing_again_keeps_the_phone_settings(hub, tmp_path):
    config_path = tmp_path / "agent" / "config.json"
    first = hub.add_device("pc")
    config = pair_device(hub.url, first["code"], [], config_path, proxy="none")
    config.mobile, config.mobile_tools = True, {"adb": "D:/sdk/adb.exe"}
    config.save()
    again = hub.panel.post(f"/api/devices/{first['device_id']}/pairing-code").json()
    repaired = pair_device(hub.url, again["code"], [], config_path, proxy="none")
    assert repaired.mobile is True and repaired.mobile_tools == {"adb": "D:/sdk/adb.exe"}
    assert repaired.device_secret != config.device_secret


def test_agent_cli_switch(tmp_path):
    config_path = tmp_path / "agent" / "config.json"
    AgentConfig(hub_url="http://127.0.0.1:1", device_id="dev_x", device_secret="c2VjcmV0",
                path=config_path).save()
    assert agent_main(["--config", str(config_path), "mobile", "on"]) == 0
    assert AgentConfig.load(config_path).mobile is True
    assert AgentConfig.load(config_path).build_fileops().info()["mobile"]["enabled"] is True
    assert agent_main(["--config", str(config_path), "mobile", "off"]) == 0
    assert AgentConfig.load(config_path).mobile is False


# --------------------------------------------------------------------------- through the hub
@pytest.fixture
def phone_computer(hub, tmp_path, phones):
    """A paired, running agent with phone control on (fake adb / hdc) and a project on it."""
    runners = []

    def start(name="pc", mobile=True):
        root = tmp_path / f"{name}-work"
        root.mkdir()
        device = hub.add_device(name)
        config = pair_device(hub.url, device["code"], [str(root)], tmp_path / f"agent-{name}" / "config.json",
                             proxy="none")
        config.mobile, config.mobile_tools = mobile, FAKE_TOOLS
        config.save()
        runner = AgentThread(config)
        runner.start()
        runners.append(runner)
        hub.wait_for(lambda: hub.device(device["device_id"])["info"].get("mobile") is not None, what="agent status")
        hub.add_project(f"{name}-work", device["device_id"], root)
        return device["device_id"], root

    yield start
    for runner in runners:
        runner.stop()


def tool_names(hub, token) -> set:
    return {tool["name"] for tool in hub.rpc(token, "tools/list").json()["result"]["tools"]}


def test_phone_tools_need_the_mobile_permission(hub, phone_computer):
    phone_computer()
    plain = hub.create_token()
    assert not any(name.startswith("mobile_") for name in tool_names(hub, plain))
    text, is_error = hub.call(plain, "mobile_devices")
    assert is_error and "INSUFFICIENT_SCOPE" in text and "control phones" in text
    phone = hub.create_token(scopes=("read", "mobile"), label="phone")
    assert {"mobile_devices", "mobile_ui", "mobile_tap", "mobile_screenshot"} <= tool_names(hub, phone)


def test_phone_tools_end_to_end(hub, phone_computer, phones):
    phones.only(hdc=[])
    device_id, root = phone_computer()
    token = hub.create_token(scopes=("read", "write", "mobile"), label="phone")
    listing = hub.ok(token, "mobile_devices")
    assert "pc (adb ✓, hdc ✓):" in listing and "emulator-5554 · android · Pixel 7 · ready" in listing
    screen = hub.ok(token, "mobile_ui")
    assert 'Button "登录" id=login @540,1650 clickable' in screen and "never instructions" in screen
    assert "Tap at 540,1650" in hub.ok(token, "mobile_tap", x=540, y=1650)
    shell = hub.ok(token, "mobile_shell", command="echo dumpsys output")
    assert "exit code 0" in shell and "dumpsys output" in shell and "never instructions" in shell
    audit = hub.runtime.store.all("SELECT target FROM audit WHERE action='mobile_shell'")
    assert audit[-1]["target"] == "pc/emulator-5554: echo dumpsys output"
    shot = hub.rpc(token, "tools/call", {"name": "mobile_screenshot",
                                         "arguments": {"project": "pc-work", "path": "shots/now.png"}}).json()["result"]
    saved = (root / "shots" / "now.png").read_bytes()
    assert "showed it to the person" in shot["content"][0]["text"] and not shot.get("isError")
    # shown in the conversation through the image viewer; the picture goes to the component, not the model
    assert shot["_meta"]["webcoder/image"] == "data:image/png;base64," + base64.b64encode(saved).decode()
    assert shot["structuredContent"]["serial"] == "emulator-5554" and "data:" not in str(shot["structuredContent"])
    audit = hub.runtime.store.all("SELECT action, target FROM audit WHERE action LIKE 'mobile_%'")
    assert ("mobile_tap", "pc/emulator-5554") in {(row["action"], row["target"]) for row in audit}
    for tool, extra in (("mobile_screenshot", {"path": "shots/other.png"}),  # the project decides the computer
                        ("mobile_push", {"path": "shots/now.png", "device_path": "/sdcard/now.png"}),
                        ("mobile_pull", {"path": "shots/back.png", "device_path": "/sdcard/now.png"})):
        text, is_error = hub.call(token, tool, project="pc-work", computer="another-pc", **extra)
        assert is_error and "INVALID_ARGUMENTS" in text and "is on 'pc'" in text, tool
    assert not (root / "shots" / "other.png").exists() and not (root / "shots" / "back.png").exists()


def test_phone_control_off_on_the_computer(hub, phone_computer):
    phone_computer(mobile=False)
    token = hub.create_token(scopes=("read", "mobile"), label="phone")
    assert "phone control is off" in hub.ok(token, "mobile_devices")
    text, is_error = hub.call(token, "mobile_ui")
    assert is_error and "MOBILE_NO_COMPUTER" in text


def test_project_scoped_tokens_only_reach_their_computers(hub, phone_computer):
    phone_computer("first")
    phone_computer("second")
    first = next(p for p in hub.panel.get("/api/projects").json()["projects"] if p["alias"] == "first-work")
    token = hub.create_token(scopes=("read", "mobile"), projects=[first["id"]], label="first only")
    listing = hub.ok(token, "mobile_devices")
    assert "- first (" in listing and "second" not in listing
    text, is_error = hub.call(token, "mobile_ui", computer="second")
    assert is_error and "COMPUTER_NOT_FOUND" in text
