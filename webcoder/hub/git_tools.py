"""MCP tools for git in a project: the agent on the project's computer runs git for each (webcoder/agent/gitops.py
says how): git_run any git command as given, the others one fixed, confined command each.

Reading (status, diff, log, show, blame, branches) needs the read permission; anything that changes the
repository, the working tree or a remote needs write, on a read-write project. Apart from git_run and git_init,
the project folder must be the top of its repository.
"""
from __future__ import annotations

import shlex

from webcoder.agent.gitops import mask_remotes
from webcoder.common.errors import WebCoderError
from webcoder.hub.tools import PATH, PROJECT, Tool, ToolContext, _object

READ = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
CHANGE = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False}
DISCARD = {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False}
REMOTE = {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True}

REF = {"type": "string", "minLength": 1, "maxLength": 200,
       "description": "Commit, branch or tag, e.g. HEAD~1, main, origin/main, v1.2, a1b2c3d"}
BRANCH = {"type": "string", "minLength": 1, "maxLength": 200, "description": "Branch name, e.g. feature/login"}
PATHS = {"type": "array", "items": PATH, "minItems": 1, "maxItems": 200,
         "description": "Paths in the project (leave it out rather than empty)"}
FLAG = {"type": "boolean", "default": False}
MESSAGE = {"type": "string", "minLength": 1, "maxLength": 20000}
MAX_COMMAND = 19_000  # characters of a git_run command written out: the audit log keeps it (with the project) whole
_RENAMED = {"all": "every"}  # schema name -> agent parameter


def _format(project: dict, r: dict) -> str:
    lines = [f"$ {r['command']}   [{project['alias']} · exit code {r['exit_code']}]"]
    if r.get("snapshot"):
        lines.append(f"Uncommitted changes were saved first as {r['snapshot']}: bring them back with git_stash "
                     f"action=apply ref={r['snapshot']}.")
    out, err = r["stdout"].rstrip("\n"), r["stderr"].rstrip("\n")
    lines += [part for part in (out, "--- stderr ---\n" + err if err else "") if part] or ["(no output)"]
    if r.get("truncated"):
        lines.append("(output shortened: narrow it with paths, max_count or stat)")
    return "\n".join(lines)


def _runner(op: str, *, write: bool, timeout: float = 150.0):
    async def run(ctx: ToolContext, a: dict) -> str:
        project = ctx.project(a["project"], write=write)
        args = {_RENAMED.get(key, key): value for key, value in a.items() if key != "project" and value is not None}
        ctx.audit_target = f"{project['alias']}: {op}"  # logged even if it fails
        r = await ctx.agent(project, op, args, timeout)
        ctx.audit_target = f"{project['alias']}: {r['command']}"[:20_000]
        text = _format(project, r)
        if r["exit_code"] != 0:
            raise WebCoderError("GIT_FAILED", text)
        return text
    return run


async def _run_any(ctx: ToolContext, a: dict) -> str:
    command = mask_remotes(shlex.join(["git", *a["args"]]))
    if len(command) > MAX_COMMAND:
        ctx.audit_target = command[:MAX_COMMAND] + " …"
        raise WebCoderError("INVALID_ARGUMENT", f"The command is {len(command):,} characters written out; at most "
                            f"{MAX_COMMAND:,} can run, so that the audit log keeps it whole. Pass long text on stdin")
    project = ctx.project(a["project"], write=True)
    ctx.audit_target = f"{project['alias']}: {command}"  # logged even if it fails or times out
    args = {key: value for key, value in a.items() if key != "project" and value is not None}
    r = await ctx.agent(project, "git.run", args, a["timeout"] + 30.0)
    text = _format(project, r)
    if r["exit_code"] != 0:
        raise WebCoderError("GIT_FAILED", text)
    return text


_GIT_LIST = [
    Tool("git_status", "Git status",
         "Current branch (with its upstream, ahead/behind) and changed, staged and untracked files.",
         "read", _object({"project": PROJECT}, ("project",)), READ, _runner("git.status", write=False)),
    Tool("git_diff", "Git diff",
         "Changes as a unified diff: working tree vs index (default), staged changes (staged=true), or between "
         "commits (base, and optionally head). stat=true gives a summary. Secret files are left out.",
         "read", _object({"project": PROJECT, "staged": FLAG, "base": REF, "head": REF, "paths": PATHS, "stat": FLAG,
                          "context": {"type": "integer", "minimum": 0, "maximum": 50, "default": 3}},
                         ("project",)), READ, _runner("git.diff", write=False)),
    Tool("git_log", "Git log",
         "Commit history of ref (default HEAD), newest first; base limits it to base..ref. patch=true includes "
         "each commit's diff. paths limits it to commits touching them.",
         "read", _object({"project": PROJECT, "ref": REF, "base": REF, "paths": PATHS, "patch": FLAG,
                          "max_count": {"type": "integer", "minimum": 1, "maximum": 500, "default": 30}},
                         ("project",)), READ, _runner("git.log", write=False)),
    Tool("git_show", "Git show",
         "One commit with its message and diff (stat_only=true for a summary), or with path: that file's "
         "content as it was at the commit.",
         "read", _object({"project": PROJECT, "ref": REF, "path": PATH, "stat_only": FLAG}, ("project",)),
         READ, _runner("git.show", write=False)),
    Tool("git_blame", "Git blame",
         "Who last changed each line of a file, and in which commit (optionally a line range, at a ref).",
         "read", _object({"project": PROJECT, "path": PATH, "ref": REF,
                          "start_line": {"type": "integer", "minimum": 1},
                          "end_line": {"type": "integer", "minimum": 1}}, ("project", "path")),
         READ, _runner("git.blame", write=False)),
    Tool("git_branches", "Git branches",
         "Local and remote branches (upstream, ahead/behind, last commit), remotes, tags and stashes.",
         "read", _object({"project": PROJECT}, ("project",)), READ, _runner("git.branches", write=False)),
    Tool("git_add", "Git add",
         "Stage changes: the given paths, or everything (paths omitted). all=true also stages deletions. "
         "Secret files are never staged.",
         "write", _object({"project": PROJECT, "paths": PATHS, "all": FLAG}, ("project",)), CHANGE,
         _runner("git.add", write=True)),
    Tool("git_commit", "Git commit",
         "Commit what is staged (all=true: every change to tracked files; paths: only those). amend=true "
         "rewrites the last commit. Commit hooks do not run.",
         "write", _object({"project": PROJECT, "message": MESSAGE, "all": FLAG, "amend": FLAG, "allow_empty": FLAG,
                           "paths": PATHS}, ("project",)), CHANGE, _runner("git.commit", write=True)),
    Tool("git_restore", "Git restore",
         "Discard uncommitted changes to files (worktree, the default) and/or unstage them (staged=true); source "
         "takes them from another commit. Discarded changes are saved first as a snapshot.",
         "write", _object({"project": PROJECT, "paths": PATHS, "staged": FLAG,
                           "worktree": {"type": "boolean", "default": True}, "source": REF}, ("project", "paths")),
         DISCARD, _runner("git.restore", write=True)),
    Tool("git_reset", "Git reset",
         "Move the current branch to ref (default HEAD): soft keeps changes staged, mixed (default) unstaged, "
         "hard discards them (saved first as a snapshot). With paths: unstage those paths.",
         "write", _object({"project": PROJECT, "mode": {"type": "string", "enum": ["soft", "mixed", "hard"],
                                                        "default": "mixed"},
                           "ref": REF, "paths": PATHS}, ("project",)), DISCARD, _runner("git.reset", write=True)),
    Tool("git_branch", "Git branch",
         "Branches: switch to one (discard_changes=true throws away local changes, saved first), create one and "
         "switch to it (from start), detach at a commit, delete (force=true even if unmerged) or rename.",
         "write", _object({"project": PROJECT, "action": {"type": "string",
                                                          "enum": ["switch", "create", "detach", "delete", "rename"]},
                           "name": BRANCH, "new_name": BRANCH, "start": REF, "force": FLAG,
                           "discard_changes": FLAG}, ("project", "action", "name")),
         DISCARD, _runner("git.branch", write=True)),
    Tool("git_stash", "Git stash",
         "Stash local changes (push; include_untracked for new files), or pop / apply / drop a stash (ref, default "
         "stash@{0}; apply also takes a snapshot ref).",
         "write", _object({"project": PROJECT, "action": {"type": "string", "enum": ["push", "pop", "apply", "drop"]},
                           "message": {"type": "string", "minLength": 1, "maxLength": 500},
                           "include_untracked": FLAG, "ref": REF}, ("project", "action")),
         DISCARD, _runner("git.stash", write=True)),
    Tool("git_sync", "Git fetch / pull / push",
         "Talk to a remote configured in the repository (never a URL): fetch (all remotes unless remote), pull "
         "(rebase or ff_only), push (current branch to its upstream, or branch[:to] to remote; set_upstream, tags, "
         "force_with_lease, delete a remote branch). Uses the computer's saved git credentials; never prompts.",
         "write", _object({"project": PROJECT, "action": {"type": "string", "enum": ["fetch", "pull", "push"]},
                           "remote": {"type": "string", "minLength": 1, "maxLength": 100}, "branch": BRANCH,
                           "to": BRANCH, "prune": FLAG, "rebase": FLAG, "ff_only": FLAG, "set_upstream": FLAG,
                           "force_with_lease": FLAG, "tags": FLAG, "delete": FLAG}, ("project", "action")),
         REMOTE, _runner("git.sync", write=True, timeout=330.0)),
    Tool("git_integrate", "Git merge / rebase / cherry-pick / revert",
         "Bring commits together: merge ref (no_ff, squash, message), rebase onto ref (never interactive), "
         "cherry_pick ref or refs, revert ref. After conflicts, fix the files, git_add them, then step=continue; "
         "or step=abort (skip for rebase / cherry-pick).",
         "write", _object({"project": PROJECT,
                           "operation": {"type": "string", "enum": ["merge", "rebase", "cherry_pick", "revert"]},
                           "step": {"type": "string", "enum": ["start", "abort", "continue", "skip"],
                                    "default": "start"},
                           "ref": REF, "refs": {"type": "array", "items": REF, "maxItems": 100},
                           "no_ff": FLAG, "squash": FLAG, "message": MESSAGE}, ("project", "operation")),
         DISCARD, _runner("git.integrate", write=True)),
    Tool("git_tag", "Git tag",
         "Create a tag at ref (default HEAD; annotated when message is given) or delete one.",
         "write", _object({"project": PROJECT, "action": {"type": "string", "enum": ["create", "delete"]},
                           "name": BRANCH, "ref": REF, "message": MESSAGE}, ("project", "action", "name")),
         CHANGE, _runner("git.tag", write=True)),
    Tool("git_clean", "Git clean",
         "Delete untracked files (directories=true: also untracked folders; ignored=true: also ignored files). "
         "dry_run (the default) only lists them: show the list, then call again with dry_run=false. Secret files "
         "are never deleted.",
         "write", _object({"project": PROJECT, "dry_run": {"type": "boolean", "default": True},
                           "directories": FLAG, "ignored": FLAG, "paths": PATHS}, ("project",)),
         DISCARD, _runner("git.clean", write=True)),
    Tool("git_init", "Git init",
         "Create a git repository in the project folder.",
         "write", _object({"project": PROJECT, "initial_branch": BRANCH}, ("project",)), CHANGE,
         _runner("git.init", write=True)),
    Tool("git_run", "Run git",
         "Run any git command in the project folder with any arguments (a list, no shell), as the owner would at "
         "their computer: hooks, settings, credentials and every option work as usual; it never prompts. Use it "
         "for anything the other git_* tools do not cover or refuse (submodules, worktrees, bisect, config, "
         "remotes, commits with hooks ...). stdin feeds the command, e.g. args [\"apply\", \"-\"] with a patch; "
         "long text goes there too (a command runs only up to 19,000 characters written out).",
         "write", _object({"project": PROJECT,
                           "args": {"type": "array", "items": {"type": "string", "maxLength": 30000}, "minItems": 1,
                                    "maxItems": 500,
                                    "description": "git's arguments without git itself, e.g. [\"submodule\", "
                                                   "\"update\", \"--init\"]"},
                           "stdin": {"type": "string", "maxLength": 1000000,
                                     "description": "Text for the command's standard input"},
                           "timeout": {"type": "integer", "minimum": 1, "maximum": 600, "default": 120}},
                          ("project", "args")),
         REMOTE, _run_any),
]
GIT_TOOLS: dict[str, Tool] = {tool.name: tool for tool in _GIT_LIST}
