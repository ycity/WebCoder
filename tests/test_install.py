"""Device setup: folder validation, the generated install / folder commands, the installers, agent CLI."""
import base64
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from webcoder.agent.__main__ import main as agent_main
from webcoder.agent.__main__ import pair_device
from webcoder.agent.config import AgentConfig
from webcoder.hub.auth import ApiError
from webcoder.hub.install import install_commands, ps_quote, roots_command, sh_quote, validate_roots

INSTALLERS = Path(__file__).resolve().parents[1] / "webcoder" / "hub" / "installers"
WINDOWS_ROOTS = ["C:\\Users\\me\\My Projects", "D:\\it's\\代码"]
UNIX_ROOTS = ["/home/me/My Projects", "/srv/it's/代码"]


def powershell() -> str:
    exe = shutil.which("powershell") or shutil.which("pwsh")
    if not exe:
        pytest.skip("PowerShell is not installed")
    return exe


def bash() -> str:
    exe = shutil.which("bash")
    if not exe or Path(exe).parent.name.lower() == "system32":  # the WSL launcher, not a usable bash
        pytest.skip("bash is not installed")
    return exe


def run_powershell(script: str) -> subprocess.CompletedProcess:
    # -EncodedCommand (UTF-16LE) keeps non-ASCII paths intact on every code page.
    encoded = base64.b64encode(("[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false\n" + script)
                               .encode("utf-16-le")).decode()
    return subprocess.run([powershell(), "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                          capture_output=True, timeout=120)


def test_validate_roots():
    assert validate_roots([" C:\\Users\\me\\code ", '"D:\\x"', "C:\\Users\\me\\code", ""], "windows") == [
        "C:\\Users\\me\\code", "D:\\x"]
    assert validate_roots(["\\\\nas\\share\\proj"], "windows") == ["\\\\nas\\share\\proj"]
    assert validate_roots(["/home/me/code", "/Users/me/x"], "linux") == ["/home/me/code", "/Users/me/x"]
    refused = {"windows": ["code", "C:relative", "/home/me", "\\\\.\\PhysicalDrive0", "\\\\?\\C:\\x",
                           "%USERPROFILE%\\code", "$env:USERPROFILE\\x", "~\\code", "C:\\a\nb"],
               "macos": ["code", "~/code", "$HOME/code", "${HOME}/x", "C:\\x"]}
    for platform, values in refused.items():
        for value in values:
            with pytest.raises(ApiError):
                validate_roots([value], platform)
    with pytest.raises(ApiError):
        validate_roots([f"/data/{i}" for i in range(11)], "linux")
    with pytest.raises(ApiError):
        validate_roots(["/x"], "solaris")


def test_quoting():
    assert ps_quote("D:\\it's") == "'D:\\it''s'"
    assert sh_quote("/a b/it's") == "'/a b/it'\\''s'"


def test_install_commands_windows():
    commands = install_commands("https://hub.example", "ABCD-EFGH", "windows", WINDOWS_ROOTS, True, False)
    one_click = commands["one_click"]
    assert one_click.startswith("& ([scriptblock]::Create((irm 'https://hub.example/downloads/install-agent.ps1')))")
    assert "-Root 'C:\\Users\\me\\My Projects','D:\\it''s\\代码'" in one_click
    assert one_click.endswith("-ReadOnly -NoAutostart")
    assert commands["manual"][1:] == [  # every setting explicit, like the one-click installer
        "python -m webcoder.agent pair --hub https://hub.example --code ABCD-EFGH",
        "python -m webcoder.agent roots set --create 'C:\\Users\\me\\My Projects' 'D:\\it''s\\代码'",
        "python -m webcoder.agent read-only on", "python -m webcoder.agent mobile off", "python -m webcoder.agent run"]
    plain = install_commands("https://hub.example", "ABCD-EFGH", "windows", ["C:\\x"], False, True)["one_click"]
    assert "-ReadOnly" not in plain and "-NoAutostart" not in plain
    empty = install_commands("https://hub.example", "ABCD-EFGH", "linux", [], False, True)["manual"]
    assert empty[2] == "python3 -m webcoder.agent roots set --create"  # no folders: take away any old ones


def test_installers_apply_the_folder_list_even_when_empty(tmp_path):
    """Reinstalling with fewer (or no) folders must take the others away, not keep the old config's."""
    ps1 = (INSTALLERS / "install-agent.ps1").read_text(encoding="utf-8")
    assert re.search(r"^    & \$Python -m webcoder\.agent --config \$Config roots set --create @Root \| Out-Null$",
                     ps1, re.M)  # at the top level of the install block, not inside `if ($Root.Count -gt 0)`
    sh = (INSTALLERS / "install-agent.sh").read_text(encoding="utf-8")
    line = next(line for line in sh.splitlines() if line.startswith("agent roots set --create"))
    out = tmp_path / "args"
    for roots, expected in (("()", []), ("('/srv/my code' \"it's\")", ["/srv/my code", "it's"])):
        script = (f"set -euo pipefail\nagent() {{ printf '%s\\0' \"$@\" > {sh_quote(out.as_posix())}; }}\n"
                  f"ROOTS={roots}\n{line}\n")
        result = subprocess.run([bash(), "-c", script], capture_output=True, timeout=60)
        assert result.returncode == 0, result.stderr  # an empty array must survive set -u (bash 3.2 on macOS)
        assert out.read_bytes().decode().split("\0")[:-1] == ["roots", "set", "--create", *expected]


def test_install_commands_unix():
    one_click = install_commands("https://hub.example", "ABCD-EFGH", "linux", UNIX_ROOTS, False, True)["one_click"]
    assert one_click.startswith("bash -c \"$(curl -fsSL 'https://hub.example/downloads/install-agent.sh' || echo "
                                "'exit 1')\" install-agent.sh ")
    assert "--root '/srv/it'\\''s/代码'" in one_click and "--no-autostart" not in one_click
    assert roots_command("macos", UNIX_ROOTS).startswith(
        "~/.webcoder-agent/venv/bin/python -m webcoder.agent roots set --create '/home/me/My Projects' ")


def test_windows_one_click_passes_folders_through_intact():
    """Run the real one-click line with the download stubbed out and check what the installer would receive."""
    one_click = install_commands("https://hub.example", "ABCD-EFGH", "windows", WINDOWS_ROOTS, True, False)[
        "one_click"]
    # Aliases outrank functions, so stub what the irm alias points to (functions outrank cmdlets).
    stub = ("function Invoke-RestMethod($Uri) { 'param([string]$Hub, [string]$Code, [string[]]$Root, "
            "[switch]$ReadOnly, [switch]$NoAutostart) [pscustomobject]@{hub=$Hub; code=$Code; root=@($Root); "
            "read_only=[bool]$ReadOnly; no_autostart=[bool]$NoAutostart} | ConvertTo-Json -Compress' }\n")
    result = run_powershell(stub + one_click)
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    received = json.loads(result.stdout.decode("utf-8-sig"))
    assert received == {"hub": "https://hub.example", "code": "ABCD-EFGH", "root": WINDOWS_ROOTS,
                        "read_only": True, "no_autostart": True}
    roots = run_powershell("function python { $args | ConvertTo-Json -Compress }\n"
                           + roots_command("windows", WINDOWS_ROOTS).replace(
                               '& "$env:USERPROFILE\\.webcoder-agent\\venv\\Scripts\\python.exe"', "python"))
    assert json.loads(roots.stdout.decode("utf-8-sig"))[-2:] == WINDOWS_ROOTS


def test_windows_settings_command_stops_at_the_first_failure():
    """PowerShell 5.1 has no &&: a folder that cannot be set must not leave phone / SSH settings half applied."""
    command = roots_command("windows", WINDOWS_ROOTS, True, ["web-1"]).replace(
        '& "$env:USERPROFILE\\.webcoder-agent\\venv\\Scripts\\python.exe"', "python")
    stub = ("$global:ran = New-Object System.Collections.ArrayList\n"
            "function python { [void]$global:ran.Add($args[2]); "
            "$global:LASTEXITCODE = $(if ($args[2] -eq $env:FAIL_STEP) { 1 } else { 0 }) }\n")
    for failing, expected in (("roots", "roots"), ("mobile", "roots,mobile"), ("none", "roots,mobile,ssh-hosts")):
        result = run_powershell(f"$env:FAIL_STEP = '{failing}'\n{stub}{command}\n$global:ran -join ','")
        assert result.stdout.decode("utf-8-sig").strip() == expected, failing


def test_unix_one_click_passes_folders_through_intact(tmp_path):
    one_click = install_commands("https://hub.example", "ABCD-EFGH", "linux", UNIX_ROOTS, True, True)["one_click"]
    stub = tmp_path / "stub.sh"
    stub.write_text('for a in "$@"; do printf "%s\\0" "$a"; done\n', encoding="utf-8")
    script = f"curl() {{ cat {sh_quote(stub.as_posix())}; }}\n{one_click}\n"
    result = subprocess.run([bash(), "-c", script], capture_output=True, timeout=60)
    assert result.returncode == 0, result.stderr
    received = result.stdout.decode("utf-8").split("\0")[:-1]
    assert received == ["--hub", "https://hub.example", "--code", "ABCD-EFGH", "--root", UNIX_ROOTS[0],
                        "--root", UNIX_ROOTS[1], "--read-only"]
    unreachable = subprocess.run([bash(), "-c", f"curl() {{ return 7; }}\n{one_click}\n"], capture_output=True,
                                 timeout=60)
    assert unreachable.returncode != 0  # a download that fails is a failed installation, not an empty success


def test_launchd_plist_paths_are_xml_escaped():
    script = (INSTALLERS / "install-agent.sh").read_text(encoding="utf-8")
    helper = next(line for line in script.splitlines() if line.startswith("xml() {"))
    result = subprocess.run([bash(), "-c", f"{helper}\nxml '/Users/Tom & Jerry/<a>/.webcoder-agent'"],
                            capture_output=True, timeout=60)
    assert result.stdout.decode() == "/Users/Tom &amp; Jerry/&lt;a&gt;/.webcoder-agent"
    assert "<string>$PY</string>" not in script and '<string>$(xml "$PY")</string>' in script


def test_installers_put_the_cli_programs_on_path(tmp_path):
    """webc, webcoder-bridge and webcoder-agent (whose commands the messages suggest) end up on PATH."""
    ps1 = (INSTALLERS / "install-agent.ps1").read_text(encoding="utf-8")
    assert "foreach ($name in 'webc.exe', 'webcoder-bridge.exe', 'webcoder-agent.exe') {" in ps1
    script = (INSTALLERS / "install-agent.sh").read_text(encoding="utf-8")
    block = re.search(r'^if \[ -x "\$VENV/bin/webc" \]; then\n.*?^fi\n', script, re.S | re.M).group(0)
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    for name in ("webc", "webcoder-agent"):  # no webcoder-bridge: a missing one is skipped, not an error
        (venv / "bin" / name).write_text("#!/bin/sh\necho ran\n", encoding="utf-8", newline="\n")
        (venv / "bin" / name).chmod(0o755)
    run = subprocess.run([bash(), "-c", f"set -euo pipefail\nVENV={sh_quote(venv.as_posix())}\n{block}echo done"],
                         capture_output=True, timeout=60, env={**os.environ, "HOME": tmp_path.as_posix()})
    assert run.returncode == 0 and run.stdout.decode().strip().endswith("done"), run.stderr
    assert "webcoder-agent" in run.stdout.decode()  # not on PATH here: the hint names it too
    linked = tmp_path / ".local" / "bin"
    assert sorted(path.name for path in linked.iterdir()) == ["webc", "webcoder-agent"]


def test_installer_scripts_parse():
    # Parsed from UTF-8 text, the way Invoke-RestMethod hands it to [scriptblock]::Create.
    result = run_powershell(
        "$errors = $null; $text = [System.IO.File]::ReadAllText("
        f"{ps_quote(str(INSTALLERS / 'install-agent.ps1'))}, [System.Text.Encoding]::UTF8); "
        "[void][System.Management.Automation.Language.Parser]::ParseInput($text, [ref]$null, [ref]$errors); "
        "if ($errors) { $errors | ForEach-Object { $_.ToString() }; exit 1 }")
    assert result.returncode == 0, result.stdout.decode("utf-8", "replace")
    checked = subprocess.run([bash(), "-n", (INSTALLERS / "install-agent.sh").as_posix()], capture_output=True)
    assert checked.returncode == 0, checked.stderr


def test_pairing_checks_folders_before_using_up_the_code(tmp_path):
    config_path = tmp_path / "agent" / "config.json"
    with pytest.raises(SystemExit) as exc:  # port 9: were the hub contacted first, this would fail differently
        pair_device("http://127.0.0.1:9", "ABCD-EFGH", [str(tmp_path / "missing")], config_path, proxy="none")
    assert "not an existing folder" in str(exc.value) and not config_path.exists()


def test_https_compose_mounts_the_real_caddyfile():
    project = INSTALLERS.parents[2]
    text = (project / "deploy" / "compose.https.yml").read_text(encoding="utf-8")
    mount = re.search(r"-\s*(\S+):/etc/caddy/Caddyfile", text).group(1)
    assert (project / mount).is_file()  # compose resolves it against the first -f file: the repo root


def test_linux_bound_files_use_lf():
    """A CR breaks `set -euo pipefail` and systemd units; Windows editors like to add them."""
    project = INSTALLERS.parents[2]
    for path in (INSTALLERS / "install-agent.sh", project / "deploy" / "install-hub-native.sh",
                 project / "deploy" / "webcoder-hub.service"):
        assert b"\r" not in path.read_bytes(), path


def test_installers_are_served_as_utf8(hub):
    for name, media in (("install-agent.ps1", "text/plain"), ("install-agent.sh", "text/x-shellscript")):
        response = hub.http.get(f"/downloads/{name}")
        assert response.status_code == 200 and response.headers["content-type"] == f"{media}; charset=utf-8"
        assert response.content == (INSTALLERS / name).read_bytes()


def test_device_setup_is_kept_and_commands_generated(hub, start_agent, tmp_path):
    root = tmp_path / "work"
    created = hub.panel.post("/api/devices", json={"name": "nas", "platform": "linux", "roots": ["/srv/data"],
                                                   "read_only": True, "autostart": False}).json()
    assert created["setup"] == {"platform": "linux", "roots": ["/srv/data"], "read_only": True, "autostart": False,
                                "mobile": False}
    assert "--read-only --no-autostart" in created["commands"]["one_click"]
    refused = hub.panel.post("/api/devices", json={"name": "x", "platform": "windows", "roots": ["relative"]})
    assert refused.status_code == 400 and refused.json()["error"]["code"] == "INVALID_ROOT"

    device_id, _ = start_agent("pc", root)
    hub.wait_for(lambda: hub.device(device_id)["info"].get("hostname"), what="agent status")
    hub.panel.post(f"/api/devices/{device_id}/roots-command", json={"roots": [str(root)]}).raise_for_status()
    hub.runtime.store.execute("UPDATE devices SET last_seen=0 WHERE id=?", (device_id,))
    agent_main(["--config", str(tmp_path / "agent-pc" / "config.json"), "read-only", "off"])  # triggers a status
    hub.wait_for(lambda: hub.device(device_id)["last_seen"] > 0, timeout=20, what="a fresh agent status")
    info = hub.device(device_id)["info"]  # the status report merged in; the setup saved in the panel survived
    assert info["setup"]["roots"] == [str(root)] and info["allowed_roots"] == [str(root.resolve())]
    again = hub.panel.post(f"/api/devices/{device_id}/pairing-code").json()
    local = {"win32": "windows", "darwin": "macos"}.get(sys.platform, "linux")
    assert again["setup"]["platform"] == local and again["setup"]["roots"] == [str(root)]


def test_agent_roots_cli(tmp_path):
    config_path = tmp_path / "agent" / "config.json"
    AgentConfig(hub_url="http://127.0.0.1:1", device_id="dev_x", device_secret="c2VjcmV0",
                allowed_roots=[], path=config_path).save()
    base = ["--config", str(config_path)]
    new = tmp_path / "new folder" / "代码"
    with pytest.raises(SystemExit):
        agent_main(base + ["roots", "set", str(new)])  # missing without --create
    with pytest.raises(SystemExit):
        agent_main(base + ["roots", "add", "relative/path", "--create"])
    assert agent_main(base + ["roots", "set", "--create", str(new), str(tmp_path)]) == 0
    assert new.is_dir()
    assert AgentConfig.load(config_path).allowed_roots == [str(new.resolve()), str(tmp_path.resolve())]
    assert agent_main(base + ["roots", "remove", str(tmp_path)]) == 0
    assert agent_main(base + ["read-only", "on"]) == 0
    reloaded = AgentConfig.load(config_path)
    assert reloaded.allowed_roots == [str(new.resolve())] and reloaded.read_only is True
    assert agent_main(base + ["roots", "set"]) == 0
    assert AgentConfig.load(config_path).allowed_roots == []


def test_running_agent_applies_folder_changes_and_repairing(hub, start_agent, tmp_path):
    device_id, config = start_agent("pc", tmp_path / "work")
    extra = tmp_path / "extra"
    assert agent_main(["--config", str(config.path), "roots", "add", "--create", str(extra)]) == 0
    hub.wait_for(lambda: str(extra.resolve()) in hub.device(device_id)["info"]["allowed_roots"], timeout=20,
                 what="agent to pick up the new folder")
    hub.add_project("extra", device_id, extra)
    assert agent_main(["--config", str(config.path), "read-only", "on"]) == 0
    hub.wait_for(lambda: hub.device(device_id)["info"]["read_only"] is True, timeout=20, what="read-only switch")

    code = hub.panel.post(f"/api/devices/{device_id}/pairing-code").json()["code"]
    pair_device(hub.url, code, [], Path(config.path), proxy="none")  # new secret while the agent is running
    connected = lambda: sum(1 for row in hub.runtime.store.all(  # noqa: E731
        "SELECT action FROM audit WHERE target=? AND action='device.connected'", (device_id,)))
    hub.wait_for(lambda: connected() >= 2 and hub.device(device_id)["online"], timeout=30,
                 what="agent to reconnect with the new credentials")
    assert os.path.exists(config.path)


def test_a_new_device_does_not_inherit_the_old_ones_access(hub, tmp_path):
    """Re-pairing the same device keeps its folders, phones and servers; pairing as another device (e.g. added
    again after deleting the old one) starts without them. Local preferences carry over either way."""
    config_path = tmp_path / "agent" / "config.json"
    old_root, new_root = tmp_path / "old", tmp_path / "new"
    old_root.mkdir()
    new_root.mkdir()
    first = hub.add_device("pc")
    config = pair_device(hub.url, first["code"], [str(old_root)], config_path, proxy="none")
    config.mobile, config.ssh_hosts, config.ssh_command, config.max_write_mb = True, ["web-1"], "C:/ssh.exe", 64
    config.save()
    same = hub.panel.post(f"/api/devices/{first['device_id']}/pairing-code").json()["code"]
    kept = pair_device(hub.url, same, [], config_path, proxy="none")
    assert kept.allowed_roots == [str(old_root.resolve())] and kept.mobile and kept.ssh_hosts == ["web-1"]

    hub.panel.delete(f"/api/devices/{first['device_id']}").raise_for_status()
    other = pair_device(hub.url, hub.add_device("pc-again")["code"], [str(new_root)], config_path, proxy="none")
    assert other.allowed_roots == [str(new_root.resolve())] and not other.mobile and other.ssh_hosts == []
    assert other.ssh_command == "C:/ssh.exe" and other.max_write_mb == 64 and other.proxy == "none"


def test_uninstalling_stops_only_this_installations_agent(tmp_path):
    """An agent run on purpose with another --config is another device: the uninstall command leaves it running.
    Checked with stand-ins for the process tools, never against this computer's real agents."""
    from tests.conftest import posix_shell
    from webcoder.hub.install import _POSIX_STOP, UNINSTALL

    stand_ins = ('pgrep() { printf "%s\\n" 101 102 103 104; }; '
                 'ps() { case "$4" in '
                 '101) echo "/x/venv/bin/python -m webcoder.agent --config $HOME/.webcoder-agent/config.json run";; '
                 '102) echo "/x/venv/bin/python -m webcoder.agent --config /other/config.json run";; '
                 '103) echo "/x/venv/bin/python -m webcoder.agent run";; '
                 '104) echo "python -m webcoder.agent --config=/elsewhere.json run";; esac; }; '
                 'kill() { echo "stopped $1"; }; ')
    assert all(_POSIX_STOP in UNINSTALL[name] for name in ("macos", "linux"))
    done = subprocess.run([posix_shell(), "-c", stand_ins + _POSIX_STOP], capture_output=True, text=True,
                          env={**os.environ, "HOME": str(tmp_path)}, timeout=60)
    assert done.stdout.split("\n")[:-1] == ["stopped 101", "stopped 103"], done.stdout + done.stderr
    # the installer stops the agent it upgrades the same way (its own config, or its venv with no --config)
    script = (Path(__file__).parents[1] / "webcoder" / "hub" / "installers" / "install-agent.sh").read_text(
        encoding="utf-8")
    ours = re.search(r"^ours\(\) \{\n.*?^\}\n", script, re.S | re.M).group(0)
    setup = 'VENV=/x/venv; CONFIG="$HOME/.webcoder-agent/config.json"; '
    found = subprocess.run([posix_shell(), "-c", stand_ins + setup + ours + "ours"], capture_output=True, text=True,
                           env={**os.environ, "HOME": str(tmp_path)}, timeout=60)
    assert found.stdout.split() == ["101", "103"], found.stdout + found.stderr
    if os.name != "nt":
        return
    command = UNINSTALL["windows"]
    real_query = "Get-CimInstance Win32_Process -Filter \"Name like 'python%'\""
    real_stop = "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
    assert real_query in command and real_stop in command
    script = command[command.index("$cfg = "):command.index("; Start-Sleep")]
    script = script.replace(real_query, "$procs").replace(real_stop, "ForEach-Object { $_.ProcessId }")
    assert "Stop-Process" not in script and "Get-CimInstance" not in script  # nothing real is touched
    default = os.path.join(os.environ["USERPROFILE"], ".webcoder-agent", "config.json")
    lines = {101: f'"C:\\py\\python.exe" -m webcoder.agent --config "{default}" run',
             102: '"C:\\py\\python.exe" -m webcoder.agent --config "D:\\other\\config.json" run',
             103: '"C:\\py\\python.exe" -m webcoder.agent run', 104: '"C:\\py\\python.exe" -m http.server'}
    procs = "$procs = @(" + ", ".join(f"[pscustomobject]@{{ProcessId={pid}; CommandLine='{line}'}}"
                                      for pid, line in lines.items()) + "); "
    shown = subprocess.run(["powershell.exe", "-NoProfile", "-Command", procs + script], capture_output=True,
                           text=True, timeout=120)
    assert shown.stdout.split() == ["101", "103"], shown.stdout + shown.stderr
