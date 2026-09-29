"""Everyday Android / HarmonyOS debugging against the fake adb / hdc: filtered and crash logs, app info, clearing
data, opening links, port mappings and wireless debugging, through the agent and the hub."""
import pytest

from tests import test_mobile as fixtures
from tests.test_mobile import code, tool_names
from webcoder.common.errors import WebCoderError

ops, phone_computer, phones, project = fixtures.ops, fixtures.phone_computer, fixtures.phones, fixtures.project
ANDROID, HARMONY = {"hdc": []}, {"adb": []}


def sent(phones, prefix):
    return [call[len(prefix):] for call in phones.calls() if call[:len(prefix)] == prefix]


def test_android_logs_by_app_level_crash_and_clear(ops, phones):
    phones.only(**ANDROID)
    logs = ops.dispatch("mobile.logs", {"package": "com.demo", "level": "warn", "lines": 20})
    rows = logs["text"].splitlines()
    assert logs["pid"] == "4321,4322" and len(rows) == 20  # the app and its :remote process
    assert {row.split()[2] for row in rows} == {"4321", "4322"}
    # no -t (it would cut before filtering) and no --pid (logcat takes one; two are filtered on the computer)
    assert ["logcat", "-d", "*:W"] in sent(phones, ["adb", "-s", "emulator-5554"])
    with pytest.raises(WebCoderError) as stopped:
        ops.dispatch("mobile.logs", {"package": "com.not.running"})
    assert code(stopped) == "MOBILE_APP_NOT_RUNNING"
    crash = ops.dispatch("mobile.logs", {"buffer": "crash", "package": "com.demo"})["text"]
    assert "NullPointerException" in crash and "com.other" not in crash  # whole crash of that process only
    assert "SIGSEGV" in crash and "com.demo:gpu" in crash  # a native crash of one of its processes too,
    assert "*** ***" in crash and "SIGABRT" not in crash  # whole report, though crash_dump wrote it
    assert "DEMO_FRAME" in crash and "OTHER_FRAME" not in crash  # one writer, two apps' reports: only ours
    assert "com.other" in ops.dispatch("mobile.logs", {"buffer": "crash"})["text"]
    assert ops.dispatch("mobile.logs", {"clear": True})["cleared"]
    assert ["logcat", "-c"] in sent(phones, ["adb", "-s", "emulator-5554"])
    for bad in ({"level": "loud"}, {"buffer": "radio"}, {"package": "demo; reboot"}, {"clear": "yes"},
                {"buffer": "crash", "level": "error"}):  # crash logs have no level: refused, not ignored
        with pytest.raises(WebCoderError) as err:
            ops.dispatch("mobile.logs", bad)
        assert code(err) == "INVALID_ARGUMENT", bad


def test_harmony_logs_by_app_level_crash_reports_and_clear(ops, phones):
    phones.only(**HARMONY)
    logs = ops.dispatch("mobile.logs", {"package": "com.example.demo", "level": "error", "lines": 5})
    assert logs["pid"] == "777" and len(logs["text"].splitlines()) == 5
    assert ["shell", "hilog", "-x", "-P", "777", "-L", "E"] in sent(phones, ["hdc", "-t", "FAKEHM001"])
    crash = ops.dispatch("mobile.logs", {"buffer": "crash", "package": "com.example.demo"})["text"]
    assert "jscrash-com.example.demo" in crash and "TypeError" in crash and "com.other" not in crash
    assert ops.dispatch("mobile.logs", {"clear": True})["cleared"]
    assert ["shell", "hilog", "-r"] in sent(phones, ["hdc", "-t", "FAKEHM001"])


def test_app_info_memory_clear_and_open_url_on_both_platforms(ops, phones):
    phones.only(**ANDROID)
    info = ops.dispatch("mobile.info", {"kind": "app", "package": "com.demo"})["value"]
    assert info["versionName"] == "1.2.3" and info["versionCode"] == "42" and info["debuggable"] is True
    assert info["firstInstallTime"] == "2026-09-01 10:00:00" and info["pid"] == "4321"
    with pytest.raises(WebCoderError) as missing:
        ops.dispatch("mobile.info", {"kind": "app", "package": "com.nope"})
    assert code(missing) == "MOBILE_APP_NOT_FOUND"
    assert any("TOTAL PSS" in line for line in ops.dispatch("mobile.info", {"kind": "memory",
                                                                            "package": "com.demo"})["value"])
    assert "Success" in ops.dispatch("mobile.app", {"action": "clear", "package": "com.demo"})["output"]
    ops.dispatch("mobile.app", {"action": "open_url", "url": "https://example.test/a?b=1&c='x'"})
    ops.dispatch("mobile.app", {"action": "open_url", "url": "myapp://order/42", "package": "com.demo"})
    calls = sent(phones, ["adb", "-s", "emulator-5554", "shell", "am", "start"])
    # one single-quoted word for the phone's shell: & and ' cannot start or end anything
    assert ["-W", "-a", "android.intent.action.VIEW", "-d", "'https://example.test/a?b=1&c='\\''x'\\'''"] in calls
    assert ["-W", "-a", "android.intent.action.VIEW", "-d", "'myapp://order/42'", "-p", "com.demo"] in calls
    with pytest.raises(WebCoderError) as crashed:  # am -W: "Status: crash" and no "Error": still a failure
        ops.dispatch("mobile.app", {"action": "open_url", "url": "https://crash.example/"})
    assert code(crashed) == "MOBILE_COMMAND_FAILED"
    for bad in ("not a url", "javascript alert(1)", "https://x\nreboot", "", "https://example:bad", "https://",
                "https://[::1"):
        with pytest.raises(WebCoderError) as err:
            ops.dispatch("mobile.app", {"action": "open_url", "url": bad})
        assert code(err) == "INVALID_ARGUMENT", bad

    phones.only(**HARMONY)
    info = ops.dispatch("mobile.info", {"kind": "app", "package": "com.example.demo"})["value"]
    assert info["versionName"] == "2.0.1" and info["debuggable"] is True and info["pid"] == "777"
    assert "12345" in "".join(ops.dispatch("mobile.info", {"kind": "memory", "package": "com.example.demo"})["value"])
    ops.dispatch("mobile.app", {"action": "clear", "package": "com.example.demo"})
    with pytest.raises(WebCoderError) as err:  # "unsuccessfully" contains "success": still a failure
        ops.dispatch("mobile.app", {"action": "clear", "package": "com.example.other"})
    assert code(err) == "MOBILE_COMMAND_FAILED"
    ops.dispatch("mobile.app", {"action": "open_url", "url": "https://example.test"})
    calls = sent(phones, ["hdc", "-t", "FAKEHM001", "shell", "aa", "start"])
    assert ["-A", "ohos.want.action.viewData", "-U", "'https://example.test'", "-e", "entity.system.browsable"] in calls


@pytest.mark.parametrize("platform", ["android", "harmony"])
def test_port_mappings_for_dev_servers(ops, phones, platform):
    phones.only(**(ANDROID if platform == "android" else HARMONY))
    assert ops.dispatch("mobile.port", {"action": "list"})["mappings"] == []
    reverse = ops.dispatch("mobile.port", {"action": "reverse", "computer_port": 5173, "phone_port": 5173})
    assert reverse["mappings"] == [{"direction": "reverse", "computer": "tcp:5173", "phone": "tcp:5173"}]
    both = ops.dispatch("mobile.port", {"action": "forward", "computer_port": 18080, "phone_port": 8080})["mappings"]
    assert {"direction": "forward", "computer": "tcp:18080", "phone": "tcp:8080"} in both and len(both) == 2
    with pytest.raises(WebCoderError) as unknown:
        ops.dispatch("mobile.port", {"action": "remove", "direction": "forward", "computer_port": 18081,
                                     "phone_port": 8080})
    assert code(unknown) == "MOBILE_PORT_NOT_FOUND"
    left = ops.dispatch("mobile.port", {"action": "remove", "direction": "reverse", "computer_port": 5173,
                                        "phone_port": 5173})["mappings"]
    assert left == [{"direction": "forward", "computer": "tcp:18080", "phone": "tcp:8080"}]
    for bad in ({"action": "forward", "computer_port": 80, "phone_port": 8080},
                {"action": "reverse", "computer_port": 5173, "phone_port": "5173"},
                {"action": "remove", "computer_port": 18080, "phone_port": 8080}, {"action": "open"}):
        with pytest.raises(WebCoderError) as err:
            ops.dispatch("mobile.port", bad)
        assert code(err) == "INVALID_ARGUMENT", bad


def test_read_only_device_lists_but_does_not_change(ops, phones):
    phones.only(**ANDROID)
    ops.sb.read_only = True
    assert ops.dispatch("mobile.port", {"action": "list"})["mappings"] == []
    assert ops.dispatch("mobile.logs", {"package": "com.demo"})["pid"] == "4321,4322"
    before = len(phones.calls())
    for op, args in (("mobile.port", {"action": "reverse", "computer_port": 5173, "phone_port": 5173}),
                     ("mobile.logs", {"clear": True}),
                     ("mobile.app", {"action": "clear", "package": "com.demo"}),
                     ("mobile.app", {"action": "open_url", "url": "https://example.test"}),
                     ("mobile.connect", {"action": "connect", "platform": "android", "host": "192.168.1.5",
                                         "port": 5555})):
        with pytest.raises(WebCoderError) as err:
            ops.dispatch(op, args)
        assert code(err) == "AGENT_READ_ONLY", op
    assert len(phones.calls()) == before  # refused before anything ran


def test_wireless_debugging_stays_on_the_local_network(ops, phones):
    done = ops.dispatch("mobile.connect", {"action": "pair", "platform": "android", "host": "192.168.1.5",
                                          "port": 37123, "code": "123456"})
    assert "Successfully paired" in done["output"]
    ops.dispatch("mobile.connect", {"action": "connect", "platform": "android", "host": "192.168.1.5", "port": 5555})
    ops.dispatch("mobile.connect", {"action": "connect", "platform": "harmony", "host": "10.0.0.8", "port": 8710})
    ops.dispatch("mobile.connect", {"action": "disconnect", "platform": "harmony", "host": "10.0.0.8", "port": 8710})
    calls = [call for call in phones.calls() if call[1] in ("pair", "connect", "tconn")]
    assert ["adb", "pair", "192.168.1.5:37123", "123456"] in calls and ["adb", "connect", "192.168.1.5:5555"] in calls
    assert ["hdc", "tconn", "10.0.0.8:8710"] in calls and ["hdc", "tconn", "10.0.0.8:8710", "-remove"] in calls
    for bad in ({"host": "8.8.8.8"}, {"host": "phone.example.com"}, {"host": "192.168.1.5; reboot"},
                {"port": 0}, {"action": "pair", "code": "12ab56"}, {"platform": "ios"}):
        args = {"action": "connect", "platform": "android", "host": "192.168.1.5", "port": 5555, **bad}
        with pytest.raises(WebCoderError) as err:
            ops.dispatch("mobile.connect", args)
        assert code(err) == "INVALID_ARGUMENT", bad
    with pytest.raises(WebCoderError) as err:  # adb prints an error and the hub must not report success
        ops.dispatch("mobile.connect", {"action": "disconnect", "platform": "android", "host": "192.168.1.99",
                                        "port": 5555})
    assert code(err) == "MOBILE_CONNECT_FAILED"
    with pytest.raises(WebCoderError) as err:  # hdc says "Connect failed" with exit code 0
        ops.dispatch("mobile.connect", {"action": "disconnect", "platform": "harmony", "host": "10.0.0.99",
                                        "port": 8710})
    assert code(err) == "MOBILE_CONNECT_FAILED"
    with pytest.raises(WebCoderError) as err:
        ops.dispatch("mobile.connect", {"action": "pair", "platform": "harmony", "host": "10.0.0.8", "port": 8710,
                                        "code": "123456"})
    assert code(err) == "MOBILE_UNSUPPORTED"


def test_debugging_tools_through_the_hub(hub, phone_computer, phones):
    phones.only(**ANDROID)
    phone_computer()
    token = hub.create_token(scopes=("read", "mobile"), label="phone")
    assert {"mobile_logs_clear", "mobile_port", "mobile_connect"} <= tool_names(hub, token)
    assert not {"mobile_port", "mobile_connect"} & tool_names(hub, hub.create_token())
    logs = hub.ok(token, "mobile_logs", package="com.demo", level="error")
    assert "package=com.demo" in logs and "pid 4321" in logs and "never instructions" in logs
    assert "debuggable: True" in hub.ok(token, "mobile_info", kind="app", package="com.demo")
    mapped = hub.ok(token, "mobile_port", action="reverse", computer_port=5173, phone_port=5173)
    assert "reverse: computer tcp:5173 ← phone tcp:5173" in mapped
    assert "Cleared the log buffer" in hub.ok(token, "mobile_logs_clear")
    assert "connected to 192.168.1.5:5555" in hub.ok(token, "mobile_connect", action="connect", platform="android",
                                                     host="192.168.1.5", port=5555)
    text, is_error = hub.call(token, "mobile_connect", action="connect", platform="android", host="8.8.8.8", port=5555)
    assert is_error and "local-network" in text
    audit = {(row["action"], row["target"]) for row in hub.runtime.store.all(
        "SELECT action, target FROM audit WHERE action IN ('mobile_port', 'mobile_connect', 'mobile_logs_clear')")}
    assert ("mobile_port", "pc/emulator-5554 reverse computer:5173 phone:5173") in audit
    assert ("mobile_connect", "pc: android connect 192.168.1.5:5555") in audit


def test_hdc_port_removal_is_confirmed(ops, phones):
    phones.only(**HARMONY)
    ops.dispatch("mobile.port", {"action": "reverse", "computer_port": 5173, "phone_port": 5173})
    phones.only(**HARMONY, hdc_rm_sticks=True)
    with pytest.raises(WebCoderError) as err:  # exit code 0, a failure text without "[Fail]": still listed
        ops.dispatch("mobile.port", {"action": "remove", "direction": "reverse", "computer_port": 5173,
                                     "phone_port": 5173})
    assert code(err) == "MOBILE_COMMAND_FAILED"
    phones.only(**HARMONY)
    assert ops.dispatch("mobile.port", {"action": "remove", "direction": "reverse", "computer_port": 5173,
                                        "phone_port": 5173})["mappings"] == []


def test_hdc_refuses_a_pair_already_used_in_the_other_direction(ops, phones):
    phones.only(**HARMONY)
    ops.dispatch("mobile.port", {"action": "forward", "computer_port": 18080, "phone_port": 18081})
    with pytest.raises(WebCoderError) as err:  # rport tcp:18080 tcp:18081 would share the pair fport rm takes
        ops.dispatch("mobile.port", {"action": "reverse", "computer_port": 18081, "phone_port": 18080})
    assert code(err) == "MOBILE_COMMAND_FAILED"
    assert ops.dispatch("mobile.port", {"action": "list"})["mappings"] == [
        {"direction": "forward", "computer": "tcp:18080", "phone": "tcp:18081"}]


def test_a_failed_phone_call_keeps_its_audit_target(hub, phone_computer, phones):
    phones.only(**ANDROID)
    phone_computer()
    token = hub.create_token(scopes=("read", "mobile"), label="phone")
    text, is_error = hub.call(token, "mobile_port", action="remove", direction="reverse", computer_port=5999,
                              phone_port=5999)
    assert is_error and "MOBILE_PORT_NOT_FOUND" in text
    text, is_error = hub.call(token, "mobile_logs", package="com.not.running")
    assert is_error and "MOBILE_APP_NOT_RUNNING" in text
    rows = {(row["action"], row["status"], row["target"]) for row in hub.runtime.store.all(
        "SELECT action, status, target FROM audit WHERE action IN ('mobile_port', 'mobile_logs')")}
    assert ("mobile_port", "MOBILE_PORT_NOT_FOUND", "pc/? remove reverse computer:5999 phone:5999") in rows
    assert ("mobile_logs", "MOBILE_APP_NOT_RUNNING", "pc/? buffer=main package=com.not.running") in rows


def test_cdp_page_discovery_fails_when_a_forward_cannot_be_cleaned_up(ops, phones, monkeypatch):
    from contextlib import contextmanager

    from webcoder.agent import mobile_web as web

    phones.only(**ANDROID, cdp_sockets=["webview_devtools_remote_1"])

    @contextmanager
    def stuck(backend, serial, name, op):
        raise WebCoderError("MOBILE_CDP_CLEANUP_FAILED", "Cannot clean up tcp:1 → localabstract:x")
        yield

    monkeypatch.setattr(web, "forward", stuck)
    with pytest.raises(WebCoderError) as err:  # not one more "socket error" next to a successful listing
        ops.dispatch("mobile.web.pages", {})
    assert code(err) == "MOBILE_CDP_CLEANUP_FAILED"


def test_component_names_reach_the_phone_shell_as_one_quoted_word(ops, phones):
    phones.only(**ANDROID)
    ops.dispatch("mobile.app", {"action": "launch", "package": "com.demo", "component": "com.demo.Outer$Inner"})
    ops.dispatch("mobile.app", {"action": "open_url", "url": "myapp://x", "package": "com.demo",
                                "component": ".Deep$Link"})
    calls = sent(phones, ["adb", "-s", "emulator-5554", "shell", "am", "start"])
    assert ["-n", "'com.demo/com.demo.Outer$Inner'"] in calls  # $Inner is not expanded by the phone's shell
    assert ["-W", "-a", "android.intent.action.VIEW", "-d", "'myapp://x'", "-n", "'com.demo/.Deep$Link'"] in calls


def test_harmony_crash_reports_match_the_whole_bundle_name(ops, phones):
    phones.only(**HARMONY)
    # com.example is a prefix of com.example.demo, whose report must not be returned for it
    assert "no crash reports" in ops.dispatch("mobile.logs", {"buffer": "crash", "package": "com.example"})["text"]
    assert "com.example.demo" in ops.dispatch("mobile.logs", {"buffer": "crash", "package": "com.example.demo"})["text"]


def test_link_audit_keeps_where_it_points_not_its_query(hub, phone_computer, phones):
    phones.only(**ANDROID)
    phone_computer()
    token = hub.create_token(scopes=("read", "mobile"), label="phone")
    hub.ok(token, "mobile_app", action="open_url", url="https://user:pw@shop.example/pay?token=SECRET#frag")
    target = hub.runtime.store.all("SELECT target FROM audit WHERE action='mobile_app'")[-1]["target"]
    assert target == "pc/emulator-5554 open https://shop.example/pay"
    assert "SECRET" not in target and "pw" not in target


def test_harmony_crash_folder_missing_or_unreadable(ops, phones):
    phones.only(**HARMONY, faultlog="missing")
    assert "does not exist" in ops.dispatch("mobile.logs", {"buffer": "crash"})["text"]
    phones.only(**HARMONY, faultlog="denied")
    with pytest.raises(WebCoderError) as err:  # a read failure is not "no crashes"
        ops.dispatch("mobile.logs", {"buffer": "crash"})
    assert code(err) == "MOBILE_COMMAND_FAILED" and "Permission denied" in err.value.message


def test_a_malformed_url_is_refused_not_an_internal_error(hub, phone_computer, phones):
    phones.only(**ANDROID)
    phone_computer()
    token = hub.create_token(scopes=("read", "mobile"), label="phone")
    text, is_error = hub.call(token, "mobile_app", action="open_url", url="https://[::1/x")
    assert is_error and "INVALID_ARGUMENT" in text and "INTERNAL_ERROR" not in text
    audit = hub.runtime.store.all("SELECT status, target FROM audit WHERE action='mobile_app'")[-1]
    assert audit["status"] == "INVALID_ARGUMENT" and audit["target"].startswith("pc/? open ")


def test_harmony_port_creation_is_confirmed_in_its_direction(ops, phones):
    phones.only(**HARMONY)
    added = ops.dispatch("mobile.port", {"action": "reverse", "computer_port": 5173, "phone_port": 5173})
    assert added["mappings"] == [{"direction": "reverse", "computer": "tcp:5173", "phone": "tcp:5173"}]
    rport = [call for call in sent(phones, ["hdc", "-t", "FAKEHM001"]) if call[:1] == ["rport"]]
    assert rport == [["rport", "tcp:5173", "tcp:5173"]]  # the phone's port first, then the computer's


def test_harmony_crash_script_treats_a_missing_parent_as_no_reports():
    from webcoder.agent.mobile import HdcBackend

    sent_scripts = []

    class Hdc(HdcBackend):
        def text(self, args, serial=None, **kwargs):
            sent_scripts.append(args[1])
            return "WEBCODER_NOFAULTLOG\n"

    assert "does not exist" in Hdc(["hdc"]).crash_logs("HM1", 50, None)
    script = sent_scripts[0]
    assert "[ ! -e '/data/log/faultlog' ] ||" in script  # no parent: nothing was ever logged
    assert "[ -r '/data/log/faultlog' ] && [ -x '/data/log/faultlog' ]" in script  # unreadable parent: failure


def test_a_failed_port_mapping_reports_what_is_left(ops, phones):
    phones.only(**ANDROID)
    ops.dispatch("mobile.port", {"action": "reverse", "computer_port": 5173, "phone_port": 5173})
    phones.only(**ANDROID, adb_fail_forward=True)
    with pytest.raises(WebCoderError) as err:
        ops.dispatch("mobile.port", {"action": "forward", "computer_port": 18080, "phone_port": 8080})
    assert "mappings now: reverse tcp:5173↔tcp:5173" in err.value.message and "action=remove" in err.value.message


def test_hdc_port_tasks_match_their_endpoints_in_order(ops, phones):
    phones.only(**HARMONY)
    ops.dispatch("mobile.port", {"action": "forward", "computer_port": 18080, "phone_port": 18081})
    # fport tcp:18081 tcp:18080 is another task (computer 18081 → phone 18080): not refused as the same pair
    both = ops.dispatch("mobile.port", {"action": "forward", "computer_port": 18081, "phone_port": 18080})["mappings"]
    assert len(both) == 2
    left = ops.dispatch("mobile.port", {"action": "remove", "direction": "forward", "computer_port": 18080,
                                        "phone_port": 18081})["mappings"]
    assert left == [{"direction": "forward", "computer": "tcp:18081", "phone": "tcp:18080"}]  # the other stays
