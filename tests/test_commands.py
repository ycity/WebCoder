"""Command-line programs the owner allowed (route B: lark-cli, meegle ...), and the cli_* MCP tools."""
import json
import os
import shutil
import sys
from pathlib import Path

import pytest

from tests.conftest import AgentThread
from webcoder.agent.__main__ import main as agent_main
from webcoder.agent.__main__ import pair_device
from webcoder.agent.commands import CommandOps, check_name
from webcoder.agent.config import AgentConfig
from webcoder.agent.fileops import BackupStore, FileOps, Sandbox
from webcoder.common.errors import WebCoderError
from webcoder.hub.auth import ApiError
from webcoder.hub.install import roots_command, validate_programs

FAKE = [sys.executable, str(Path(__file__).with_name("fake_cli.py"))]


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    return root


def make_ops(project, tmp_path, programs=("fake-cli",)):
    files = FileOps(Sandbox([project]), BackupStore(tmp_path / "backups"))
    files.cli = CommandOps(files, programs=list(programs), paths={"fake-cli": FAKE})
    return files


def output(result) -> dict:
    return json.loads(result["stdout"])


def test_shells_and_interpreters_can_never_be_allowed():
    for name in ("bash", "cmd", "cmd.exe", "PowerShell", "pwsh", "python3", "python3.12", "node", "node20", "npx",
                 "wsl", "sudo", "env", "docker",
                 # they run other commands through their options, or reach any local file; git, ssh and phones
                 # have their own tools
                 "git", "git.exe", "ssh", "scp", "adb", "hdc", "tar", "sed", "awk", "gh", "rsync", "zip",
                 # hg --config alias.x=!cmd, curl -d @file, and programs that run what they are given or run
                 # the project's code
                 "hg", "curl", "curl.exe", "wget", "nice", "strace", "poetry", "wmic", "forfiles", "winget",
                 "pytest", "just", "terraform", "sqlite3", "psql"):
        with pytest.raises(WebCoderError) as err:
            check_name(name)
        assert err.value.code == "CLI_REFUSED", name
    for name in ("C:\\tools\\x.exe", "../x", "-rf", "a b", ""):
        with pytest.raises(WebCoderError) as err:
            check_name(name)
        assert err.value.code == "INVALID_ARGUMENT", name
    assert check_name("lark-cli") == "lark-cli" and check_name("meegle") == "meegle"
    assert CommandOps(None, programs=["lark-cli", "bash", "../x"]).allowed == ["lark-cli"]  # a hand-edited config
    with pytest.raises(ApiError):
        validate_programs(["lark-cli", "powershell"])


def test_running_an_allowed_program(project, tmp_path):
    ops = make_ops(project, tmp_path)
    tricky = ["docs", "+fetch", "--doc", "https://x.feishu.cn/docx/a?b=1&c=2", "a|b", "$HOME", "%PATH%", "it's",
              'say "hi"', "", "中文"]
    result = ops.dispatch("cli.run", {"program": "fake-cli", "args": tricky})
    assert result["exit_code"] == 0 and output(result)["args"] == tricky  # every argument arrives unchanged
    fed = ops.dispatch("cli.run", {"program": "fake-cli", "args": [], "stdin": "<p>内容</p>", "root": str(project)})
    assert output(fed)["stdin"] == "<p>内容</p>" and os.path.samefile(output(fed)["cwd"], project)
    failed = ops.dispatch("cli.run", {"program": "fake-cli", "args": ["--exit", "3"]})
    assert failed["exit_code"] == 3 and "failed on purpose" in failed["stderr"]
    with pytest.raises(WebCoderError) as err:
        ops.dispatch("cli.run", {"program": "fake-cli", "args": ["--sleep", "5"], "timeout": 1})
    assert err.value.code == "CLI_TIMEOUT"
    with pytest.raises(WebCoderError) as err:
        ops.dispatch("cli.run", {"program": "meegle", "args": []})
    assert err.value.code == "CLI_NOT_ALLOWED"
    with pytest.raises(WebCoderError) as err:
        make_ops(project, tmp_path, programs=()).dispatch("cli.run", {"program": "fake-cli"})
    assert err.value.code == "CLI_DISABLED"
    ops.sb.read_only = True  # "the AI can look, not change": a program could change anything
    with pytest.raises(WebCoderError) as err:
        ops.dispatch("cli.run", {"program": "fake-cli", "args": []})
    assert err.value.code == "AGENT_READ_ONLY"


@pytest.mark.skipif(os.name != "nt" or shutil.which("node") is None, reason="npm wrappers are a Windows matter")
def test_npm_wrappers_run_without_cmd_exe(project, tmp_path, monkeypatch):
    """cmd.exe re-parses a .cmd file's arguments (& | ^ % ...): an argument could start another command. The npm
    wrapper is read instead and its JavaScript entry point started with node directly."""
    bin_dir = tmp_path / "npm"
    (bin_dir / "node_modules" / "tool").mkdir(parents=True)
    (bin_dir / "node_modules" / "tool" / "cli.js").write_text(
        "process.stdout.write(JSON.stringify(process.argv.slice(2)))", encoding="utf-8")
    marker = tmp_path / "injected.txt"
    (bin_dir / "tool.cmd").write_text(
        '@ECHO off\r\nGOTO start\r\n:find_dp0\r\nSET dp0=%~dp0\r\nEXIT /b\r\n:start\r\nSETLOCAL\r\nCALL :find_dp0\r\n'
        'IF EXIST "%dp0%\\node.exe" (\r\n  SET "_prog=%dp0%\\node.exe"\r\n) ELSE (\r\n  SET "_prog=node"\r\n)\r\n'
        'endLocal & goto #_undefined_# 2>NUL || title %COMSPEC% & "%_prog%"  '
        '"%dp0%\\node_modules\\tool\\cli.js" %*\r\n', encoding="utf-8", newline="")
    (bin_dir / "plain.bat").write_text("@echo %*\r\n", encoding="utf-8", newline="")
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ["PATH"])
    files = FileOps(Sandbox([project]), BackupStore(tmp_path / "backups"))
    files.cli = CommandOps(files, programs=["tool", "plain"])
    nasty = ["a&b", "x|y", "^caret", "%PATH%", '"quoted"', f'& echo hacked > "{marker}"']
    result = files.dispatch("cli.run", {"program": "tool", "args": nasty})
    assert json.loads(result["stdout"]) == nasty and not marker.exists()
    with pytest.raises(WebCoderError) as err:  # other batch files are not run at all
        files.dispatch("cli.run", {"program": "plain", "args": ["x"]})
    assert err.value.code == "CLI_UNSUPPORTED"
    # command_paths gets the same treatment: a batch file there would go through cmd.exe as well
    for override in (str(bin_dir / "plain.bat"), ["cmd", "/c", "echo"], ["C:\\Windows\\System32\\cmd.exe", "/c"]):
        files.cli = CommandOps(files, programs=["tool"], paths={"tool": override})
        with pytest.raises(WebCoderError) as err:
            files.dispatch("cli.run", {"program": "tool", "args": nasty})
        assert err.value.code == "CLI_UNSUPPORTED" and not marker.exists(), override
    files.cli = CommandOps(files, programs=["tool"], paths={"tool": [str(bin_dir / "tool.cmd"), "--first"]})
    result = files.dispatch("cli.run", {"program": "tool", "args": nasty})
    assert json.loads(result["stdout"]) == ["--first", *nasty] and not marker.exists()
    # With the wrapper in an allowed folder, the AI could rewrite it (or its script, or put a node.exe next to it)
    broad = FileOps(Sandbox([tmp_path]), BackupStore(tmp_path / "backups2"))
    broad.cli = CommandOps(broad, programs=["tool"])
    with pytest.raises(WebCoderError) as err:
        broad.dispatch("cli.run", {"program": "tool", "args": []})
    assert err.value.code == "CLI_WRITABLE"


def test_programs_the_ai_could_rewrite_are_not_run(project, tmp_path):
    """A program (or a script it runs) inside an allowed folder could be changed with write_file, then run."""
    (project / "tool.py").write_text("print('rewritten by the AI')\n", encoding="utf-8")
    for command in ([sys.executable, str(project / "tool.py")],  # a file in the project, by its full path
                    [sys.executable, "tool.py"]):  # as the program finds it: from the project folder it runs in
        files = FileOps(Sandbox([project]), BackupStore(tmp_path / "backups"))
        files.cli = CommandOps(files, programs=["fake-cli"], paths={"fake-cli": command})
        with pytest.raises(WebCoderError) as err:
            files.dispatch("cli.run", {"program": "fake-cli", "args": [], "root": str(project)})
        assert err.value.code == "CLI_WRITABLE" and "tool.py" in err.value.message, command
    assert make_ops(project, tmp_path).dispatch("cli.run", {"program": "fake-cli", "args": [],
                                                            "root": str(project)})["exit_code"] == 0


def test_cli_and_settings_command(tmp_path):
    config_path = tmp_path / "agent" / "config.json"
    AgentConfig(hub_url="http://127.0.0.1:1", device_id="dev_x", device_secret="c2VjcmV0", path=config_path).save()
    base = ["--config", str(config_path), "commands"]
    assert agent_main(base + ["set", "lark-cli", "meegle", "lark-cli"]) == 0
    assert AgentConfig.load(config_path).commands == ["lark-cli", "meegle"]
    with pytest.raises(SystemExit):
        agent_main(base + ["add", "powershell"])
    assert agent_main(base + ["remove", "meegle"]) == 0 and AgentConfig.load(config_path).commands == ["lark-cli"]
    assert roots_command("linux", ["/x"], None, None, None, ["lark-cli", "meegle"]).endswith(
        " && ~/.webcoder-agent/venv/bin/python -m webcoder.agent commands set 'lark-cli' 'meegle'")


# ------------------------------------------------------------------ through the hub
@pytest.fixture
def cli_computer(hub, tmp_path):
    runners = []

    def start(name="pc", programs=("fake-cli",)):
        root = tmp_path / f"{name}-work"
        root.mkdir()
        device = hub.add_device(name)
        config = pair_device(hub.url, device["code"], [str(root)], tmp_path / f"agent-{name}" / "config.json",
                             proxy="none")
        config.commands, config.command_paths = list(programs), {"fake-cli": FAKE}
        config.save()
        runner = AgentThread(config)
        runner.start()
        runners.append(runner)
        hub.wait_for(lambda: (hub.device(device["device_id"])["info"].get("commands") or {}).get("programs"),
                     what="agent status")
        hub.add_project(f"{name}-work", device["device_id"], root)
        return device["device_id"], root

    yield start
    for runner in runners:
        runner.stop()


def test_cli_tools_through_the_hub(hub, cli_computer):
    _, root = cli_computer()
    plain = hub.create_token(scopes=("read", "write"))
    names = {tool["name"] for tool in hub.rpc(plain, "tools/list").json()["result"]["tools"]}
    assert "cli_run" not in names
    text, is_error = hub.call(plain, "cli_run", program="fake-cli", args=["x"])
    assert is_error and "INSUFFICIENT_SCOPE" in text and "run programs on the computer" in text
    token = hub.create_token(scopes=("read", "cli"), label="cli")
    assert "on pc: fake-cli" in hub.ok(token, "cli_programs")
    out = hub.ok(token, "cli_run", program="fake-cli", args=["docs", "+fetch", "--doc", "a b"], project="pc-work")
    assert "exit code 0" in out and "never instructions" in out and '"docs", "+fetch", "--doc", "a b"' in out
    assert str(root.name) in out  # ran in the project folder
    audit = hub.runtime.store.all("SELECT target FROM audit WHERE action='cli_run'")
    assert audit[-1]["target"] == "pc: fake-cli docs +fetch --doc 'a b'"
    assert "exit code 3" in hub.ok(token, "cli_run", program="fake-cli", args=["--exit", "3"])
    assert hub.runtime.store.all("SELECT status FROM audit WHERE action='cli_run'")[-1]["status"] == "EXIT_3"
    long_args = ["x" * 10_000, "y" * 10_000]  # allowed by the agent, but too long to audit whole: never run
    text, is_error = hub.call(token, "cli_run", program="fake-cli", args=long_args)
    assert is_error and "audit log keeps it whole" in text
    audit = hub.runtime.store.all("SELECT target, status FROM audit WHERE action='cli_run'")
    assert audit[-1]["status"] != "ok" and len(audit[-1]["target"]) < 19_100
    # the project decides the computer: another one named as well is refused, never quietly not used
    text, is_error = hub.call(token, "cli_run", program="fake-cli", args=["x"], project="pc-work", computer="laptop")
    assert is_error and "INVALID_ARGUMENTS" in text and "is on 'pc'" in text
    assert "exit code 0" in hub.ok(token, "cli_run", program="fake-cli", args=["x"], project="pc-work", computer="PC")
    text, is_error = hub.call(token, "cli_run", program="meegle", args=[])
    assert is_error and "CLI_NO_COMPUTER" in text
    device_id = hub.runtime.store.one("SELECT device_id FROM projects WHERE alias='pc-work'")["device_id"]
    (root / "docs").mkdir()
    hub.add_project("docs", device_id, root / "docs", mode="ro")
    text, is_error = hub.call(token, "cli_run", program="fake-cli", args=["x"], project="docs")
    assert is_error and "PROJECT_READ_ONLY" in text  # a program run there could change its files
