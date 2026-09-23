"""The shell scripts the agent runs on servers (over ssh) and phones (adb / hdc shell), run in a real POSIX
shell rather than only against the fakes: noclobber, the size check, backup and clean-up must really work."""
import os
import stat
import subprocess

from tests.conftest import posix_shell
from webcoder.agent.mobile import move_into_place_script
from webcoder.agent.remote import _remote_path, exists_script, remote_quote, upload_script


def run(script: str, data: bytes = b"") -> subprocess.CompletedProcess:
    return subprocess.run([posix_shell(), "-c", script], input=data, capture_output=True, timeout=60)


def names(folder) -> list[str]:
    return sorted(path.name for path in folder.iterdir())


def test_upload_creates_new_files_only_whole_and_only_once(tmp_path):
    target, stem = (tmp_path / "app.conf").as_posix(), (tmp_path / "app.conf.webcoder-1").as_posix()
    done = run(upload_script(target, stem, 4, overwrite=False), b"ours")
    assert done.returncode == 0 and (tmp_path / "app.conf").read_bytes() == b"ours"
    again = run(upload_script(target, stem, 5, overwrite=False), b"again")  # noclobber: never replaced
    assert again.returncode != 0 and b"exist" in again.stderr.lower()
    assert (tmp_path / "app.conf").read_bytes() == b"ours"
    cut = run(upload_script((tmp_path / "new.conf").as_posix(), stem, 10, overwrite=False), b"only 7")
    assert cut.returncode == 1 and b"WEBCODER_INCOMPLETE" in cut.stderr and not (tmp_path / "new.conf").exists()
    assert names(tmp_path) == ["app.conf"]  # no temporary copies left


def test_upload_overwrites_in_place_and_never_with_a_partial_file(tmp_path):
    target, stem = (tmp_path / "app.conf").as_posix(), (tmp_path / "app.conf.webcoder-2").as_posix()
    (tmp_path / "app.conf").write_bytes(b"old content\n")
    cut = run(upload_script(target, stem, 100, overwrite=True), b"the connection dropped")
    assert cut.returncode == 1 and b"WEBCODER_INCOMPLETE" in cut.stderr
    assert (tmp_path / "app.conf").read_bytes() == b"old content\n"
    done = run(upload_script(target, stem, 4, overwrite=True), b"new!")
    assert done.returncode == 0 and (tmp_path / "app.conf").read_bytes() == b"new!"
    fresh = run(upload_script((tmp_path / "b.conf").as_posix(), stem, 2, overwrite=True), b"hi")
    assert fresh.returncode == 0 and (tmp_path / "b.conf").read_bytes() == b"hi"
    assert names(tmp_path) == ["app.conf", "b.conf"]


def test_a_failed_overwrite_puts_the_original_back(tmp_path):
    target, stem = (tmp_path / "app.conf").as_posix(), (tmp_path / "app.conf.webcoder-3").as_posix()
    (tmp_path / "app.conf").write_bytes(b"precious original\n")
    script = upload_script(target, stem, 20, overwrite=True)
    data = b"twenty bytes of new!"
    no_room = run("cp() { return 1; }; " + script, data)  # the backup cannot be made: nothing is touched
    assert no_room.returncode == 1 and b"WEBCODER_NO_BACKUP" in no_room.stderr
    assert (tmp_path / "app.conf").read_bytes() == b"precious original\n" and names(tmp_path) == ["app.conf"]
    # writing the target breaks off after a few bytes (disk full): the backup is written back
    flag = tmp_path / "fail-once"
    flag.write_bytes(b"")
    fail_once = (f"cat() {{ if [ \"$1\" = '{stem}.tmp' ] && [ -e '{flag.as_posix()}' ]; then "
                 f"rm -f '{flag.as_posix()}'; command cat \"$@\" | head -c 3; return 1; fi; command cat \"$@\"; }}; ")
    restored = run(fail_once + script, data)
    assert restored.returncode == 1 and b"WEBCODER_RESTORED" in restored.stderr
    assert (tmp_path / "app.conf").read_bytes() == b"precious original\n" and names(tmp_path) == ["app.conf"]
    # and when even that fails, the backup stays for the owner to recover
    always_fail = "cat() { if [ -n \"$1\" ]; then return 1; fi; command cat; }; "
    kept = run(always_fail + script, data)
    assert kept.returncode == 1 and b"WEBCODER_BACKUP_KEPT" in kept.stderr
    assert (tmp_path / "app.conf.webcoder-3.bak").read_bytes() == b"precious original\n"


def test_new_files_get_their_mode_only_once_written(tmp_path):
    """A new file is written private (umask 077), then given its mode: as asked (exact), or the local file's
    limited by the server's umask. Every variant must run in a real shell."""
    for name, mode, exact in (("secret.json", 0o600, True), ("run.sh", 0o755, False), ("page.html", 0o666, False)):
        target, stem = (tmp_path / name).as_posix(), (tmp_path / f"{name}.webcoder-6").as_posix()
        done = run("umask 022; " + upload_script(target, stem, 2, overwrite=False, mode=mode, exact=exact), b"ok")
        assert done.returncode == 0, done.stderr
        if os.name != "nt":  # Git Bash does not keep POSIX modes on NTFS
            expected = mode if exact else mode & ~0o022
            assert stat.S_IMODE((tmp_path / name).stat().st_mode) == expected, name
    replaced = run(upload_script((tmp_path / "secret.json").as_posix(), (tmp_path / "s.webcoder-7").as_posix(), 3,
                                 overwrite=True, mode=0o640, exact=True), b"new")
    assert replaced.returncode == 0 and (tmp_path / "secret.json").read_bytes() == b"new"
    assert names(tmp_path) == ["page.html", "run.sh", "secret.json"]


def test_a_new_file_that_cannot_be_written_whole_is_removed(tmp_path):
    for overwrite in (False, True):
        target, stem = (tmp_path / "new.conf").as_posix(), (tmp_path / "new.conf.webcoder-9").as_posix()
        disk_full = (f"cat() {{ if [ \"$1\" = '{stem}.tmp' ]; then command cat \"$@\" | head -c 3; return 1; fi; "
                     "command cat \"$@\"; }; ")  # the disk fills up after 3 bytes
        done = run(disk_full + upload_script(target, stem, 10, overwrite=overwrite), b"0123456789")
        assert done.returncode == 1 and b"WEBCODER_REMOVED" in done.stderr, overwrite
        assert names(tmp_path) == []  # neither a partial file (a retry would find it "existing") nor the copy


def test_a_mode_that_cannot_be_set_undoes_the_overwrite(tmp_path):
    target, stem = (tmp_path / "app.conf").as_posix(), (tmp_path / "app.conf.webcoder-8").as_posix()
    (tmp_path / "app.conf").write_bytes(b"original\n")
    no_chmod = "chmod() { return 1; }; "  # e.g. the file belongs to someone else, writable through the group
    done = run(no_chmod + upload_script(target, stem, 3, overwrite=True, mode=0o600, exact=True), b"new")
    assert done.returncode == 1 and b"WEBCODER_RESTORED" in done.stderr
    assert (tmp_path / "app.conf").read_bytes() == b"original\n" and names(tmp_path) == ["app.conf"]


def test_exists_and_move_into_place(tmp_path):
    present, absent = (tmp_path / "a.txt"), (tmp_path / "b.txt")
    present.write_bytes(b"theirs")
    assert run(exists_script(present.as_posix())).stdout.strip() == b"WEBCODER_EXISTS"
    assert run(exists_script(absent.as_posix())).stdout.strip() == b"WEBCODER_MISSING"
    temp = tmp_path / "x.webcoder-4.tmp"
    temp.write_bytes(b"ours")
    assert run(move_into_place_script(temp.as_posix(), present.as_posix())).stdout.strip() == b"WEBCODER_EXISTS"
    assert present.read_bytes() == b"theirs" and temp.exists()  # the agent removes the copy afterwards
    assert run(move_into_place_script(temp.as_posix(), absent.as_posix())).stdout.strip() == b"WEBCODER_MOVED"
    assert absent.read_bytes() == b"ours" and not temp.exists()


def test_names_starting_with_a_dash_are_never_options(tmp_path):
    assert (_remote_path("-"), _remote_path("-n.conf"), _remote_path("/etc/-x")) == ("./-", "./-n.conf", "/etc/-x")
    target = _remote_path("-n.conf")  # a relative name, from the login folder: here, tmp_path
    done = subprocess.run([posix_shell(), "-c", upload_script(target, target + ".webcoder-1", 2, overwrite=False)],
                          input=b"ok", capture_output=True, timeout=60, cwd=tmp_path)
    assert done.returncode == 0 and (tmp_path / "-n.conf").read_bytes() == b"ok" and names(tmp_path) == ["-n.conf"]
    read = subprocess.run([posix_shell(), "-c", f"cat {remote_quote(_remote_path('-'))}"], input=b"stdin, not a file",
                          capture_output=True, timeout=60, cwd=tmp_path)
    assert read.returncode != 0 and read.stdout == b""  # a file named "-", which is not there; never stdin


def test_paths_with_quotes_and_spaces(tmp_path):
    folder = tmp_path / "it's a dir"
    folder.mkdir()
    target, stem = (folder / "a b.txt").as_posix(), (folder / "a b.txt.webcoder-5").as_posix()
    assert run(upload_script(target, stem, 2, overwrite=False), b"ok").returncode == 0
    assert (folder / "a b.txt").read_bytes() == b"ok"
    assert run(exists_script(target)).stdout.strip() == b"WEBCODER_EXISTS"
