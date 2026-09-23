"""Agent configuration file (default: ~/.webcoder-agent/config.json)."""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

from webcoder.agent.commands import CommandOps
from webcoder.agent.fileops import DEFAULT_DENY, BackupStore, FileOps, Limits, MiB, Sandbox, make_private_dir
from webcoder.agent.gitops import GitOps
from webcoder.agent.mobile import MobileOps
from webcoder.agent.remote import SshOps
from webcoder.agent.skills import SkillOps


def default_home() -> Path:
    return Path(os.environ.get("WEBCODER_AGENT_HOME") or (Path.home() / ".webcoder-agent"))


def default_config_path() -> Path:
    return default_home() / "config.json"


@dataclass
class AgentConfig:
    hub_url: str
    device_id: str
    device_secret: str
    allowed_roots: list[str] = field(default_factory=list)
    read_only: bool = False
    proxy: str = "system"          # "system" (environment proxies), "none", or a proxy URL
    deny: list[str] | None = None  # None means the built-in secret deny list
    max_write_mb: int = 16
    backup_keep: int = 20
    retired: bool = False          # the device was deleted in the hub panel; pairing again resets it
    mobile: bool = False           # let the hub control phones attached here (adb / hdc); set only on this device
    mobile_tools: dict | None = None  # optional {"adb": path, "hdc": path} when they are not found automatically
    ssh_hosts: list[str] = field(default_factory=list)  # servers the hub may run commands on (ssh config names)
    ssh_command: str | None = None  # optional path of the ssh client when it is not on PATH
    git_command: str | None = None  # optional path of git when it is not on PATH
    skills: bool = False           # share user skills (~/.codex/skills ...) read-only; set only on this device
    skill_dirs: list[str] | None = None  # None: ~/.codex/skills, ~/.claude/skills, ~/.agents/skills
    commands: list[str] = field(default_factory=list)  # programs the hub may run here (e.g. lark-cli); set here only
    command_paths: dict | None = None  # optional {"name": path or [argv...]} for programs not found on PATH
    path: Path | None = field(default=None, compare=False, repr=False)

    @property
    def home(self) -> Path:
        return (self.path or default_config_path()).parent

    @classmethod
    def load(cls, path) -> AgentConfig:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        known = {name for name in cls.__dataclass_fields__ if name != "path"}
        config = cls(**{key: value for key, value in data.items() if key in known})
        config.path = Path(path)
        return config

    def save(self) -> Path:
        path = Path(self.path or default_config_path())
        make_private_dir(path.parent)  # it also holds edit backups and logs
        payload = {key: value for key, value in asdict(self).items() if key != "path"}
        tmp = path.with_name(path.name + ".tmp")
        # The file holds the device secret: create it private (0600 on POSIX).
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        self.path = path
        return path

    def build_fileops(self) -> FileOps:
        limit = max(1, int(self.max_write_mb)) * MiB
        sandbox = Sandbox(self.allowed_roots, deny=self.deny if self.deny is not None else DEFAULT_DENY,
                          read_only=self.read_only, protected_dirs=[str(self.home)])
        backups = BackupStore(self.home / "backups", keep_per_file=self.backup_keep)
        ops = FileOps(sandbox, backups, Limits(max_write_bytes=limit, max_transfer_bytes=limit))
        ops.mobile = MobileOps(ops, enabled=bool(self.mobile), tools=self.mobile_tools)
        ops.ssh = SshOps(ops, hosts=self.ssh_hosts, command=self.ssh_command)
        ops.git = GitOps(ops, command=self.git_command, hooks_dir=self.home / "no-hooks")
        ops.skills = SkillOps(ops, enabled=bool(self.skills), dirs=self.skill_dirs)
        ops.cli = CommandOps(ops, programs=self.commands, paths=self.command_paths)
        return ops
