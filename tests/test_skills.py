"""Skills: user skill folders shared read-only when the owner turns it on, project skills, and the MCP tools."""
import pytest

from tests.conftest import AgentThread
from webcoder.agent.__main__ import main as agent_main
from webcoder.agent.__main__ import pair_device
from webcoder.agent.config import AgentConfig
from webcoder.agent.fileops import BackupStore, FileOps, Sandbox
from webcoder.agent.skills import SkillOps, front_matter
from webcoder.common.errors import WebCoderError
from webcoder.hub.install import roots_command

REVIEW = "---\nname: pair-review\ndescription: |\n  Let another model review\n  the local changes.\n---\n\n# Steps\n"


@pytest.fixture
def home(tmp_path):
    """A user home with Codex and Claude skills, and the things next to them that must stay private."""
    base = tmp_path / "home"
    (base / ".codex" / "skills" / "pair-review" / "references").mkdir(parents=True)
    (base / ".codex" / "skills" / "pair-review" / "SKILL.md").write_bytes(REVIEW.encode("utf-8"))
    (base / ".codex" / "skills" / "pair-review" / "references" / "notes.md").write_text("details", encoding="utf-8")
    (base / ".codex" / "skills" / "pair-review" / ".env").write_text("TOKEN=secret", encoding="utf-8")
    (base / ".codex" / "skills" / ".system" / "creator").mkdir(parents=True)
    (base / ".codex" / "skills" / ".system" / "creator" / "SKILL.md").write_text(
        "---\nname: creator\ndescription: Make skills\n---\n", encoding="utf-8")
    (base / ".codex" / "auth.json").write_text('{"token": "codex-login"}', encoding="utf-8")
    (base / ".claude" / "skills" / "meegle").mkdir(parents=True)
    (base / ".claude" / "skills" / "meegle" / "SKILL.md").write_text(
        "---\nname: meegle\ndescription: 飞书项目 work items\n---\nRun meegle ...\n", encoding="utf-8")
    return base


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "work"
    (root / ".claude" / "skills" / "deploy").mkdir(parents=True)
    (root / ".claude" / "skills" / "deploy" / "SKILL.md").write_text(
        "---\nname: deploy\ndescription: How this project is deployed\n---\n", encoding="utf-8")
    return root


def make_ops(home, project, tmp_path, enabled=True):
    files = FileOps(Sandbox([project]), BackupStore(tmp_path / "backups"))
    files.skills = SkillOps(files, enabled=enabled, dirs=[home / ".codex" / "skills", home / ".claude" / "skills"])
    return files


def test_front_matter_takes_folded_descriptions():
    assert front_matter(REVIEW) == {"name": "pair-review", "description": "Let another model review the local changes."}
    assert front_matter("# no header") == {}


def test_user_skills_only_when_turned_on(home, project, tmp_path):
    off = make_ops(home, project, tmp_path, enabled=False)
    listed = off.dispatch("skills.list", {"roots": [str(project)]})
    assert listed["user"] == [] and [item["path"] for item in listed["projects"]] == [
        ".claude/skills/deploy/SKILL.md"]  # project skills are project files anyway
    with pytest.raises(WebCoderError) as err:
        off.dispatch("skills.read", {"skill": "codex/pair-review"})
    assert err.value.code == "SKILLS_DISABLED"
    on = make_ops(home, project, tmp_path)
    listed = on.dispatch("skills.list", {"roots": [str(project)]})
    assert {item["id"]: item["description"] for item in listed["user"]} == {
        "codex/.system/creator": "Make skills", "codex/pair-review": "Let another model review the local changes.",
        "claude/meegle": "飞书项目 work items"}


def test_only_skill_folders_are_readable(home, project, tmp_path):
    ops = make_ops(home, project, tmp_path)
    skill = ops.dispatch("skills.read", {"skill": "codex/pair-review"})
    assert skill["content"].startswith("---\nname: pair-review") and skill["files"] == ["SKILL.md",
                                                                                      "references/notes.md"]
    assert ops.dispatch("skills.read", {"skill": "codex/pair-review", "path": "references/notes.md"})["content"] == \
        "details"
    for path, code in (("../../auth.json", "INVALID_PATH"), (".env", "PATH_DENIED"), ("missing.md", "NOT_FOUND")):
        with pytest.raises(WebCoderError) as err:
            ops.dispatch("skills.read", {"skill": "codex/pair-review", "path": path})
        assert err.value.code == code, path
    with pytest.raises(WebCoderError) as err:  # only skill ids from the list, never a folder of our choosing
        ops.dispatch("skills.read", {"skill": "codex/../auth.json"})
    assert err.value.code == "SKILL_NOT_FOUND"


def test_project_skills_linked_from_outside_are_not_shown(home, project, tmp_path):
    outside = tmp_path / "elsewhere" / "private-skill"
    outside.mkdir(parents=True)
    (outside / "SKILL.md").write_bytes(b"---\nname: private\ndescription: not the project's\n---\n")
    try:
        (project / ".claude" / "skills" / "linked").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are not available")
    listed = make_ops(home, project, tmp_path, enabled=False).dispatch("skills.list", {"roots": [str(project)]})
    assert [item["path"] for item in listed["projects"]] == [".claude/skills/deploy/SKILL.md"]


def test_a_skill_file_linked_to_a_secret_is_not_described(home, project, tmp_path):
    secret = home / ".ssh"
    secret.mkdir()
    (secret / "notes").write_bytes(b"---\nname: leaked\ndescription: the key is hunter2\n---\n")
    (home / ".codex" / "skills" / "trap").mkdir()
    try:
        (home / ".codex" / "skills" / "trap" / "SKILL.md").symlink_to(secret / "notes")
    except OSError:
        pytest.skip("symlinks are not available")
    listed = make_ops(home, project, tmp_path).dispatch("skills.list", {"roots": []})
    assert "hunter2" not in str(listed) and "codex/trap" not in {item["id"] for item in listed["user"]}


def test_cli_and_settings_command(tmp_path):
    config_path = tmp_path / "agent" / "config.json"
    AgentConfig(hub_url="http://127.0.0.1:1", device_id="dev_x", device_secret="c2VjcmV0", path=config_path).save()
    assert agent_main(["--config", str(config_path), "skills", "on"]) == 0
    assert AgentConfig.load(config_path).skills is True
    assert roots_command("linux", ["/x"], None, None, True).endswith(" && ~/.webcoder-agent/venv/bin/python -m "
                                                                     "webcoder.agent skills on")


# ------------------------------------------------------------------ through the hub
def test_skill_tools_through_the_hub(hub, home, project, tmp_path):
    device = hub.add_device("pc")
    config = pair_device(hub.url, device["code"], [str(project)], tmp_path / "agent" / "config.json", proxy="none")
    config.skills, config.skill_dirs = True, [str(home / ".codex" / "skills"), str(home / ".claude" / "skills")]
    config.save()
    runner = AgentThread(config)
    runner.start()
    try:
        hub.wait_for(lambda: (hub.device(device["device_id"])["info"].get("skills") or {}).get("enabled"),
                     what="agent status")
        hub.add_project("work", device["device_id"], project)
        token = hub.create_token(scopes=("read",), label="reader")
        listing = hub.ok(token, "list_skills")
        assert "- codex/pair-review: Let another model review the local changes." in listing
        assert "project work: .claude/skills/deploy/SKILL.md" in listing and "auth.json" not in listing
        skill = hub.ok(token, "read_skill", skill="claude/meegle")
        assert "Run meegle" in skill and "Files in this skill: SKILL.md" in skill
        limited = hub.create_token(scopes=("read",), projects=[hub.runtime.store.one(
            "SELECT id FROM projects WHERE alias='work'")["id"]], label="limited")
        assert "codex/pair-review" not in hub.ok(limited, "list_skills")  # user skills are outside its projects
        text, is_error = hub.call(limited, "read_skill", skill="claude/meegle")
        assert is_error and "SKILLS_NOT_ALLOWED" in text
    finally:
        runner.stop()
