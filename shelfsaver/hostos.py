"""The few places where ShelfSaver has to behave differently on Windows and on Linux.

Why this module exists: the app was written for Windows and called Windows-only APIs
(`msvcrt`, `ctypes.windll`, NTFS file attributes) directly from many files. On Linux, merely
importing some of those files fails. Everything OS-specific now goes through this one module,
so a reviewer can see the whole Windows/Linux split in one place, and the rest of the code
stays free of `if os.name == "nt"` checks.

Rules for adding to this module:
- A Windows-only import must sit inside the `IS_WINDOWS` branch, never at the top level,
  or Linux fails at import time.
- Each function keeps the Windows behaviour exactly as it was before this module existed.
  The POSIX branch is new.

The module is named `hostos`, not `platform`, so it can never be confused with (or shadow)
Python's built-in `platform` module.
"""
from __future__ import annotations

import ctypes
import errno
import os
import re
import stat
from pathlib import Path
from typing import BinaryIO, Optional

IS_WINDOWS = os.name == "nt"

if IS_WINDOWS:
    import msvcrt
    from ctypes import wintypes
else:
    import fcntl

# Linux's renameat2(2) flag: fail with EEXIST instead of replacing an existing target.
_RENAME_NOREPLACE = 1
_AT_FDCWD = -100
# What a filesystem answers when it does not support RENAME_NOREPLACE (some network and FUSE
# filesystems), as opposed to a real failure. Only these fall back to check-then-rename.
_NOREPLACE_UNSUPPORTED = {errno.EINVAL, errno.ENOSYS, errno.ENOTSUP}

_renameat2 = None
if not IS_WINDOWS:
    try:
        _libc = ctypes.CDLL(None, use_errno=True)
        _renameat2 = _libc.renameat2  # glibc 2.28+ / musl 1.2.2+; AttributeError if missing
        _renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p,
                               ctypes.c_uint]
        _renameat2.restype = ctypes.c_int
    except (OSError, AttributeError):
        _renameat2 = None  # rename_no_replace falls back to check-then-rename


def try_lock_file(handle: BinaryIO) -> bool:
    """Take an exclusive, non-blocking lock on an open file. True if we got it.

    The lock is held for as long as `handle` stays open and is dropped by the OS when the
    process dies, even in a crash. That is what lets ShelfSaver refuse to run twice without a
    stale lock file ever blocking the next start.

    - Windows: `msvcrt.locking` on the first byte (unchanged from before).
    - Linux: `flock`. It is tied to the open file, so it is released on close or exit. We use
      `flock` rather than `lockf`/`fcntl.F_SETLK` because POSIX record locks are dropped when
      *any* descriptor for the same file is closed in this process, which would silently
      release our lock if some other code ever opened the lock file. `flock` has no such trap.
      One limit: `flock` is not reliable on network filesystems, so the data folder (which
      holds this lock file and the database) should be on local storage.
    """
    try:
        if IS_WINDOWS:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        # Windows raises OSError when another process holds the lock; flock raises
        # BlockingIOError (an OSError subclass) for the same reason.
        return False
    return True


def volume_root(path) -> str:
    """The root of the volume (filesystem mount) that holds `path`.

    ShelfSaver keeps its `incoming/` and `holding/` folders at the root of the library's
    volume so every swap step is a rename on one filesystem, which is atomic. A rename
    across filesystems would be a copy followed by a delete and could be cut off halfway.
    `path` need not exist.

    - Windows: `GetVolumePathNameW`, which also understands folder-mounted volumes
      (unchanged from before this module existed).
    - Linux: the longest mount point in `/proc/self/mountinfo` that contains the path. This
      sees bind mounts, which is what a Docker volume is: `os.path.ismount` misses a bind
      mount of a folder from the same filesystem, so it would report a parent mount and put
      the work folder outside the volume the user mapped. The path is resolved through
      symlinks first, because the swap must happen on the filesystem the file really lives on.
    """
    if IS_WINDOWS:
        return _windows_volume_root(path)
    return _posix_volume_root(path)


def _windows_volume_root(path) -> str:
    full = os.path.abspath(path)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetVolumePathNameW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    kernel32.GetVolumePathNameW.restype = wintypes.BOOL
    buffer = ctypes.create_unicode_buffer(1024)
    found = kernel32.GetVolumePathNameW(full, buffer, len(buffer))
    drive = os.path.splitdrive(full)[0]
    # For a drive that is not connected, Windows fails or can answer with the current drive.
    if not found or os.path.normcase(os.path.splitdrive(buffer.value)[0]) != os.path.normcase(drive):
        return drive + "\\"
    return buffer.value


def _mount_points() -> list[str]:
    """Every mount point this process can see, or [] where /proc is unavailable."""
    try:
        text = Path("/proc/self/mountinfo").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    points = []
    for line in text.splitlines():
        fields = line.split(" ")
        if len(fields) > 4:
            points.append(_unescape_mountinfo(fields[4]))
    return points


def _unescape_mountinfo(text: str) -> str:
    """Undo the kernel's octal escaping of spaces, tabs, newlines and backslashes (\\040)."""
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), text)


def _posix_volume_root(path) -> str:
    full = os.path.realpath(path)
    best = ""
    for point in _mount_points():
        inside = full == point or full.startswith(point.rstrip("/") + "/")
        if inside and len(point) > len(best):
            best = point
    if best:
        return best
    # No /proc/self/mountinfo (not Linux): walk up to the nearest mount point instead.
    current = full
    while not os.path.ismount(current) and os.path.dirname(current) != current:
        current = os.path.dirname(current)
    return current


def is_link_like(info: os.stat_result) -> bool:
    """True if an `lstat` result is a link that could point somewhere else.

    The holding-folder code must never follow one out of the holding folder, because
    clearing it would then delete files elsewhere. Windows folders can be junctions or other
    reparse points, which are not always symlinks, so the reparse-point attribute is the test
    there. Linux has only symlinks (a bind mount is not a link and is handled by volume_root).
    """
    if IS_WINDOWS:
        return bool(info.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
    return stat.S_ISLNK(info.st_mode)


def copy_owner_and_mode(source: str, target: str) -> list:
    """Give `target` the owner, group and permission bits of `source`. Returns a list of plain-text
    problems; empty means everything that needed doing was done. It never raises.

    Why: the converted file is created by ShelfSaver's own process, so left alone it belongs to
    whoever ShelfSaver runs as (root, in a typical container). Other programs that manage the same
    library (and people using the share over the network) expect the original's owner, so the
    new file must match, or they may be unable to rename, replace or delete it.

    Rules, each for a reason:
    - Windows does nothing. Files there take their access rules from the folder they are in.
    - Only the nine ordinary permission bits are copied. setuid, setgid and sticky are never
      carried onto a media file, even if the original somehow had them.
    - chown is attempted only when the owner or group actually differs, so a correctly configured
      setup (ShelfSaver already running as the right user) makes no call and no noise.
    - Changing an owner needs root (or the right group). When it is refused, that is reported as
      a problem and the caller carries on: a finished conversion is worth more than its ownership.
    - A source that is not a regular file (a symlink, say) has no meaningful mode to copy.
    """
    if IS_WINDOWS:
        return []
    problems = []
    try:
        wanted = os.lstat(source)
        have = os.lstat(target)
    except OSError as exc:
        return [f"could not read the file details ({exc.strerror or exc})"]
    if not (stat.S_ISREG(wanted.st_mode) and stat.S_ISREG(have.st_mode)):
        return []
    if (wanted.st_uid, wanted.st_gid) != (have.st_uid, have.st_gid):
        try:
            os.chown(target, wanted.st_uid, wanted.st_gid, follow_symlinks=False)
        except OSError as exc:
            problems.append(f"owner and group {wanted.st_uid}:{wanted.st_gid} were refused ({exc.strerror or exc})")
    wanted_bits = stat.S_IMODE(wanted.st_mode) & 0o777
    try:
        # Read the mode again: a successful chown can clear bits on its own.
        if stat.S_IMODE(os.lstat(target).st_mode) & 0o777 != wanted_bits:
            os.chmod(target, wanted_bits)
    except OSError as exc:
        problems.append(f"permissions {wanted_bits:o} were refused ({exc.strerror or exc})")
    return problems


def make_dirs_like(path: str, model: str) -> list:
    """Like os.makedirs(path, exist_ok=True), but each folder this call creates is given the owner,
    group and permission bits of the existing folder `model`. Returns the problems met; folder
    creation itself raises exactly as os.makedirs would.

    Why: ShelfSaver's work folders (`ShelfSaver/incoming`, `ShelfSaver/holding/...`) sit inside the
    library's drive, usually a share that other programs and people use. Created plainly they
    belong to whoever ShelfSaver runs as (root, in a typical container), so the owner of the
    library could be unable to look inside them, tidy them or delete them. Modelling them on a
    library folder keeps them manageable. The trust decision is deliberate: whoever may change the
    model folder already controls the library files themselves, so matching it hands them nothing
    they did not have.

    Rules, each for a reason:
    - Only folders created by this call are changed. A folder that already existed (the library
      itself, or a work folder made by an older version) belongs to somebody else's decision.
    - If another process creates a folder between our check and our mkdir, that folder is left
      alone too: it is not ours.
    - The folder is created with the model's permission bits from the start (the umask can only
      narrow them), so it is never, even for an instant, more open than its model, except for
      one deliberate addition: the owner always keeps read, write and enter (`| 0o700`). Some
      people make a library's top folder read-only (0555) so it cannot be deleted by mistake.
      Copying that exactly would make ShelfSaver's own `ShelfSaver` folder read-only to ShelfSaver,
      and it could not create `incoming` inside it. The others' bits are still copied as they are.
    - The owner and mode are then applied through an open handle to the folder, opened without
      following links. A path could be swapped for a link to somewhere else between our mkdir and
      a chown/chmod; a handle cannot.
    - Windows takes access rules from the parent folder, so it simply creates the folders.
    """
    if IS_WINDOWS:
        os.makedirs(path, exist_ok=True)
        return []
    problems = []
    try:
        wanted = os.lstat(model)
        if not stat.S_ISDIR(wanted.st_mode):
            raise OSError("not a folder")
    except OSError as exc:
        wanted = None
        problems.append(f"could not read the folder to copy from ({exc.strerror or exc})")
    missing = []
    current = os.path.normpath(path)
    while not os.path.isdir(current):
        missing.append(current)
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    for folder in reversed(missing):
        try:
            if wanted is None:
                os.mkdir(folder)
            else:
                os.mkdir(folder, _folder_bits(wanted))
        except FileExistsError:
            if os.path.isdir(folder):
                continue  # created by someone else just now: not ours to change
            raise  # a file (or a dangling link) is in the way, as os.makedirs would say
        if wanted is not None:
            problems += _match_new_folder(wanted, folder)
    return problems


def _folder_bits(wanted) -> int:
    """The nine permission bits for a folder we create: the model's, plus full access for the
    owner (the account ShelfSaver runs as, once ownership has been matched or kept)."""
    return (stat.S_IMODE(wanted.st_mode) & 0o777) | 0o700


def _match_new_folder(wanted, folder: str) -> list:
    """Apply `wanted`'s owner, group and nine permission bits to the just-created `folder`, using
    a handle that refuses to follow a link. Returns plain-text problems; never raises."""
    try:
        fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        return [f"could not open the new folder safely ({exc.strerror or exc})"]
    problems = []
    try:
        have = os.fstat(fd)
        if (wanted.st_uid, wanted.st_gid) != (have.st_uid, have.st_gid):
            try:
                os.fchown(fd, wanted.st_uid, wanted.st_gid)
            except OSError as exc:
                problems.append(f"owner and group {wanted.st_uid}:{wanted.st_gid} were refused ({exc.strerror or exc})")
        wanted_bits = _folder_bits(wanted)
        try:
            # Read the mode again: a successful chown can clear bits on its own.
            if stat.S_IMODE(os.fstat(fd).st_mode) & 0o777 != wanted_bits:
                os.fchmod(fd, wanted_bits)
        except OSError as exc:
            problems.append(f"permissions {wanted_bits:o} were refused ({exc.strerror or exc})")
    finally:
        os.close(fd)
    return problems


def rename_no_replace(source: str, target: str) -> None:
    """Rename, but raise FileExistsError instead of ever replacing an existing `target`.

    The swap promises that it never overwrites a file it did not create. Windows' own rename
    already refuses to replace. Linux's rename silently replaces, which would break that
    promise if another program (an importer, a scanner) creates the target in the instant
    after we looked. So on Linux:
    1. Prefer renameat2(RENAME_NOREPLACE): the kernel refuses atomically, with no race.
    2. If the filesystem does not support that flag, check that the target is absent and then
       rename. That leaves a window of microseconds in which a file could appear, which is
       the best that filesystem allows, and is no worse than not checking at all.
    """
    if IS_WINDOWS:
        os.rename(source, target)
        return
    if _renameat2 is not None:
        done = _renameat2(_AT_FDCWD, os.fsencode(source), _AT_FDCWD, os.fsencode(target),
                          _RENAME_NOREPLACE)
        if done == 0:
            return
        code = ctypes.get_errno()
        if code not in _NOREPLACE_UNSUPPORTED:
            # OSError(errno, ...) turns itself into FileExistsError, FileNotFoundError, etc.
            raise OSError(code, os.strerror(code), source, None, target)
    _rename_after_check(source, target)


def _rename_after_check(source: str, target: str) -> None:
    """The fallback for filesystems without RENAME_NOREPLACE. See rename_no_replace."""
    try:
        os.lstat(target)
    except FileNotFoundError:
        pass  # target is free
    else:
        raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), target)
    # Any other OSError from lstat propagates: a disk that cannot answer is not "free".
    os.rename(source, target)
