"""Skills (folders with a SKILL.md: instructions for a kind of task) on this computer, read-only.

User skills live outside the allowed folders (~/.codex/skills, ~/.claude/skills, ~/.agents/skills), so they
are only shared when the owner turns it on here (``webcoder-agent skills on``); the hub cannot. Only those
skill folders are readable, never the rest of ~/.codex or ~/.claude (logins, settings, conversation logs),
and the deny list still applies inside them. Skills inside projects (.codex/skills, .claude/skills,
.agents/skills) are listed too; they are project files anyway.
"""
from __future__ import annotations

import inspect
import os
import re
from pathlib import Path

from webcoder.agent.fileops import Sandbox, is_within
from webcoder.common.errors import WebCoderError

DEFAULT_DIRS = ("~/.codex/skills", "~/.claude/skills", "~/.agents/skills")
PROJECT_DIRS = (".codex/skills", ".claude/skills", ".agents/skills")
MAX_SKILLS = 500
MAX_READ = 1024 * 1024
MAX_FILES = 300
_KEY = re.compile(r"^([A-Za-z_][\w-]*):\s*(.*)$")


def front_matter(text: str) -> dict:
    """name / description from a SKILL.md header (the simple YAML used by Codex and Claude skills)."""
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    data: dict[str, str] = {}
    key = None
    for line in (text[3:end] if end != -1 else "").splitlines():
        match = None if line[:1] in (" ", "\t") else _KEY.match(line)
        if match:
            key, value = match.group(1), match.group(2).strip()
            data[key] = "" if value in ("|", ">", "|-", ">-", "|+", ">+") else value.strip("'\"")
        elif key and line.strip():
            data[key] = (data[key] + " " + line.strip()).strip()
    return data


def _source(base: str) -> str:
    """codex for ~/.codex/skills, claude for ~/.claude/skills, the folder's own name otherwise."""
    path = Path(base)
    return (path.parent.name if path.name == "skills" else path.name).lstrip(".") or "skills"


def _skill_folders(base: str) -> list[str]:
    """Folders under base (one or two levels down, e.g. .system/x) that hold a SKILL.md."""
    found = []
    for first in sorted(os.scandir(base), key=lambda entry: entry.name) if os.path.isdir(base) else []:
        if not first.is_dir():
            continue
        if os.path.isfile(os.path.join(first.path, "SKILL.md")):
            found.append(first.name)
            continue
        for second in sorted(os.scandir(first.path), key=lambda entry: entry.name):
            if second.is_dir() and os.path.isfile(os.path.join(second.path, "SKILL.md")):
                found.append(f"{first.name}/{second.name}")
    return found[:MAX_SKILLS]


def _describe(path: str) -> dict:
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            head = handle.read(16 * 1024)
    except OSError:
        return {}
    meta = front_matter(head)
    return {"name": meta.get("name", ""), "description": meta.get("description", "")[:1000]}


class SkillOps:
    """The skills.* operations the hub may request."""

    OPERATIONS = {"skills.list": "list_skills", "skills.read": "read_skill"}

    def __init__(self, files, *, enabled: bool = False, dirs=None):
        self.files = files  # FileOps: projects, the deny list and the agent's protected folders
        self.enabled = enabled
        self.dirs = [str(Path(os.path.expanduser(str(d))).resolve()) for d in (dirs if dirs is not None
                                                                                  else DEFAULT_DIRS)]

    def status(self) -> dict:
        return {"enabled": self.enabled, "dirs": [d for d in self.dirs if os.path.isdir(d)] if self.enabled else []}

    def dispatch(self, op: str, args: dict) -> dict:
        method = getattr(self, self.OPERATIONS[op])
        try:
            inspect.signature(method).bind(**args)
        except TypeError as exc:
            raise WebCoderError("INVALID_ARGUMENT", f"Invalid arguments for {op}: {exc}") from None
        return method(**args)

    def _user_skills(self) -> dict[str, str]:
        """skill id (e.g. codex/pair-review) -> its folder, for the user skill folders."""
        skills: dict[str, str] = {}
        for base in self.dirs if self.enabled else []:
            if any(is_within(base, protected) for protected in self.files.sb.protected_dirs):
                continue  # never the agent's own folder
            for folder in _skill_folders(base):
                skills.setdefault(f"{_source(base)}/{folder}", os.path.join(base, folder))
        return skills

    def _skill_sandbox(self, folder: str) -> tuple[str, Sandbox]:
        """A skill's own read-only sandbox: its folder (a link to where the skill is kept, maybe), and nothing the
        deny list or the agent's own folder protects."""
        real = os.path.realpath(folder)
        return real, Sandbox([real], deny=self.files.sb.deny, read_only=True,
                             protected_dirs=self.files.sb.protected_dirs)

    def list_skills(self, roots=None) -> dict:
        user = []
        for skill, folder in self._user_skills().items():
            real, sandbox = self._skill_sandbox(folder)
            try:  # read as read_skill would: a SKILL.md linked to a secret is not described
                target = sandbox.resolve(real, "SKILL.md")
            except WebCoderError:
                continue
            user.append({"id": skill, **_describe(target.path)})
        projects = []
        for root in roots or []:
            try:
                self.files.sb.root(root)
            except WebCoderError:
                continue  # not a project of this device (any more)
            for rel in PROJECT_DIRS:
                try:
                    target = self.files.sb.resolve(root, rel)
                except WebCoderError:
                    continue  # e.g. a link that leads out of the project
                for folder in _skill_folders(target.path):
                    try:  # a skill folder linked from outside the project is not the project's to show
                        skill = self.files.sb.resolve(root, f"{rel}/{folder}/SKILL.md")
                    except WebCoderError:
                        continue
                    projects.append({"root": root, "path": skill.rel, **_describe(skill.path)})
        return {"enabled": self.enabled, "user": user, "projects": projects[:MAX_SKILLS]}

    def read_skill(self, skill, path="SKILL.md") -> dict:
        if not self.enabled:
            raise WebCoderError("SKILLS_DISABLED", "Skills on this computer are not shared; its owner can turn them on "
                                "in the WebCoder panel (设备 → 修改设置) or run: webcoder-agent skills on")
        folder = self._user_skills().get(skill) if isinstance(skill, str) else None
        if folder is None:
            raise WebCoderError("SKILL_NOT_FOUND", f"No skill {skill!r} on this computer; see list_skills")
        real, sandbox = self._skill_sandbox(folder)
        target = sandbox.resolve(real, path)
        if not os.path.isfile(target.path):
            raise WebCoderError("NOT_FOUND", "No such file in the skill", path=target.display)
        with open(target.path, "rb") as handle:
            data = handle.read(MAX_READ + 1)
        files = []
        for dirpath, dirnames, filenames in os.walk(real):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
            inside = os.path.relpath(dirpath, real).replace("\\", "/")
            for name in sorted(filenames):
                rel = name if inside == "." else f"{inside}/{name}"
                if not sandbox.is_denied(rel, real):
                    files.append(rel)
            if len(files) >= MAX_FILES:
                break
        return {"skill": skill, "path": target.display, "size": os.path.getsize(target.path),
                "content": data[:MAX_READ].decode("utf-8", errors="replace"), "truncated": len(data) > MAX_READ,
                "files": files[:MAX_FILES]}
