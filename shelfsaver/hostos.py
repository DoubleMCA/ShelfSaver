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

import os
from typing import BinaryIO

IS_WINDOWS = os.name == "nt"

if IS_WINDOWS:
    import msvcrt
else:
    import fcntl


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
