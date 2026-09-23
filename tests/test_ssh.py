"""SSH to the owner's servers (against a fake ssh client): agent operations, boundaries, and the MCP tools."""
import io
import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

from tests.conftest import AgentThread, posix_shell
from webcoder.agent import procs, remote
from webcoder.agent.__main__ import main as agent_main
from webcoder.agent.__main__ import pair_device
from webcoder.agent.config import AgentConfig
from webcoder.agent.fileops import BackupStore, FileOps, Sandbox
from webcoder.agent.remote import SshOps
from webcoder.common.errors import WebCoderError
from webcoder.hub.install import roots_command

FAKE_SSH = [sys.executable, str(Path(__file__).with_name("fake_ssh.py"))]


@pytest.fixture
def servers(tmp_path, monkeypatch):
    state = tmp_path / "fake-ssh"
    monkeypatch.setenv("WEBCODER_FAKE_SSH", str(state))
    monkeypatch.setenv("WEBCODER_FAKE_SH", posix_shell())  # the agent's server-side scripts run for real

    def calls() -> list[list[str]]:
        path = state / "calls.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []

    return type("Servers", (), {"calls": staticmethod(calls), "root": state / "servers"})


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    return root


def make_ops(project, tmp_path, hosts=("web-1", "unreachable", "needs-password")):
    files = FileOps(Sandbox([project]), BackupStore(tmp_path / "backups"))
    files.ssh = SshOps(files, hosts=list(hosts), command=FAKE_SSH)
    return files


def code(excinfo) -> str:
    return excinfo.value.code


def test_off_until_servers_are_listed_on_the_computer(project, tmp_path, servers):
    ops = make_ops(project, tmp_path, hosts=())
    with pytest.raises(WebCoderError) as err:
        ops.dispatch("ssh.exec", {"host": "web-1", "command": "hostname"})
    assert code(err) == "SSH_DISABLED" and servers.calls() == []


def test_a_read_only_device_runs_nothing_on_servers(project, tmp_path, servers):
    """No server command can be told to be only a read, and transfers write: a read-only device lists servers only."""
    files = FileOps(Sandbox([project], read_only=True), BackupStore(tmp_path / "backups"))
    files.ssh = SshOps(files, hosts=["web-1"], command=FAKE_SSH)
    (project / "a.txt").write_bytes(b"x\n")
    for op, args in (("ssh.exec", {"host": "web-1", "command": "cat /etc/hostname"}),
                     ("ssh.upload", {"host": "web-1", "root": str(project), "path": "a.txt", "remote_path": "/tmp/a"}),
                     ("ssh.download", {"host": "web-1", "remote_path": "/tmp/a", "root": str(project),
                                       "path": "b.txt"})):
        with pytest.raises(WebCoderError) as err:
            files.dispatch(op, args)
        assert code(err) == "AGENT_READ_ONLY", op
    assert servers.calls() == [] and files.dispatch("ssh.hosts", {})["hosts"]


def test_only_listed_hosts_and_no_option_injection(project, tmp_path, servers):
    ops = make_ops(project, tmp_path)
    with pytest.raises(WebCoderError) as err:
        ops.dispatch("ssh.exec", {"host": "db-9", "command": "hostname"})
    assert code(err) == "SSH_HOST_NOT_ALLOWED"
    for bad in ("-oProxyCommand=calc", "web-1 hostname", "", "web-1;id"):
        with pytest.raises(WebCoderError) as err:
            ops.dispatch("ssh.exec", {"host": bad, "command": "hostname"})
        assert code(err) == "INVALID_ARGUMENT"
    assert servers.calls() == []  # none of these reached ssh


def test_exec_is_non_interactive_and_reports_everything(project, tmp_path, servers):
    ops = make_ops(project, tmp_path)
    r = ops.dispatch("ssh.exec", {"host": "web-1", "command": "echo hello 'from server'"})
    assert r["exit_code"] == 0 and r["stdout"] == "hello from server\n" and r["stderr"] == ""
    argv = servers.calls()[-1]
    options = " ".join(argv)
    assert "BatchMode=yes" in options and "ClearAllForwardings=yes" in options and "PermitLocalCommand=no" in options
    assert argv[-3:] == ["--", "web-1", "echo hello 'from server'"]  # the host can never be read as an option
    failed = ops.dispatch("ssh.exec", {"host": "web-1", "command": "fail 3"})
    assert failed["exit_code"] == 3 and failed["stderr"] == "boom\n"


def test_connection_problems_are_explained(project, tmp_path, servers):
    ops = make_ops(project, tmp_path)
    with pytest.raises(WebCoderError) as err:
        ops.dispatch("ssh.exec", {"host": "unreachable", "command": "hostname"})
    assert code(err) == "SSH_CONNECTION_FAILED" and "Could not resolve hostname" in err.value.message
    with pytest.raises(WebCoderError) as err:
        ops.dispatch("ssh.exec", {"host": "needs-password", "command": "hostname"})
    assert code(err) == "SSH_CONNECTION_FAILED" and "passwords cannot be typed here" in err.value.message
    with pytest.raises(WebCoderError) as err:
        ops.dispatch("ssh.exec", {"host": "web-1", "command": "sleep 5", "timeout": 1})
    assert code(err) == "SSH_TIMEOUT"


def test_files_move_between_project_and_server(project, tmp_path, servers):
    ops = make_ops(project, tmp_path)
    (project / "nginx.conf").write_bytes(b"server {}\n")
    (project / ".env").write_text("SECRET=1", encoding="utf-8")
    up = ops.dispatch("ssh.upload", {"host": "web-1", "root": str(project), "path": "nginx.conf",
                                     "remote_path": "/etc/nginx/nginx.conf"})
    assert up["size"] == 10 and (servers.root / "web-1" / "etc/nginx/nginx.conf").read_bytes() == b"server {}\n"
    (project / "nginx.conf").write_bytes(b"server { listen 80; }\n")
    with pytest.raises(WebCoderError) as err:  # the server's file has no backup: never replaced by accident
        ops.dispatch("ssh.upload", {"host": "web-1", "root": str(project), "path": "nginx.conf",
                                    "remote_path": "/etc/nginx/nginx.conf"})
    assert code(err) == "DESTINATION_EXISTS"
    ops.dispatch("ssh.upload", {"host": "web-1", "root": str(project), "path": "nginx.conf",
                                "remote_path": "/etc/nginx/nginx.conf", "overwrite": True})
    assert "listen 80" in (servers.root / "web-1" / "etc/nginx/nginx.conf").read_text()
    with pytest.raises(WebCoderError) as err:  # the deny list still applies
        ops.dispatch("ssh.upload", {"host": "web-1", "root": str(project), "path": ".env", "remote_path": "/tmp/x"})
    assert code(err) == "PATH_DENIED"

    down = ops.dispatch("ssh.download", {"host": "web-1", "remote_path": "/etc/nginx/nginx.conf",
                                         "root": str(project), "path": "from-server/nginx.conf"})
    assert down["size"] == 22 and "listen 80" in (project / "from-server" / "nginx.conf").read_text()
    with pytest.raises(WebCoderError) as err:  # never overwrites a project file
        ops.dispatch("ssh.download", {"host": "web-1", "remote_path": "/etc/nginx/nginx.conf",
                                      "root": str(project), "path": "nginx.conf"})
    assert code(err) == "DESTINATION_EXISTS"
    with pytest.raises(WebCoderError) as err:
        ops.dispatch("ssh.download", {"host": "web-1", "remote_path": "/missing", "root": str(project),
                                      "path": "missing.txt"})
    assert code(err) == "SSH_COMMAND_FAILED" and not (project / "missing.txt").exists()
    assert not list(project.rglob(".webcoder-*.tmp"))


def test_upload_never_replaces_what_appears_meanwhile(project, tmp_path, servers):
    ops = make_ops(project, tmp_path, hosts=("racy", "web-1"))
    (project / "app.conf").write_bytes(b"ours\n")
    with pytest.raises(WebCoderError) as err:  # created by someone else between the check and the write
        ops.dispatch("ssh.upload", {"host": "racy", "root": str(project), "path": "app.conf",
                                    "remote_path": "/etc/app.conf"})
    assert code(err) == "DESTINATION_EXISTS" and (servers.root / "racy" / "etc/app.conf").read_bytes() == b"theirs\n"
    assert all(call[-1].startswith("sh -c ") for call in servers.calls())  # POSIX sh, whatever the login shell
    link = servers.root / "web-1" / "etc" / "app.conf"
    link.parent.mkdir(parents=True)
    try:
        os.symlink(servers.root / "web-1" / "nowhere", link)
    except OSError:  # Windows without Developer Mode
        return
    with pytest.raises(WebCoderError) as err:  # writing through it would create its target
        ops.dispatch("ssh.upload", {"host": "web-1", "root": str(project), "path": "app.conf",
                                    "remote_path": "/etc/app.conf"})
    assert code(err) == "DESTINATION_EXISTS" and not (servers.root / "web-1" / "nowhere").exists()


def test_an_interrupted_overwrite_leaves_the_server_file_alone(project, tmp_path, servers):
    ops = make_ops(project, tmp_path, hosts=("flaky",))
    (servers.root / "flaky" / "etc").mkdir(parents=True)
    (servers.root / "flaky" / "etc" / "app.conf").write_bytes(b"old\n")
    (project / "app.conf").write_bytes(b"new version of the file\n")
    with pytest.raises(WebCoderError) as err:  # the connection drops half way through
        ops.dispatch("ssh.upload", {"host": "flaky", "root": str(project), "path": "app.conf",
                                    "remote_path": "/etc/app.conf", "overwrite": True})
    assert code(err) == "SSH_COMMAND_FAILED" and "was not changed" in err.value.message
    assert (servers.root / "flaky" / "etc" / "app.conf").read_bytes() == b"old\n"
    assert os.listdir(servers.root / "flaky" / "etc") == ["app.conf"]  # the temporary copy is gone


def test_huge_output_is_capped_while_it_arrives(project, tmp_path, servers):
    ops = make_ops(project, tmp_path)
    r = ops.dispatch("ssh.exec", {"host": "web-1", "command": "spam 5000000"})  # 5 MB: only the end is kept
    assert r["truncated"] and r["stdout"].endswith("xEND\n") and len(r["stdout"]) == remote.MAX_OUTPUT + 1


def test_download_stops_at_the_size_limit(project, tmp_path, servers, monkeypatch):
    monkeypatch.setattr(remote, "MAX_FILE", 1000)
    (servers.root / "web-1").mkdir(parents=True)
    (servers.root / "web-1" / "big.bin").write_bytes(b"x" * 5000)
    ops = make_ops(project, tmp_path)
    with pytest.raises(WebCoderError) as err:
        ops.dispatch("ssh.download", {"host": "web-1", "remote_path": "/big.bin", "root": str(project),
                                      "path": "big.bin"})
    assert code(err) == "FILE_TOO_LARGE" and not (project / "big.bin").exists()
    assert not list(project.rglob(".webcoder-*.tmp"))


def test_a_download_that_cannot_be_saved_is_not_kept(project, tmp_path, servers, monkeypatch):
    (servers.root / "web-1").mkdir(parents=True)
    (servers.root / "web-1" / "dump.sql").write_bytes(b"x" * 300000)

    class FullDisk(io.FileIO):  # the disk fills up after the first 64 KiB
        def write(self, data):
            if self.tell() >= 65536:
                raise OSError(28, "No space left on device")
            return super().write(data)

    monkeypatch.setattr(remote, "open", lambda path, mode: FullDisk(path, "w"), raising=False)
    ops = make_ops(project, tmp_path)
    with pytest.raises(WebCoderError) as err:
        ops.dispatch("ssh.download", {"host": "web-1", "remote_path": "/dump.sql", "root": str(project),
                                      "path": "dump.sql"})
    assert code(err) == "WRITE_FAILED" and "No space left" in err.value.message
    assert not (project / "dump.sql").exists() and not list(project.rglob(".webcoder-*.tmp"))


def test_cli_and_settings_command(tmp_path):
    config_path = tmp_path / "agent" / "config.json"
    AgentConfig(hub_url="http://127.0.0.1:1", device_id="dev_x", device_secret="c2VjcmV0",
                path=config_path).save()
    base = ["--config", str(config_path), "ssh-hosts"]
    assert agent_main(base + ["set", "web-1", "db-1", "web-1"]) == 0
    assert AgentConfig.load(config_path).ssh_hosts == ["web-1", "db-1"]
    assert agent_main(base + ["add", "cache-1"]) == 0 and agent_main(base + ["remove", "db-1"]) == 0
    assert AgentConfig.load(config_path).ssh_hosts == ["web-1", "cache-1"]
    with pytest.raises(SystemExit):
        agent_main(base + ["add", "-oProxyCommand=calc"])
    assert AgentConfig.load(config_path).build_fileops().info()["ssh"]["hosts"] == ["web-1", "cache-1"]
    assert roots_command("linux", ["/x"], None, ["web-1", "db-1"]).endswith(
        " && ~/.webcoder-agent/venv/bin/python -m webcoder.agent ssh-hosts set 'web-1' 'db-1'")
    assert roots_command("windows", ["C:\\x"], None, []).endswith("-m webcoder.agent ssh-hosts set }")


# --------------------------------------------------------------------------- through the hub
@pytest.fixture
def ssh_computer(hub, tmp_path, servers):
    runners = []

    def start(name="pc", hosts=("web-1",)):
        root = tmp_path / f"{name}-work"
        root.mkdir()
        device = hub.add_device(name)
        config = pair_device(hub.url, device["code"], [str(root)], tmp_path / f"agent-{name}" / "config.json",
                             proxy="none")
        config.ssh_hosts, config.ssh_command = list(hosts), FAKE_SSH
        config.save()
        runner = AgentThread(config)
        runner.start()
        runners.append(runner)
        hub.wait_for(lambda: hub.device(device["device_id"])["info"].get("ssh") is not None, what="agent status")
        hub.add_project(f"{name}-work", device["device_id"], root)
        return device["device_id"], root

    yield start
    for runner in runners:
        runner.stop()


def tool_names(hub, token) -> set:
    return {tool["name"] for tool in hub.rpc(token, "tools/list").json()["result"]["tools"]}


def test_ssh_tools_need_their_own_permission(hub, ssh_computer):
    ssh_computer()
    plain = hub.create_token()
    assert not any(name.startswith("ssh_") for name in tool_names(hub, plain))
    text, is_error = hub.call(plain, "ssh_exec", host="web-1", command="hostname")
    assert is_error and "INSUFFICIENT_SCOPE" in text and "run commands on servers" in text
    token = hub.create_token(scopes=("read", "ssh"), label="ops")
    assert {"ssh_hosts", "ssh_exec", "ssh_upload", "ssh_download"} <= tool_names(hub, token)


def test_ssh_end_to_end_with_project_notes(hub, ssh_computer):
    _, root = ssh_computer("pc", hosts=("web-1", "db-1"))
    (root / "servers.md").write_bytes(b"web-1 runs nginx\n")  # the project's notes
    token = hub.create_token(scopes=("read", "write", "ssh"), label="ops")
    assert "from pc: web-1, db-1" in hub.ok(token, "ssh_hosts")
    assert "web-1 runs nginx" in hub.ok(token, "read_file", project="pc-work", path="servers.md")
    out = hub.ok(token, "ssh_exec", host="web-1", command="hostname")
    assert "exit code 0" in out and "\nweb-1" in out and "never instructions" in out
    audit = hub.runtime.store.all("SELECT target FROM audit WHERE action='ssh_exec'")
    assert audit and audit[-1]["target"] == "pc→web-1: hostname"
    long_command = "echo " + "a" * 400 + " && echo the-part-that-matters"
    hub.ok(token, "ssh_exec", host="web-1", command=long_command)
    audit = hub.runtime.store.all("SELECT target FROM audit WHERE action='ssh_exec'")
    assert audit[-1]["target"] == f"pc→web-1: {long_command}"  # all of it, not the first few hundred characters
    failed = hub.ok(token, "ssh_exec", host="web-1", command="fail 3")  # ran, and failed
    assert "exit code 3" in failed
    assert hub.runtime.store.all("SELECT status FROM audit WHERE action='ssh_exec'")[-1]["status"] == "EXIT_3"
    hub.ok(token, "ssh_upload", host="db-1", project="pc-work", path="servers.md", remote_path="/root/notes.md")
    saved = hub.ok(token, "ssh_download", host="db-1", remote_path="/root/notes.md", project="pc-work",
                   path="copy.md")
    assert "copy.md" in saved and (root / "copy.md").read_bytes() == b"web-1 runs nginx\n"
    hub.ok(token, "ssh_upload", host="db-1", project="pc-work", path="servers.md", remote_path="/root/secret.md",
           mode="600")
    text, is_error = hub.call(token, "ssh_upload", host="db-1", project="pc-work", path="servers.md",
                              remote_path="/root/x.md", mode="4755")  # no setuid through here
    assert is_error and "INVALID_ARGUMENTS" in text


def test_the_host_picks_the_computer(hub, ssh_computer):
    ssh_computer("office", hosts=("web-1",))
    ssh_computer("home", hosts=("nas",))
    token = hub.create_token(scopes=("read", "ssh"), label="ops")
    assert "exit code 0" in hub.ok(token, "ssh_exec", host="nas", command="hostname")
    assert hub.runtime.store.all("SELECT target FROM audit WHERE action='ssh_exec'")[-1]["target"].startswith("home→")
    text, is_error = hub.call(token, "ssh_exec", host="web-1", command="hostname", computer="home")
    assert is_error and "SSH_HOST_NOT_ALLOWED" in text
    text, is_error = hub.call(token, "ssh_exec", host="db-9", command="hostname")  # neither computer lists it
    assert is_error and "SSH_HOST_NOT_ALLOWED" in text and "AMBIGUOUS" not in text
    text, is_error = hub.call(token, "ssh_exec", host="nas", command="sleep 5", timeout=1)
    assert is_error and "SSH_TIMEOUT" in text
    last = hub.runtime.store.all("SELECT target, status FROM audit WHERE action='ssh_exec'")[-1]
    assert last == {"target": "home→nas: sleep 5", "status": "SSH_TIMEOUT"}  # may still run there: keep the command


def test_disabling_the_device_stops_a_running_command(hub, ssh_computer):
    device_id, _ = ssh_computer()
    token = hub.create_token(scopes=("read", "ssh"), label="ops")
    outcome = {}
    call = threading.Thread(target=lambda: outcome.update(
        result=hub.call(token, "ssh_exec", host="web-1", command="sleep 60", timeout=120)))
    started = time.monotonic()
    call.start()
    hub.wait_for(lambda: procs.running() == 1, what="the ssh command to start")
    hub.panel.patch(f"/api/devices/{device_id}", json={"enabled": False}).raise_for_status()  # the kill switch
    hub.wait_for(lambda: procs.running() == 0, timeout=15, what="the agent to stop the command")
    call.join(15)
    assert time.monotonic() - started < 30  # not the 60 s the command wanted
    assert outcome["result"][1]  # the call reports an error instead of hanging


def test_panel_generates_the_server_list_command(hub, ssh_computer):
    device_id, _ = ssh_computer()
    ok = hub.panel.post(f"/api/devices/{device_id}/roots-command", json={"roots": [], "ssh_hosts": ["web-1", "db-1"]})
    assert ok.status_code == 200 and "ssh-hosts set 'web-1' 'db-1'" in ok.json()["command"].replace('"', "'")
    bad = hub.panel.post(f"/api/devices/{device_id}/roots-command", json={"roots": [], "ssh_hosts": ["-oProxy=x"]})
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "INVALID_HOST"


def test_pairing_again_keeps_the_server_list(hub, tmp_path):
    config_path = tmp_path / "agent" / "config.json"
    first = hub.add_device("pc")
    config = pair_device(hub.url, first["code"], [], config_path, proxy="none")
    config.ssh_hosts, config.ssh_command = ["web-1"], "C:/tools/ssh.exe"
    config.save()
    again = hub.panel.post(f"/api/devices/{first['device_id']}/pairing-code").json()
    repaired = pair_device(hub.url, again["code"], [], config_path, proxy="none")
    assert repaired.ssh_hosts == ["web-1"] and repaired.ssh_command == "C:/tools/ssh.exe"
