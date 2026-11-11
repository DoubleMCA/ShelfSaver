"""The Windows/Linux seam. Each test says which OS it needs; the rest run everywhere."""
from __future__ import annotations

import errno
import os
from types import SimpleNamespace

import pytest

from shelfsaver import hostos

windows_only = pytest.mark.skipif(not hostos.IS_WINDOWS, reason="Windows-only behaviour")
posix_only = pytest.mark.skipif(hostos.IS_WINDOWS, reason="Linux-only behaviour")


# --- try_lock_file ---


def test_a_second_open_of_the_same_file_cannot_take_the_lock(tmp_path):
    # flock (Linux) and msvcrt.locking (Windows) both refuse a second holder, even inside
    # one process, which is what stops two ShelfSaver copies sharing a data folder.
    path = tmp_path / "x.lock"
    with open(path, "a+b") as first, open(path, "a+b") as second:
        assert hostos.try_lock_file(first) is True
        assert hostos.try_lock_file(second) is False


def test_the_lock_is_free_again_once_the_holder_closes(tmp_path):
    path = tmp_path / "x.lock"
    with open(path, "a+b") as first:
        assert hostos.try_lock_file(first) is True
    with open(path, "a+b") as again:
        assert hostos.try_lock_file(again) is True


# --- rename_no_replace ---


def make(path, data=b"x"):
    path.write_bytes(data)
    return path


def test_rename_moves_to_a_free_name(tmp_path):
    source = make(tmp_path / "a")
    hostos.rename_no_replace(str(source), str(tmp_path / "b"))
    assert not source.exists() and (tmp_path / "b").read_bytes() == b"x"


def test_rename_never_replaces_an_existing_file(tmp_path):
    source = make(tmp_path / "a", b"new")
    target = make(tmp_path / "b", b"precious")
    with pytest.raises(FileExistsError):
        hostos.rename_no_replace(str(source), str(target))
    assert source.read_bytes() == b"new" and target.read_bytes() == b"precious"


def test_a_missing_source_is_file_not_found(tmp_path):
    with pytest.raises(FileNotFoundError):
        hostos.rename_no_replace(str(tmp_path / "nope"), str(tmp_path / "b"))


@posix_only
def test_the_fallback_also_refuses_to_replace(tmp_path):
    # The path used on filesystems that lack RENAME_NOREPLACE.
    source = make(tmp_path / "a", b"new")
    target = make(tmp_path / "b", b"precious")
    with pytest.raises(FileExistsError):
        hostos._rename_after_check(str(source), str(target))
    assert target.read_bytes() == b"precious"


@posix_only
def test_a_filesystem_without_noreplace_falls_back_instead_of_failing(tmp_path, monkeypatch):
    import ctypes

    def unsupported(*args):
        ctypes.set_errno(errno.EINVAL)
        return -1

    monkeypatch.setattr(hostos, "_renameat2", unsupported)
    source = make(tmp_path / "a")
    hostos.rename_no_replace(str(source), str(tmp_path / "b"))
    assert (tmp_path / "b").exists()
    # And the fallback still refuses to replace.
    other = make(tmp_path / "c")
    with pytest.raises(FileExistsError):
        hostos.rename_no_replace(str(other), str(tmp_path / "b"))


@posix_only
def test_a_real_failure_is_not_mistaken_for_missing_support(tmp_path, monkeypatch):
    import ctypes

    def denied(*args):
        ctypes.set_errno(errno.EACCES)
        return -1

    monkeypatch.setattr(hostos, "_renameat2", denied)
    source = make(tmp_path / "a")
    with pytest.raises(PermissionError):
        hostos.rename_no_replace(str(source), str(tmp_path / "b"))
    assert source.exists()  # nothing was moved by a silent fallback


# --- volume_root (Linux) ---


@posix_only
@pytest.mark.parametrize(
    "path,expected",
    [
        ("/media/movies/Action", "/media"),
        ("/media", "/media"),
        ("/media2/x", "/"),  # a longer name must not match "/media"
        ("/media/with space/x", "/media/with space"),
        ("/data/x", "/"),
    ],
)
def test_the_longest_containing_mount_point_wins(monkeypatch, path, expected):
    monkeypatch.setattr(hostos, "_mount_points", lambda: ["/", "/media", "/media/with space"])
    monkeypatch.setattr(os.path, "realpath", lambda p: p)
    assert hostos.volume_root(path) == expected


def test_mountinfo_octal_escapes_are_undone():
    assert hostos._unescape_mountinfo("/media/with\\040space") == "/media/with space"
    assert hostos._unescape_mountinfo("/plain") == "/plain"


@posix_only
def test_with_no_mountinfo_it_walks_up_to_a_mount_point(tmp_path, monkeypatch):
    monkeypatch.setattr(hostos, "_mount_points", lambda: [])
    root = hostos.volume_root(tmp_path / "does" / "not" / "exist")
    assert os.path.ismount(root)


@posix_only
def test_a_real_path_resolves_to_a_real_mount_point(tmp_path):
    root = hostos.volume_root(tmp_path / "need" / "not" / "exist")
    assert os.path.isdir(root)
    assert os.path.realpath(tmp_path).startswith(root.rstrip("/") + "/") or root == "/"
    assert not (tmp_path / "need").exists()


# --- is_link_like ---


@posix_only
def test_a_symlink_is_link_like_but_a_file_and_folder_are_not(tmp_path):
    folder = tmp_path / "d"
    folder.mkdir()
    file = make(tmp_path / "f")
    link = tmp_path / "l"
    link.symlink_to(folder, target_is_directory=True)
    assert hostos.is_link_like(os.lstat(link)) is True
    assert hostos.is_link_like(os.lstat(folder)) is False
    assert hostos.is_link_like(os.lstat(file)) is False


# --- jobobject on Linux ---


@posix_only
def test_tying_a_child_to_the_app_is_a_quiet_no_op_on_linux():
    # See jobobject.kill_with_app for why Linux does not attempt this. It must report that
    # it did nothing, and must not raise or touch the process.
    import subprocess
    import sys

    from shelfsaver.jobobject import kill_with_app

    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    try:
        assert kill_with_app(proc) is False
    finally:
        proc.wait()


# --- copy_owner_and_mode: the new file takes the original's owner and permissions ---


def _file(path, data=b"x"):
    path.write_bytes(data)
    return path


def test_windows_does_nothing_and_touches_nothing(tmp_path, monkeypatch):
    # On Windows access rules come from the folder, so nothing is copied and nothing may be called.
    monkeypatch.setattr(hostos, "IS_WINDOWS", True)

    def boom(*args, **kwargs):
        raise AssertionError("must not be called on Windows")

    monkeypatch.setattr(os, "chown", boom, raising=False)
    monkeypatch.setattr(os, "chmod", boom)
    assert hostos.copy_owner_and_mode(str(_file(tmp_path / "a")), str(_file(tmp_path / "b"))) == []


@posix_only
def test_the_permission_bits_are_copied(tmp_path):
    source, target = _file(tmp_path / "s"), _file(tmp_path / "t")
    os.chmod(source, 0o640)
    os.chmod(target, 0o600)
    assert hostos.copy_owner_and_mode(str(source), str(target)) == []
    assert os.stat(target).st_mode & 0o777 == 0o640


@posix_only
def test_special_bits_are_never_carried_onto_the_new_file(tmp_path):
    source, target = _file(tmp_path / "s"), _file(tmp_path / "t")
    os.chmod(source, 0o6755)  # setuid + setgid + rwxr-xr-x
    os.chmod(target, 0o600)
    hostos.copy_owner_and_mode(str(source), str(target))
    assert os.stat(target).st_mode & 0o7777 == 0o755


@posix_only
@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() != 0, reason="changing an owner needs root")
def test_the_owner_and_group_are_copied_when_allowed(tmp_path):
    source, target = _file(tmp_path / "s"), _file(tmp_path / "t")
    os.chown(source, 12345, 23456)
    assert hostos.copy_owner_and_mode(str(source), str(target)) == []
    info = os.stat(target)
    assert (info.st_uid, info.st_gid) == (12345, 23456)


@posix_only
def test_no_chown_is_attempted_when_the_owner_already_matches(tmp_path, monkeypatch):
    source, target = _file(tmp_path / "s"), _file(tmp_path / "t")  # same process, so same owner
    os.chmod(source, 0o640)

    def boom(*args, **kwargs):
        raise AssertionError("chown must not be called when nothing differs")

    monkeypatch.setattr(os, "chown", boom)
    assert hostos.copy_owner_and_mode(str(source), str(target)) == []
    assert os.stat(target).st_mode & 0o777 == 0o640


@posix_only
def test_a_refused_owner_change_is_reported_not_raised_and_the_mode_is_still_copied(tmp_path, monkeypatch):
    source, target = _file(tmp_path / "s"), _file(tmp_path / "t")
    os.chmod(source, 0o640)
    os.chmod(target, 0o600)
    real_lstat = os.lstat

    def other_owner(path, **kwargs):
        info = real_lstat(path, **kwargs)
        if str(path) == str(source):  # pretend the original belongs to someone else
            return SimpleNamespace(st_mode=info.st_mode, st_uid=info.st_uid + 1, st_gid=info.st_gid + 1)
        return info

    def refused(*args, **kwargs):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(os, "lstat", other_owner)
    monkeypatch.setattr(os, "chown", refused)
    problems = hostos.copy_owner_and_mode(str(source), str(target))
    monkeypatch.undo()
    assert len(problems) == 1 and "owner and group" in problems[0] and "Operation not permitted" in problems[0]
    assert os.stat(target).st_mode & 0o777 == 0o640


@posix_only
def test_a_refused_permission_change_is_reported_not_raised(tmp_path, monkeypatch):
    source, target = _file(tmp_path / "s"), _file(tmp_path / "t")
    os.chmod(source, 0o640)
    os.chmod(target, 0o600)

    def refused(*args, **kwargs):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(os, "chmod", refused)
    problems = hostos.copy_owner_and_mode(str(source), str(target))
    assert len(problems) == 1 and "permissions" in problems[0]


@posix_only
def test_a_symlink_source_has_no_mode_worth_copying(tmp_path):
    real = _file(tmp_path / "real")
    link = tmp_path / "link"
    link.symlink_to(real)
    target = _file(tmp_path / "t")
    os.chmod(target, 0o600)
    assert hostos.copy_owner_and_mode(str(link), str(target)) == []
    assert os.stat(target).st_mode & 0o777 == 0o600


def test_files_that_cannot_be_read_are_reported_not_raised(tmp_path):
    if hostos.IS_WINDOWS:
        pytest.skip("Windows does nothing, by design")
    problems = hostos.copy_owner_and_mode(str(tmp_path / "missing"), str(_file(tmp_path / "t")))
    assert len(problems) == 1 and "could not read" in problems[0]
