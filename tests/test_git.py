"""git in projects, against real repositories: what git may do, what it may not, and the MCP tools."""
import os
import shutil
import subprocess

import pytest

from webcoder.agent import gitops as gitops_module
from webcoder.agent.fileops import BackupStore, FileOps, Sandbox
from webcoder.agent.gitops import GitOps
from webcoder.common.errors import WebCoderError

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def git(cwd, *args) -> str:
    done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8")
    assert done.returncode == 0, done.stderr
    return done.stdout


def make_repo(path):
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.name", "Tester")
    git(path, "config", "user.email", "tester@example.com")
    git(path, "config", "commit.gpgsign", "false")
    (path / "app.py").write_bytes(b"print('v1')\n")
    (path / ".env").write_bytes(b"TOKEN=secret-v1\n")  # a tracked secret: it happens
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "first")
    return path


@pytest.fixture
def repo(tmp_path):
    return make_repo(tmp_path / "work")


@pytest.fixture
def ops(repo, tmp_path):
    files = FileOps(Sandbox([repo]), BackupStore(tmp_path / "backups"))
    files.git = GitOps(files, hooks_dir=tmp_path / "no-hooks")
    return files


def run(ops, op, **args) -> dict:
    return ops.dispatch(op, args)


def root(repo) -> str:
    return str(repo)


def code(excinfo) -> str:
    return excinfo.value.code


def test_reading_the_repository(ops, repo):
    (repo / "app.py").write_bytes(b"print('v2')\n")
    status = run(ops, "git.status", root=root(repo))
    assert status["exit_code"] == 0 and "## main" in status["stdout"] and " M app.py" in status["stdout"]
    diff = run(ops, "git.diff", root=root(repo))
    assert "-print('v1')" in diff["stdout"] and "+print('v2')" in diff["stdout"]
    log = run(ops, "git.log", root=root(repo))
    assert "Tester" in log["stdout"] and "first" in log["stdout"]
    shown = run(ops, "git.show", root=root(repo), ref="HEAD", path="app.py")
    assert shown["stdout"] == "print('v1')\n"
    assert "Not Committed Yet" in run(ops, "git.blame", root=root(repo), path="app.py")["stdout"]
    assert "Tester" in run(ops, "git.blame", root=root(repo), path="app.py", ref="HEAD")["stdout"]
    overview = run(ops, "git.branches", root=root(repo))["stdout"]
    assert "== branches" in overview and "main" in overview and "== stashes\n(none)" in overview


def test_secrets_stay_out_of_diffs_and_the_index(ops, repo):
    (repo / ".env").write_bytes(b"TOKEN=secret-v2\n")
    (repo / "config").mkdir()
    (repo / "config" / "id_rsa").write_bytes(b"PRIVATE KEY\n")
    for op, args in (("git.diff", {}), ("git.log", {"patch": True}), ("git.show", {"ref": "HEAD"})):
        assert "secret" not in run(ops, op, root=root(repo), **args)["stdout"], op
    with pytest.raises(WebCoderError) as err:  # asked for by name: the deny list says no
        run(ops, "git.show", root=root(repo), ref="HEAD", path=".env")
    assert code(err) == "PATH_DENIED"
    blob = git(repo, "rev-parse", "HEAD:.env").strip()
    for ref in (blob, "HEAD^{tree}"):  # nor by the id of its blob (or a tree): git_show shows commits only
        with pytest.raises(WebCoderError) as err:
            run(ops, "git.show", root=root(repo), ref=ref)
        assert code(err) == "INVALID_ARGUMENT", ref
    run(ops, "git.add", root=root(repo), every=True)
    staged = git(repo, "diff", "--cached", "--name-only")
    assert ".env" not in staged and "id_rsa" not in staged
    (repo / "junk").mkdir()
    (repo / "junk" / "tmp.log").write_bytes(b"log\n")
    cleaned = run(ops, "git.clean", root=root(repo), dry_run=False, directories=True)
    assert (repo / "config" / "id_rsa").exists() and not (repo / "junk").exists()  # secrets are never deleted
    assert "Kept, because they hold protected files: config" in cleaned["stdout"]
    run(ops, "git.stash", root=root(repo), action="push", include_untracked=True)
    assert (repo / "config" / "id_rsa").exists() and b"secret-v2" in (repo / ".env").read_bytes()


def test_committing_everything_never_commits_a_tracked_secret(ops, repo):
    (repo / ".env").write_bytes(b"TOKEN=secret-v2\n")  # tracked, and changed
    (repo / "app.py").write_bytes(b"print('v2')\n")
    run(ops, "git.commit", root=root(repo), message="all changes", every=True)
    assert git(repo, "show", "--name-only", "--format=", "HEAD").split() == ["app.py"]
    (repo / "app.py").write_bytes(b"print('v3')\n")
    run(ops, "git.commit", root=root(repo), message="by path", paths=["."])
    assert git(repo, "show", "--name-only", "--format=", "HEAD").split() == ["app.py"]
    assert b"secret-v2" in (repo / ".env").read_bytes() and " M .env" in git(repo, "status", "--short")
    git(repo, "add", ".env")  # the owner staged it: a commit of the index would take it along
    (repo / "app.py").write_bytes(b"print('v4')\n")
    for args in ({"every": True}, {}):
        with pytest.raises(WebCoderError) as err:
            run(ops, "git.commit", root=root(repo), message="x", **args)
        assert code(err) == "GIT_SECRETS_STAGED" and ".env" in err.value.message
    assert git(repo, "diff", "--cached", "--name-only").split() == [".env"]  # nothing else was staged either
    run(ops, "git.commit", root=root(repo), message="by path", paths=["app.py"])  # leaves the staged secret out
    assert git(repo, "show", "--name-only", "--format=", "HEAD").split() == ["app.py"]
    git(repo, "rm", "-q", "--cached", ".env")  # untracked: taking it out of the repository is fine
    run(ops, "git.commit", root=root(repo), message="stop tracking the secret")
    assert git(repo, "show", "--name-only", "--format=", "HEAD").split() == [".env"]


def test_uncommitted_secret_changes_are_never_thrown_away(ops, repo):
    (repo / ".env").write_bytes(b"TOKEN=changed-locally\n")  # tracked, with work not committed
    (repo / "app.py").write_bytes(b"print('v2')\n")
    run(ops, "git.restore", root=root(repo), paths=["."])  # everything else goes back
    assert (repo / "app.py").read_bytes() == b"print('v1')\n" and b"changed-locally" in (repo / ".env").read_bytes()
    git(repo, "branch", "other")
    for op, args in (("git.reset", {"mode": "hard"}),
                     ("git.branch", {"action": "switch", "name": "other", "discard_changes": True})):
        with pytest.raises(WebCoderError) as err:
            run(ops, op, root=root(repo), **args)
        assert code(err) == "GIT_SECRETS_CHANGED" and ".env" in err.value.message, op
    assert b"changed-locally" in (repo / ".env").read_bytes()
    git(repo, "stash", "push", "-q", "-m", "the owner's own stash, secret included")
    with pytest.raises(WebCoderError) as err:  # applying it would write the secret back
        run(ops, "git.stash", root=root(repo), action="apply")
    assert code(err) == "GIT_SECRETS_CHANGED" and b"secret-v1" in (repo / ".env").read_bytes()
    git(repo, "stash", "drop", "-q")
    (repo / "app.py").write_bytes(b"print('stashed')\n")
    run(ops, "git.stash", root=root(repo), action="push")
    assert run(ops, "git.stash", root=root(repo), action="pop")["exit_code"] == 0  # no secret in it: fine
    assert run(ops, "git.reset", root=root(repo), mode="hard")["exit_code"] == 0


def test_a_project_that_is_a_secret_folder_itself(tmp_path):
    """The file tools judge whole paths: a repository at ~/.aws or ~/.ssh is covered by the rules for them."""
    home = tmp_path / "home"
    aws, ssh = make_repo(home / ".aws"), make_repo(home / ".ssh")
    for folder, secret in ((aws, "credentials"), (ssh, "config")):
        (folder / secret).write_bytes(b"[default]\naws_secret_access_key = s3cret\n")
        (folder / "notes.txt").write_bytes(b"fine\n")
    files = FileOps(Sandbox([home]), BackupStore(tmp_path / "b"))
    files.git = GitOps(files, hooks_dir=tmp_path / "no-hooks")
    run(files, "git.add", root=str(aws), every=True)
    assert git(aws, "diff", "--cached", "--name-only").split() == ["notes.txt"]  # credentials stays out
    assert "s3cret" not in run(files, "git.diff", root=str(aws), base="HEAD")["stdout"]
    run(files, "git.add", root=str(ssh), every=True)
    assert git(ssh, "diff", "--cached", "--name-only").split() == []  # all of ~/.ssh is secret


def test_sample_env_files_are_not_secrets(ops, repo):
    """.env.example and the like are readable and writable through the file tools, so git treats them the same."""
    (repo / ".env.example").write_bytes(b"TOKEN=\n")
    (repo / ".env.local").write_bytes(b"TOKEN=real\n")
    run(ops, "git.add", root=root(repo))
    assert git(repo, "diff", "--cached", "--name-only").split() == [".env.example"]
    run(ops, "git.commit", root=root(repo), message="sample config")  # staged, and no secret among it
    (repo / ".env.example").write_bytes(b"TOKEN=\nREGION=\n")
    assert "+REGION=" in run(ops, "git.diff", root=root(repo))["stdout"]
    run(ops, "git.commit", root=root(repo), message="more", every=True)
    assert git(repo, "show", "--name-only", "--format=", "HEAD").split() == [".env.example"]
    assert "?? .env.local" in git(repo, "status", "--short")


def test_hooks_never_run(ops, repo, tmp_path):
    marker = tmp_path / "hook-ran"
    script = f"#!/bin/sh\necho ran > '{marker.as_posix()}'\n"
    (repo / ".git" / "hooks" / "pre-commit").write_text(script, encoding="utf-8", newline="\n")
    (repo / ".husky").mkdir()  # husky style: hooks in the working tree, which the AI can edit
    (repo / ".husky" / "post-commit").write_text(script, encoding="utf-8", newline="\n")
    git(repo, "config", "core.hooksPath", ".husky")
    for hook in (repo / ".git" / "hooks" / "pre-commit", repo / ".husky" / "post-commit"):
        hook.chmod(0o755)
    (repo / "app.py").write_bytes(b"print('v3')\n")
    result = run(ops, "git.commit", root=root(repo), message="change", every=True)
    assert result["exit_code"] == 0 and "change" in git(repo, "log", "-1", "--format=%s")
    assert not marker.exists()


def test_clean_never_takes_a_folder_as_a_whole_unless_asked(ops, repo):
    """With a pathspec, git clean deletes untracked folders even without -d, and does not look inside them."""
    (repo / "junk").mkdir()
    (repo / "junk" / ".env").write_bytes(b"TOKEN=inside-an-untracked-folder\n")
    (repo / "junk" / "a.log").write_bytes(b"log\n")
    (repo / "stray.log").write_bytes(b"log\n")
    run(ops, "git.clean", root=root(repo), dry_run=False)  # files only, as asked
    assert not (repo / "stray.log").exists() and (repo / "junk" / "a.log").exists()
    assert (repo / "junk" / ".env").exists()
    cleaned = run(ops, "git.clean", root=root(repo), dry_run=False, directories=True)
    assert (repo / "junk" / ".env").exists() and "Kept, because they hold protected files: junk" in cleaned["stdout"]


def test_an_empty_path_list_is_not_the_whole_repository(ops, repo):
    (repo / "app.py").write_bytes(b"print('keep me')\n")
    (repo / "junk.txt").write_bytes(b"untracked\n")
    for op, args in (("git.restore", {"paths": []}), ("git.clean", {"paths": [], "dry_run": False}),
                     ("git.add", {"paths": []}), ("git.commit", {"message": "x", "paths": []})):
        with pytest.raises(WebCoderError) as err:  # the secret excludes alone would cover everything
            run(ops, op, root=root(repo), **args)
        assert code(err) == "INVALID_ARGUMENT", op
    assert (repo / "app.py").read_bytes() == b"print('keep me')\n" and (repo / "junk.txt").exists()


def test_arguments_cannot_become_options(ops, repo, tmp_path):
    bad_calls = [("git.log", {"ref": "--output=" + str(tmp_path / "x")}), ("git.diff", {"base": "-p"}),
                 ("git.diff", {"base": "main..HEAD"}), ("git.show", {"ref": "HEAD --exec=x"}),
                 ("git.branch", {"action": "create", "name": "-D"}),
                 ("git.branch", {"action": "create", "name": "a..b"}),
                 ("git.sync", {"action": "fetch", "remote": "https://evil.example/x.git"}),
                 ("git.sync", {"action": "fetch", "remote": "--upload-pack=calc"}),
                 ("git.sync", {"action": "push", "remote": "."})]
    for op, args in bad_calls:
        with pytest.raises(WebCoderError) as err:
            run(ops, op, root=root(repo), **args)
        assert code(err) == "INVALID_ARGUMENT", (op, args)
    with pytest.raises(WebCoderError) as err:
        run(ops, "git.sync", root=root(repo), action="fetch", remote="upstream")  # not configured
    assert code(err) == "GIT_UNKNOWN_REMOTE"
    with pytest.raises(WebCoderError) as err:  # paths stay in the project
        run(ops, "git.add", root=root(repo), paths=["../outside"])
    assert code(err) == "INVALID_PATH"
    (repo / "new.txt").write_bytes(b"new\n")
    run(ops, "git.add", root=root(repo), paths=["*.txt"])  # taken literally: no glob, no pathspec magic
    assert "new.txt" not in git(repo, "diff", "--cached", "--name-only")
    assert not (tmp_path / "x").exists()


def test_snapshots_never_hold_secrets(ops, repo, tmp_path):
    """A snapshot is a commit: it could be branched from, merged or pushed. Secret files stay out of it."""
    (repo / ".env").write_bytes(b"TOKEN=uncommitted-secret\n")  # tracked, changed, and staged as well
    git(repo, "add", ".env")
    (repo / "app.py").write_bytes(b"print('work in progress')\n")
    saved = run(ops, "git.restore", root=root(repo), paths=["app.py"])["snapshot"]
    assert saved.startswith("refs/webcoder/snapshots/")
    for part in (saved, f"{saved}^2"):  # the working tree and the index it saved
        assert "uncommitted-secret" not in git(repo, "show", f"{part}:.env")
    assert "work in progress" in git(repo, "show", f"{saved}:app.py")
    run(ops, "git.stash", root=root(repo), action="apply", ref=saved)  # and it still comes back like a stash
    assert (repo / "app.py").read_bytes() == b"print('work in progress')\n"
    assert b"uncommitted-secret" in (repo / ".env").read_bytes()  # the owner's own change, untouched throughout
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", str(remote))
    git(repo, "remote", "add", "origin", str(remote))
    for name in (saved, saved.removeprefix("refs/")):
        with pytest.raises(WebCoderError) as err:
            run(ops, "git.sync", root=root(repo), action="push", remote="origin", branch=name, to="leak")
        assert code(err) == "INVALID_ARGUMENT"


def test_throwing_work_away_keeps_a_snapshot(ops, repo):
    (repo / "app.py").write_bytes(b"print('unsaved work')\n")
    result = run(ops, "git.reset", root=root(repo), mode="hard")
    assert result["snapshot"].startswith("refs/webcoder/snapshots/")
    assert (repo / "app.py").read_bytes() == b"print('v1')\n"
    run(ops, "git.stash", root=root(repo), action="apply", ref=result["snapshot"])
    assert (repo / "app.py").read_bytes() == b"print('unsaved work')\n"  # and it comes back
    restored = run(ops, "git.restore", root=root(repo), paths=["app.py"])
    assert restored["snapshot"] and (repo / "app.py").read_bytes() == b"print('v1')\n"
    assert run(ops, "git.reset", root=root(repo), mode="hard")["snapshot"] is None  # nothing to save


def test_branches_merges_and_conflicts(ops, repo):
    run(ops, "git.branch", root=root(repo), action="create", name="feature/x")
    (repo / "app.py").write_bytes(b"print('feature')\n")
    run(ops, "git.commit", root=root(repo), message="feature", every=True)
    run(ops, "git.branch", root=root(repo), action="switch", name="main")
    (repo / "app.py").write_bytes(b"print('main')\n")
    run(ops, "git.commit", root=root(repo), message="main change", every=True)
    conflict = run(ops, "git.integrate", root=root(repo), operation="merge", ref="feature/x")
    assert conflict["exit_code"] != 0 and "CONFLICT" in conflict["stdout"] + conflict["stderr"]
    (repo / "notes.md").write_bytes(b"half-resolved\n")
    git(repo, "add", "notes.md")
    with pytest.raises(WebCoderError) as err:  # unresolved conflicts cannot be saved first: nothing is thrown away
        run(ops, "git.reset", root=root(repo), mode="hard")
    assert code(err) == "GIT_FAILED" and "git_integrate" in err.value.message
    assert (repo / "notes.md").exists() and b"<<<<<<<" in (repo / "app.py").read_bytes()
    (repo / ".env").write_bytes(b"TOKEN=staged-during-the-merge\n")
    git(repo, "add", ".env")
    with pytest.raises(WebCoderError) as err:  # continuing commits the index: not with a secret staged in it
        run(ops, "git.integrate", root=root(repo), operation="merge", step="continue")
    assert code(err) == "GIT_SECRETS_STAGED"
    with pytest.raises(WebCoderError) as err:  # aborting resets what is staged: the owner's .env change with it
        run(ops, "git.integrate", root=root(repo), operation="merge", step="abort")
    assert code(err) == "GIT_SECRETS_CHANGED" and (repo / ".env").read_bytes() == b"TOKEN=staged-during-the-merge\n"
    git(repo, "checkout", "HEAD", "--", ".env")
    assert run(ops, "git.integrate", root=root(repo), operation="merge", step="abort")["exit_code"] == 0
    assert (repo / "app.py").read_bytes() == b"print('main')\n"
    stopped = run(ops, "git.integrate", root=root(repo), operation="rebase", ref="feature/x")
    assert stopped["exit_code"] != 0
    (repo / ".env").write_bytes(b"TOKEN=edited-during-the-rebase\n")  # not staged: a rebase abort resets it too
    for step in ("abort", "skip"):
        with pytest.raises(WebCoderError) as err:
            run(ops, "git.integrate", root=root(repo), operation="rebase", step=step)
        assert code(err) == "GIT_SECRETS_CHANGED" and b"edited-during" in (repo / ".env").read_bytes(), step
    git(repo, "checkout", "HEAD", "--", ".env")
    assert run(ops, "git.integrate", root=root(repo), operation="rebase", step="abort")["exit_code"] == 0
    assert (repo / "app.py").read_bytes() == b"print('main')\n" and git(repo, "branch", "--show-current") == "main\n"
    tag = run(ops, "git.tag", root=root(repo), action="create", name="v1.0", message="release")
    assert tag["exit_code"] == 0 and "v1.0" in git(repo, "tag")
    assert run(ops, "git.branch", root=root(repo), action="delete", name="feature/x", force=True)["exit_code"] == 0


def test_a_push_checks_what_the_remote_has_now(ops, repo, tmp_path):
    """The owner published a commit that changes .env, then took it back on the remote (a forced push from
    elsewhere). This clone has not fetched since: origin/main still shows that commit, but pushing main would
    bring it back. What the remote has is asked at the push, not taken from the last fetch."""
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", str(remote))
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "push", "-q", "origin", "main")  # by hand: the first commit tracks .env
    clean = git(repo, "rev-parse", "HEAD").strip()
    (repo / ".env").write_bytes(b"TOKEN=secret-v2\n")
    git(repo, "commit", "-qam", "rotate the token")
    git(repo, "push", "-q", "origin", "main")
    git(remote, "update-ref", "refs/heads/main", clean)  # taken back there; origin/main here is out of date
    (repo / "app.py").write_bytes(b"print('v2')\n")
    run(ops, "git.commit", root=root(repo), message="v2", every=True)
    with pytest.raises(WebCoderError) as err:
        run(ops, "git.sync", root=root(repo), action="push", remote="origin", branch="main")
    assert code(err) == "GIT_SECRETS_PUSH" and ".env" in err.value.message
    assert git(remote, "rev-parse", "main").strip() == clean
    git(repo, "reset", "-q", "--hard", clean)  # the owner drops the commit here too: nothing secret is new
    (repo / "app.py").write_bytes(b"print('v3')\n")
    run(ops, "git.commit", root=root(repo), message="v3", every=True)
    pushed = run(ops, "git.sync", root=root(repo), action="push", remote="origin", branch="main")
    assert pushed["exit_code"] == 0 and git(remote, "log", "-1", "--format=%s", "main") == "v3\n"


def test_push_and_fetch_only_to_configured_remotes(ops, repo, tmp_path):
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", str(remote))
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "config", "remote.origin.pushurl", str(remote))
    with pytest.raises(WebCoderError) as err:  # the first commit tracks .env, and no remote has it yet
        run(ops, "git.sync", root=root(repo), action="push", remote="origin", branch="main")
    assert code(err) == "GIT_SECRETS_PUSH" and ".env" in err.value.message and not git(remote, "branch")
    git(repo, "push", "-q", "origin", "main")  # the owner publishes it, by hand
    (repo / "app.py").write_bytes(b"print('v2')\n")
    run(ops, "git.commit", root=root(repo), message="v2", every=True)
    pushed = run(ops, "git.sync", root=root(repo), action="push", remote="origin", branch="main", set_upstream=True)
    assert pushed["exit_code"] == 0 and "v2" in git(remote, "log", "-1", "--format=%s", "main")
    public = tmp_path / "public.git"
    git(tmp_path, "init", "-q", "--bare", str(public))
    git(repo, "remote", "add", "public", str(public))
    with pytest.raises(WebCoderError) as err:  # origin has the .env history, public does not: it counts per remote
        run(ops, "git.sync", root=root(repo), action="push", remote="public", branch="main")
    assert code(err) == "GIT_SECRETS_PUSH" and "public" in err.value.message and not git(public, "branch")
    git(repo, "config", "remote.origin.pushurl", str(public))  # fetched from origin, pushed somewhere else
    with pytest.raises(WebCoderError) as err:  # what origin's refs show says nothing about that place
        run(ops, "git.sync", root=root(repo), action="push", remote="origin", branch="main")
    assert code(err) == "GIT_SECRETS_PUSH" and not git(public, "branch")
    git(repo, "config", "remote.origin.pushurl", str(remote))
    (repo / "app.py").write_bytes(b"print('v3')\n")
    run(ops, "git.commit", root=root(repo), message="v3", every=True)
    git(repo, "config", "push.default", "matching")  # would push every branch: the tool pushes this one only
    to_upstream = run(ops, "git.sync", root=root(repo), action="push")  # where git would push it (@{push})
    assert to_upstream["exit_code"] == 0 and "origin main:main" in to_upstream["command"]
    assert "v3" in git(remote, "log", "-1", "--format=%s", "main")
    assert run(ops, "git.sync", root=root(repo), action="fetch", remote="origin")["exit_code"] == 0
    elsewhere = tmp_path / "elsewhere"  # someone else pushes a file this clone keeps ignored, locally
    git(tmp_path, "clone", "-q", "-b", "main", str(remote), str(elsewhere))
    (elsewhere / "local.cfg").write_bytes(b"from the remote\n")
    git(elsewhere, "add", "local.cfg")
    git(elsewhere, "-c", "user.name=T", "-c", "user.email=t@example.com", "commit", "-q", "-m", "add local.cfg")
    git(elsewhere, "push", "-q")
    (repo / ".git" / "info" / "exclude").write_bytes(b"local.cfg\n")
    (repo / "local.cfg").write_bytes(b"mine\n")
    with pytest.raises(WebCoderError) as err:  # git would overwrite the ignored file
        run(ops, "git.sync", root=root(repo), action="pull", remote="origin", branch="main")
    assert code(err) == "GIT_UNTRACKED_IN_THE_WAY" and (repo / "local.cfg").read_bytes() == b"mine\n"
    (repo / "local.cfg").unlink()
    pulled = run(ops, "git.sync", root=root(repo), action="pull")  # from the upstream: the commit checked above
    assert pulled["exit_code"] == 0 and (repo / "local.cfg").read_bytes() == b"from the remote\n"
    assert pulled["command"].startswith("git fetch") and "&& git merge" in pulled["command"]
    (elsewhere / "other.txt").write_bytes(b"theirs\n")  # both sides move on: the histories diverge
    git(elsewhere, "add", "other.txt")
    git(elsewhere, "-c", "user.name=T", "-c", "user.email=t@example.com", "commit", "-q", "-m", "theirs")
    git(elsewhere, "push", "-q")
    (repo / "mine.txt").write_bytes(b"mine\n")
    git(repo, "add", "mine.txt")
    git(repo, "commit", "-q", "-m", "mine")
    git(repo, "config", "pull.rebase", "true")
    mine = git(repo, "rev-parse", "HEAD")
    diverged = run(ops, "git.sync", root=root(repo), action="pull", ff_only=True)  # the setting says rebase
    assert diverged["exit_code"] != 0 and git(repo, "rev-parse", "HEAD") == mine  # not rewritten
    (repo / ".env").write_bytes(b"TOKEN=never-committed\n")  # the owner's own stash holds an uncommitted secret
    git(repo, "stash", "push", "-q")
    run(ops, "git.branch", root=root(repo), action="create", name="from-stash", start="stash@{0}")
    for args in ({"branch": "from-stash"}, {"branch": "main", "tags": True}):
        if args.get("tags"):
            git(repo, "tag", "leak", "stash@{0}")
        with pytest.raises(WebCoderError) as err:  # made into a branch or a tag, it still never leaves
            run(ops, "git.sync", root=root(repo), action="push", remote="origin", **args)
        assert code(err) == "GIT_SECRETS_PUSH" and ".env" in err.value.message, args
    assert "never-committed" not in git(remote, "log", "--all", "-p")
    git(repo, "remote", "add", "tokens", "https://user:ghp_secret123@example.com/x.git")
    git(repo, "remote", "add", "signed", "s3://bucket/x.git?X-Amz-Signature=sig456&X-Amz-Credential=cred789")
    overview = run(ops, "git.branches", root=root(repo))["stdout"]
    assert "ghp_secret123" not in overview and "https://***@example.com/x.git" in overview
    assert "sig456" not in overview and "cred789" not in overview and "s3://bucket/x.git?***" in overview
    failed = run(ops, "git.sync", root=root(repo), action="fetch", remote="signed")  # no such helper here
    assert "sig456" not in failed["stdout"] + failed["stderr"]


def test_where_git_may_run(ops, repo, tmp_path):
    inner = repo / "sub"
    inner.mkdir()
    nested = FileOps(Sandbox([repo]), BackupStore(tmp_path / "b2"))
    nested.git = GitOps(nested)
    with pytest.raises(WebCoderError) as err:  # a project inside a larger repository could reach outside it
        run(nested, "git.status", root=str(inner))
    assert code(err) == "GIT_NOT_REPOSITORY_ROOT"
    plain = tmp_path / "plain"
    plain.mkdir()
    fresh = FileOps(Sandbox([plain]), BackupStore(tmp_path / "b3"))
    fresh.git = GitOps(fresh)
    with pytest.raises(WebCoderError) as err:
        run(fresh, "git.status", root=str(plain))
    assert code(err) == "GIT_NOT_A_REPOSITORY"
    assert run(fresh, "git.init", root=str(plain), initial_branch="main")["exit_code"] == 0
    assert run(fresh, "git.status", root=str(plain))["exit_code"] == 0
    readonly = FileOps(Sandbox([repo], read_only=True), BackupStore(tmp_path / "b4"))
    readonly.git = GitOps(readonly)
    assert run(readonly, "git.status", root=root(repo))["exit_code"] == 0
    with pytest.raises(WebCoderError) as err:
        run(readonly, "git.commit", root=root(repo), message="x", every=True)
    assert code(err) == "AGENT_READ_ONLY"


def test_a_project_that_stops_being_a_repository_is_noticed(tmp_path):
    outer = make_repo(tmp_path / "outer")
    work = make_repo(outer / "work")
    files = FileOps(Sandbox([work]), BackupStore(tmp_path / "b"))
    files.git = GitOps(files)
    assert run(files, "git.status", root=str(work))["exit_code"] == 0
    shutil.rmtree(work / ".git", onerror=lambda func, path, _: (os.chmod(path, 0o700), func(path)))
    with pytest.raises(WebCoderError) as err:  # now inside the outer repository: git would reach beyond the project
        run(files, "git.status", root=str(work))
    assert code(err) == "GIT_NOT_REPOSITORY_ROOT"


def test_the_agents_own_folder_stays_out_of_git(repo, tmp_path):
    """A repository can contain the agent's folder (a home folder under git): its device secret is never staged,
    committed, shown or cleaned away."""
    agent = repo / ".webcoder-agent"
    agent.mkdir()
    (agent / "config.json").write_bytes(b'{"device_secret": "s3cret"}')
    nested = repo / ".local" / "share" / "webcoder"  # also when it sits deep inside an untracked folder
    nested.mkdir(parents=True)
    (nested / "config.json").write_bytes(b'{"device_secret": "s3cret"}')
    files = FileOps(Sandbox([repo], protected_dirs=[agent, nested]), BackupStore(tmp_path / "b"))
    files.git = GitOps(files, hooks_dir=tmp_path / "no-hooks")
    run(files, "git.add", root=root(repo), every=True)
    assert "webcoder" not in git(repo, "diff", "--cached", "--name-only")
    cleaned = run(files, "git.clean", root=root(repo), directories=True)  # a dry run lists what would go
    kept = cleaned["stdout"].split("Kept")
    assert ".webcoder-agent" not in kept[0] and ".local" in kept[1]
    git(repo, "add", "-f", ".webcoder-agent/config.json")  # the owner staged it by hand
    with pytest.raises(WebCoderError) as err:
        run(files, "git.commit", root=root(repo), message="x")
    assert code(err) == "GIT_AGENT_FILES_TRACKED"
    git(repo, "commit", "-q", "-m", "committed by the owner")
    assert "s3cret" not in run(files, "git.show", root=root(repo))["stdout"]  # reading goes on, without it
    for op, args in (("git.reset", {"mode": "hard", "ref": "HEAD~1"}), ("git.integrate", {"operation": "revert",
                                                                                          "ref": "HEAD"})):
        with pytest.raises(WebCoderError) as err:  # nothing may rewrite the running agent's files
            run(files, op, root=root(repo), **args)
        assert code(err) == "GIT_AGENT_FILES_TRACKED", op
    assert (agent / "config.json").read_bytes() == b'{"device_secret": "s3cret"}'


def test_git_settings_the_ai_could_change_turn_the_tools_off(ops, repo, monkeypatch):
    """Hooks are off, but filters, merge drivers, signing programs ... run too: never one from the project, and
    never settings read from a file in the project."""
    git(repo, "config", "filter.fmt.clean", "./tools/clean.sh %f")
    with pytest.raises(WebCoderError) as err:
        run(ops, "git.status", root=root(repo))
    assert code(err) == "GIT_PROJECT_SETTINGS" and "filter.fmt.clean" in err.value.message
    git(repo, "config", "--unset", "filter.fmt.clean")
    (repo / ".gitconfig").write_bytes(b"[commit]\n\tgpgsign = true\n")  # a shared, checked-in .gitconfig
    git(repo, "config", "include.path", "../.gitconfig")
    with pytest.raises(WebCoderError) as err:
        run(ops, "git.commit", root=root(repo), message="x", every=True)
    assert code(err) == "GIT_PROJECT_SETTINGS" and ".gitconfig" in err.value.message
    git(repo, "config", "--unset", "include.path")
    git(repo, "config", "filter.lfs.clean", "git-lfs clean -- %f")  # programs of this computer are fine
    git(repo, "config", "gpg.program", "C:/Program Files/GnuPG/bin/gpg.exe")
    assert run(ops, "git.status", root=root(repo))["exit_code"] == 0
    git(repo, "config", "filter.fmt.clean", "sh helper.sh")  # a bare name: the file at the project's top
    assert run(ops, "git.status", root=root(repo))["exit_code"] == 0  # none there yet
    (repo / "helper.sh").write_bytes(b"echo pwned\n")  # written by the AI, say
    with pytest.raises(WebCoderError) as err:
        run(ops, "git.status", root=root(repo))
    assert code(err) == "GIT_PROJECT_SETTINGS" and "filter.fmt.clean" in err.value.message
    git(repo, "config", "--unset", "filter.fmt.clean")
    monkeypatch.setenv("GIT_SSH_COMMAND", f"sh {repo / 'scripts' / 'ssh-wrapper.sh'}")  # from the agent's start
    with pytest.raises(WebCoderError) as err:
        run(ops, "git.status", root=root(repo))
    assert code(err) == "GIT_PROJECT_SETTINGS" and "GIT_SSH_COMMAND" in err.value.message


def test_git_settings_from_another_allowed_folder_turn_the_tools_off(repo, tmp_path):
    """The AI can change files in every folder this agent opens, not only this project's: a setting that runs a
    script from another of them, or an include from there, turns the git tools off as well."""
    other = tmp_path / "other"
    other.mkdir()
    (other / "clean.sh").write_bytes(b"cat\n")
    (other / "shared.gitconfig").write_bytes(b"[core]\n\tautocrlf = false\n")
    files = FileOps(Sandbox([repo, other]), BackupStore(tmp_path / "backups"))
    files.git = GitOps(files, hooks_dir=tmp_path / "no-hooks")
    git(repo, "config", "filter.fmt.clean", f"sh {(other / 'clean.sh').as_posix()}")
    with pytest.raises(WebCoderError) as err:
        run(files, "git.status", root=root(repo))
    assert code(err) == "GIT_PROJECT_SETTINGS" and "filter.fmt.clean" in err.value.message
    only_repo = FileOps(Sandbox([repo]), BackupStore(tmp_path / "backups2"))  # there the folder is not open
    only_repo.git = GitOps(only_repo, hooks_dir=tmp_path / "no-hooks")
    assert run(only_repo, "git.status", root=root(repo))["exit_code"] == 0
    git(repo, "config", "--unset", "filter.fmt.clean")
    git(repo, "config", "include.path", (other / "shared.gitconfig").as_posix())
    with pytest.raises(WebCoderError) as err:
        run(files, "git.status", root=root(repo))
    assert code(err) == "GIT_PROJECT_SETTINGS" and "shared.gitconfig" in err.value.message
    git(repo, "config", "--unset", "include.path")
    assert run(files, "git.status", root=root(repo))["exit_code"] == 0


def test_safety_checks_never_work_from_a_cut_list(ops, repo, monkeypatch):
    (repo / "junk").mkdir()
    (repo / "junk" / "a.log").write_bytes(b"x")
    monkeypatch.setattr(gitops_module, "MAX_LISTING", 5)  # as if the repository listed more than a check can hold
    for op, args in (("git.reset", {"mode": "hard"}), ("git.clean", {"directories": True, "dry_run": False})):
        with pytest.raises(WebCoderError) as err:
            run(ops, op, root=root(repo), **args)
        assert code(err) == "GIT_FAILED" and "nothing was" in err.value.message, op
    assert (repo / "junk" / "a.log").exists()


def test_untracked_files_are_never_overwritten_without_a_snapshot(ops, repo):
    """A hard reset, a forced switch or a restore from another commit would write over untracked and ignored files,
    which no snapshot keeps: refused, and nothing changes."""
    git(repo, "switch", "-q", "-c", "other")
    (repo / "notes.txt").write_bytes(b"committed on other\n")
    (repo / "docs").mkdir()
    (repo / "docs" / "a.md").write_bytes(b"# a\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "other")
    git(repo, "switch", "-q", "main")
    (repo / "notes.txt").write_bytes(b"my untracked notes\n")
    (repo / "docs").write_bytes(b"an untracked file where other has a folder\n")
    for op, args in (("git.branch", {"action": "switch", "name": "other", "discard_changes": True}),
                     ("git.reset", {"mode": "hard", "ref": "other"}),
                     ("git.restore", {"paths": ["notes.txt"], "source": "other"})):
        with pytest.raises(WebCoderError) as err:
            run(ops, op, root=root(repo), **args)
        assert code(err) == "GIT_UNTRACKED_IN_THE_WAY" and "notes.txt" in err.value.message, op
    assert "docs" in err.value.message or op == "git.restore"
    assert (repo / "notes.txt").read_bytes() == b"my untracked notes\n" and git(repo, "branch", "--show-current") == \
        "main\n"
    (repo / "app.py").unlink()  # a tracked file turned into a folder of untracked work
    (repo / "app.py").mkdir()
    (repo / "app.py" / "draft.txt").write_bytes(b"not in git\n")
    with pytest.raises(WebCoderError) as err:
        run(ops, "git.reset", root=root(repo), mode="hard")
    assert code(err) == "GIT_UNTRACKED_IN_THE_WAY" and "app.py" in err.value.message
    assert (repo / "app.py" / "draft.txt").exists()
    shutil.rmtree(repo / "app.py")
    git(repo, "checkout", "--", "app.py")
    (repo / "notes.txt").unlink()
    (repo / "docs").unlink()
    # An ignored file: git itself would overwrite it on a plain switch, a merge ... (they are "expendable")
    (repo / ".git" / "info" / "exclude").write_bytes(b"local.cfg\n")
    (repo / "local.cfg").write_bytes(b"my local settings\n")
    git(repo, "switch", "-q", "other")
    (repo / "local.cfg").rename(repo / "keep.tmp")
    (repo / "local.cfg").write_bytes(b"committed on other\n")
    git(repo, "add", "-f", "local.cfg")
    git(repo, "commit", "-q", "-m", "track local.cfg")
    git(repo, "switch", "-q", "main")
    (repo / "keep.tmp").rename(repo / "local.cfg")
    for op, args in (("git.branch", {"action": "switch", "name": "other"}),
                     ("git.branch", {"action": "detach", "name": "other"}),
                     ("git.branch", {"action": "create", "name": "topic", "start": "other"}),
                     ("git.integrate", {"operation": "merge", "ref": "other"}),
                     ("git.integrate", {"operation": "rebase", "ref": "other"}),
                     ("git.integrate", {"operation": "cherry_pick", "ref": "other"})):
        with pytest.raises(WebCoderError) as err:
            run(ops, op, root=root(repo), **args)
        assert code(err) == "GIT_UNTRACKED_IN_THE_WAY" and "local.cfg" in err.value.message, args
    assert (repo / "local.cfg").read_bytes() == b"my local settings\n" and git(repo, "branch", "--show-current") == \
        "main\n"
    (repo / "local.cfg").unlink()
    (repo / "app.py").write_bytes(b"print('my version')\n")
    git(repo, "rm", "-q", "--cached", "app.py")  # untracked now; unstaging brings HEAD's back over it
    with pytest.raises(WebCoderError) as err:
        run(ops, "git.restore", root=root(repo), paths=["app.py"], staged=True)
    assert code(err) == "GIT_UNTRACKED_IN_THE_WAY" and (repo / "app.py").read_bytes() == b"print('my version')\n"
    git(repo, "add", "app.py")
    git(repo, "checkout", "--", "app.py")
    git(repo, "reset", "-q", "--hard")
    assert run(ops, "git.branch", root=root(repo), action="switch", name="other", discard_changes=True)[
        "exit_code"] == 0


def test_a_rebase_checks_the_files_its_replayed_commits_write(ops, repo):
    """A local commit added notes.txt and a later one deleted it; notes.txt is now an ignored local file. Replaying
    those commits writes it (and then deletes it): git itself goes ahead, as ignored files are expendable to it."""
    git(repo, "switch", "-q", "-c", "feature")
    (repo / "notes.txt").write_bytes(b"committed, then dropped\n")
    git(repo, "add", "notes.txt")
    git(repo, "commit", "-q", "-m", "add notes")
    git(repo, "rm", "-q", "notes.txt")
    git(repo, "commit", "-q", "-m", "drop notes")
    git(repo, "switch", "-q", "main")
    (repo / "more.txt").write_bytes(b"more\n")
    git(repo, "add", "more.txt")
    git(repo, "commit", "-q", "-m", "more")
    git(repo, "switch", "-q", "feature")
    (repo / ".git" / "info" / "exclude").write_bytes(b"notes.txt\n")
    (repo / "notes.txt").write_bytes(b"my own ignored notes\n")
    with pytest.raises(WebCoderError) as err:
        run(ops, "git.integrate", root=root(repo), operation="rebase", ref="main")
    assert code(err) == "GIT_UNTRACKED_IN_THE_WAY" and "notes.txt" in err.value.message
    assert (repo / "notes.txt").read_bytes() == b"my own ignored notes\n"
    (repo / "notes.txt").unlink()
    assert run(ops, "git.integrate", root=root(repo), operation="rebase", ref="main")["exit_code"] == 0


def test_going_on_after_a_conflict_checks_the_commits_still_to_apply(ops, repo):
    """A rebase or cherry-pick stopped at its first commit; meanwhile an ignored later.txt appeared, a path a later
    commit writes. continue (and skip) would go on and write over it: checked again, not only at the start."""
    git(repo, "switch", "-q", "-c", "feature")
    (repo / "app.py").write_bytes(b"print('feature')\n")
    git(repo, "commit", "-q", "-am", "change app")
    (repo / "later.txt").write_bytes(b"from the second commit\n")
    git(repo, "add", "later.txt")
    git(repo, "commit", "-q", "-m", "add later")
    picks = git(repo, "rev-list", "--reverse", "main..feature").split()
    git(repo, "switch", "-q", "main")
    (repo / "app.py").write_bytes(b"print('main')\n")
    git(repo, "commit", "-q", "-am", "main change")
    (repo / ".git" / "info" / "exclude").write_bytes(b"later.txt\n")
    for start, operation in ((lambda: run(ops, "git.integrate", root=root(repo), operation="cherry_pick", refs=picks),
                              "cherry_pick"),
                             (lambda: (git(repo, "switch", "-q", "feature"),
                                       run(ops, "git.integrate", root=root(repo), operation="rebase", ref="main"))[1],
                              "rebase")):
        assert start()["exit_code"] != 0  # stopped at the conflict in app.py
        (repo / "later.txt").write_bytes(b"my own ignored notes\n")
        (repo / "app.py").write_bytes(b"print('both')\n")
        git(repo, "add", "app.py")
        for step in ("continue", "skip"):
            with pytest.raises(WebCoderError) as err:
                run(ops, "git.integrate", root=root(repo), operation=operation, step=step)
            assert code(err) == "GIT_UNTRACKED_IN_THE_WAY" and "later.txt" in err.value.message, (operation, step)
        assert (repo / "later.txt").read_bytes() == b"my own ignored notes\n"
        (repo / "later.txt").unlink()
        assert run(ops, "git.integrate", root=root(repo), operation=operation, step="abort")["exit_code"] == 0


def test_a_pull_merges_the_branch_it_fetched(ops, repo, tmp_path):
    """The remote's refspec covers main only (a single-branch clone): pulling feature fetches it into
    origin/feature all the same, and merges that, not a missing or old origin/feature."""
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", str(remote))
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "push", "-q", "origin", "main")
    other = tmp_path / "other"
    git(tmp_path, "clone", "-q", "-b", "main", str(remote), str(other))
    git(other, "switch", "-q", "-c", "feature")
    (other / "feature.txt").write_bytes(b"from feature\n")
    git(other, "add", "feature.txt")
    git(other, "-c", "user.name=T", "-c", "user.email=t@example.com", "commit", "-q", "-m", "feature")
    git(other, "push", "-q", "origin", "feature")
    git(repo, "config", "remote.origin.fetch", "+refs/heads/main:refs/remotes/origin/main")
    pulled = run(ops, "git.sync", root=root(repo), action="pull", remote="origin", branch="feature")
    assert pulled["exit_code"] == 0, pulled
    assert (repo / "feature.txt").read_bytes() == b"from feature\n"
    assert git(repo, "rev-parse", "refs/remotes/origin/feature") == git(other, "rev-parse", "HEAD")


def test_untracked_files_in_a_tracked_file_turned_folder_are_checked_too(ops, repo):
    """git tracks the file `data`; here it became a folder with untracked work in it, and the target commit has a
    folder `data` with a file of the same name: that file would be written over."""
    (repo / "data").write_bytes(b"tracked file\n")
    git(repo, "add", "data")
    git(repo, "commit", "-q", "-m", "data is a file")
    git(repo, "switch", "-q", "-c", "other")
    git(repo, "rm", "-q", "data")
    (repo / "data").mkdir()
    (repo / "data" / "rows.csv").write_bytes(b"theirs\n")
    git(repo, "add", "data")
    git(repo, "commit", "-q", "-m", "data is a folder")
    git(repo, "switch", "-q", "main")
    (repo / "data").unlink()
    (repo / "data").mkdir()
    (repo / "data" / "rows.csv").write_bytes(b"my untracked rows\n")
    for op, args in (("git.reset", {"mode": "hard", "ref": "other"}),
                     ("git.branch", {"action": "switch", "name": "other", "discard_changes": True}),
                     ("git.integrate", {"operation": "merge", "ref": "other"})):
        with pytest.raises(WebCoderError) as err:
            run(ops, op, root=root(repo), **args)
        assert code(err) == "GIT_UNTRACKED_IN_THE_WAY" and "data/rows.csv" in err.value.message, op
    assert (repo / "data" / "rows.csv").read_bytes() == b"my untracked rows\n"
    assert git(repo, "branch", "--show-current") == "main\n"


# ------------------------------------------------------------------ through the hub
def test_git_tools_through_the_hub(hub, start_agent, tmp_path):
    work = make_repo(tmp_path / "hub-work")
    device_id, _ = start_agent("pc", work)
    hub.add_project("app", device_id, work)
    hub.add_project("frozen", device_id, work, mode="ro")
    names = {tool["name"] for tool in hub.rpc(hub.create_token(), "tools/list").json()["result"]["tools"]}
    assert {"git_status", "git_diff", "git_commit", "git_sync", "git_integrate"} <= names
    reader = hub.create_token(scopes=("read",), label="reader")
    (work / "app.py").write_bytes(b"print('v2')\n")
    assert "+print('v2')" in hub.ok(reader, "git_diff", project="app")
    text, is_error = hub.call(reader, "git_commit", project="app", message="x", all=True)
    assert is_error and "INSUFFICIENT_SCOPE" in text
    writer = hub.create_token(scopes=("read", "write"), label="writer")
    text, is_error = hub.call(writer, "git_commit", project="frozen", message="x", all=True)
    assert is_error and "PROJECT_READ_ONLY" in text
    done = hub.ok(writer, "git_commit", project="app", message="from chatgpt", all=True)
    assert "exit code 0" in done and "from chatgpt" in git(work, "log", "-1", "--format=%s")
    text, is_error = hub.call(writer, "git_integrate", project="app", operation="merge", ref="nope")
    assert is_error and "GIT_FAILED" in text
    audit = hub.runtime.store.all("SELECT action, target, status FROM audit WHERE action LIKE 'git_%'")
    committed = [row["target"] for row in audit if row["action"] == "git_commit" and row["status"] == "ok"]
    assert len(committed) == 1 and committed[0].startswith("app: git add --update -- . :(exclude,glob,icase)**/")
    assert committed[0].endswith(" && git commit --no-verify -m from chatgpt")  # both steps, in full
