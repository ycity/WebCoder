"""MCP tools for skills: the person's reusable task instructions (folders with a SKILL.md) on their computers.

User skills (~/.codex/skills, ~/.claude/skills, ~/.agents/skills) are shared read-only when the owner turned it
on for that computer; skills inside projects are project files and are listed for every credential that
can see the project. A credential limited to some projects never sees user skills: they are outside them.
"""
from __future__ import annotations

from webcoder.common.errors import WebCoderError
from webcoder.hub.tools import PATH, Tool, ToolContext, _object

READ = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
COMPUTER = {"type": "string", "minLength": 1, "maxLength": 100,
            "description": "Computer (device name) that has the skill; optional when only one shares skills"}


def _line(text: str, limit: int = 300) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit] + " …"


async def _list(ctx: ToolContext, a: dict) -> str:
    whole_user = ctx.principal.projects is None
    projects = ctx.visible_projects()
    lines = []
    for row in ctx.computers():
        if not (row["enabled"] and row["online"]):
            continue
        roots = [p["root"] for p in projects if p["device_id"] == row["id"]]
        try:
            r = await ctx.on_computer(row, "skills.list", {"roots": roots}, 30.0)
        except WebCoderError as exc:
            lines.append(f"{row['name']}: skills could not be listed ({exc.code})")
            continue
        alias = {p["root"]: p["alias"] for p in projects if p["device_id"] == row["id"]}
        section = []
        if whole_user and r["enabled"]:
            section += [f"- {item['id']}: {_line(item.get('description'))}" for item in r["user"]]
        elif whole_user:
            section.append("- (user skills are not shared; the owner can turn that on in 设备 → 修改设置)")
        section += [f"- project {alias.get(item['root'], '?')}: {item['path']} — {item.get('name') or ''}: "
                    f"{_line(item.get('description'))}" for item in r["projects"]]
        if section:
            lines.append(f"{row['name']}:\n" + "\n".join(section))
    if not lines:
        return "No skills found on the computers this credential can reach."
    return ("Skills (instructions for kinds of task). When one fits the task, read it first and follow it: user "
            "skills with read_skill, project skills with read_file. A skill may call for programs you cannot run "
            "through these tools; say so rather than pretend.\n\n" + "\n\n".join(lines))


async def _read(ctx: ToolContext, a: dict) -> str:
    if ctx.principal.projects is not None:
        raise WebCoderError("SKILLS_NOT_ALLOWED", "This credential is limited to some projects; user skills are "
                            "outside them (project skills are read with read_file)")
    rows = [row for row in ctx.computers() if row["enabled"] and row["online"] and row["skills"]]
    if a.get("computer"):
        wanted = a["computer"].strip().lower()
        rows = [row for row in rows if row["name"].lower() == wanted or row["id"] == a["computer"].strip()]
    if not rows:
        raise WebCoderError("SKILLS_DISABLED", "No online computer shares its skills (the owner can turn that on in "
                            "设备 → 修改设置)")
    if len(rows) > 1:
        raise WebCoderError("SKILLS_COMPUTER_AMBIGUOUS", "Several computers share skills: "
                            + ", ".join(row["name"] for row in rows) + "; pass computer")
    computer = rows[0]
    r = await ctx.on_computer(computer, "skills.read", {"skill": a["skill"], "path": a["path"]}, 30.0)
    ctx.audit_target = f"{computer['name']}: {r['skill']}/{r['path']}"
    header = (f"Skill {r['skill']} · {r['path']} ({r['size']:,} bytes) on {computer['name']}. Files in this skill: "
              + ", ".join(r["files"]) + ". Read another with path=…; for ../other-skill/… use "
              "skill=<source>/other-skill.")
    return header + ("\n(shortened)" if r.get("truncated") else "") + "\n\n" + r["content"]


_SKILL_LIST = [
    Tool("list_skills", "List skills",
         "List the person's skills (reusable instructions for kinds of task, e.g. code review or a company tool) on "
         "their computers and in projects. Check it when starting a task.",
         "read", _object({}), READ, _list),
    Tool("read_skill", "Read skill",
         "Read a user skill's SKILL.md, or another file in the skill's folder (path), to follow it.",
         "read", _object({"skill": {"type": "string", "minLength": 1, "maxLength": 300,
                                    "description": "Skill id from list_skills, e.g. codex/pair-review"},
                          "path": {**PATH, "default": "SKILL.md"}, "computer": COMPUTER}, ("skill",)), READ, _read),
]
SKILL_TOOLS: dict[str, Tool] = {tool.name: tool for tool in _SKILL_LIST}
