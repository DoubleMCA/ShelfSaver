"""Which folders the web UI may browse and a library may live in, on Windows and on Linux.

Two modes, chosen by `uses_roots`:

- Drive mode (Windows, no media roots configured): exactly the behaviour ShelfSaver always
  had. The user picks any local drive letter; network drives and UNC paths are refused.
- Roots mode (Linux always; Windows too if media roots are configured): every folder must sit
  inside one of the configured media roots.

Why Linux insists on roots: Windows has a short, natural list of places a person could point
at (their drives). A Linux server has one tree starting at "/", and /api/browse would let any
signed-in user walk all of it: /etc, other users' homes, the app's own data folder. In Docker
the process is often root, so the blast radius is the whole container. Requiring the owner to
name the media folders up front (SHELFSAVER_MEDIA_ROOTS=/media) keeps browsing and library
creation inside what they meant to share. It also fits how Docker/Unraid users already think:
they map a media share into the container and nothing else needs to be visible.

Containment is judged on resolved paths (symlinks followed), so a link inside a root that points
outside it does not become a way out.
"""
from __future__ import annotations

import ctypes
import os
import string
from typing import Optional

from .. import hostos, swap

DRIVE_REMOTE = 4
HIDDEN_OR_SYSTEM = 0x2 | 0x4  # FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM

NOT_LOCAL = "Choose a folder on a drive in this PC. Network folders aren't supported."
if hostos.IS_WINDOWS:
    WHOLE_DRIVE = "Choose a folder on the drive, not the whole drive."
else:
    # "Volume" because on Linux the thing that matters is the mounted filesystem (or Docker
    # volume) the folder is on, not a drive letter.
    WHOLE_DRIVE = ("That folder is the top of a mounted volume. Choose a folder inside it, such "
                   "as a Movies folder: ShelfSaver keeps its working folder at the top of the "
                   "volume, and that must not be inside your library.")
IN_WORK_FOLDER = ("That folder is inside the ShelfSaver folder that holds files being replaced. "
                  "Choose a different folder.")


def _no_roots_message(setting: str) -> str:
    return (f"No folders are set up for this. Set {setting} to the folder it may use (in Docker, "
            "the folder you mapped in, such as /media or /scratch), then restart ShelfSaver.")


class FolderProblem(Exception):
    """A folder was refused. `message` is plain text meant for the person using the page."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


def uses_roots(roots: list) -> bool:
    return bool(roots) or not hostos.IS_WINDOWS


def _resolved(path: str) -> str:
    return os.path.normcase(os.path.realpath(path))


def _within(child: str, parent: str) -> bool:
    c, p = _resolved(child), _resolved(parent)
    return c == p or c.startswith(p.rstrip("\\/") + os.sep)


def _is_a_root(path: str, roots: list) -> bool:
    here = os.path.normcase(os.path.normpath(path))
    return any(here == os.path.normcase(os.path.normpath(r)) for r in roots)


def _outside_message(roots: list) -> str:
    return ("That folder isn't inside the media folders ShelfSaver may use: "
            + ", ".join(roots) + ".")


# ---- Drive mode (Windows), unchanged behaviour

def _drive_type(path: str) -> int:
    drive = os.path.splitdrive(os.path.abspath(path))[0]
    try:
        return ctypes.windll.kernel32.GetDriveTypeW(drive + "\\")
    except (AttributeError, OSError):
        return 0


def _drives() -> list[dict]:
    found = []
    for letter in string.ascii_uppercase:
        root = f"{letter}:\\"
        if os.path.isdir(root) and _drive_type(root) != DRIVE_REMOTE:
            found.append({"name": root, "path": root})
    return found


def top_level(roots: list) -> list[dict]:
    """What the folder picker shows before anything is opened: drives, or the media roots."""
    if uses_roots(roots):
        return [{"name": r, "path": r} for r in roots if os.path.isdir(r)]
    return _drives()


def is_visible_folder(entry: os.DirEntry) -> bool:
    """Whether a directory entry belongs in the picker. Links are left out so the picker can
    never be walked out of a root through one."""
    if not entry.is_dir(follow_symlinks=False):
        return False
    if entry.name.startswith("$") or entry.name.lower() == "system volume information":
        return False
    if hostos.IS_WINDOWS:
        return not (entry.stat(follow_symlinks=False).st_file_attributes & HIDDEN_OR_SYSTEM)
    return not entry.name.startswith(".")  # dot-folders are hidden by convention on Linux


# ---- Validating a folder someone typed or picked

def check_local_folder(raw: Optional[str], what: str, roots: list,
                       setting: str = "SHELFSAVER_MEDIA_ROOTS") -> str:
    """Return the cleaned path of an existing folder we may use, or raise FolderProblem.

    `roots` are the allowed top folders for this purpose and `setting` names the variable that
    configures them, so the error can tell the owner exactly what to set.
    """
    text = (raw or "").strip()
    if not text:
        raise FolderProblem(400, f"Choose a {what} folder.")
    if uses_roots(roots):
        if not roots:
            raise FolderProblem(400, _no_roots_message(setting))
        if not os.path.isabs(text):
            raise FolderProblem(
                400, f"Enter the full path of the {what} folder, such as "
                     f"{os.path.join(roots[0], 'Movies')}.")
        path = os.path.normpath(text)
        if not any(_within(path, root) for root in roots):
            raise FolderProblem(400, _outside_message(roots))
    else:
        if text.startswith(("\\\\", "//")):
            raise FolderProblem(400, NOT_LOCAL)
        if not os.path.isabs(text) or not os.path.splitdrive(text)[0]:
            raise FolderProblem(
                400, f"Enter the full path of the {what} folder, such as D:\\Movies.")
        path = os.path.normpath(text)
        if _drive_type(path) == DRIVE_REMOTE:
            raise FolderProblem(400, NOT_LOCAL)
    try:
        is_folder = os.path.isdir(swap.ext_path(path))
    except OSError:
        is_folder = False
    if not is_folder:
        raise FolderProblem(
            400, f"That {what} folder can't be found. Check the path and that the "
                 "drive is connected.")
    if swap._is_inside(os.path.abspath(path), swap.work_root(path)):
        raise FolderProblem(400, IN_WORK_FOLDER)
    return path


# ---- Browsing

def browse(path: Optional[str], roots: list) -> dict:
    """The sub-folders of `path` for the picker. Raises FolderProblem for a bad path."""
    if not path:
        return {"path": None, "parent": None, "folders": top_level(roots)}
    if uses_roots(roots):
        if not roots:
            raise FolderProblem(400, _no_roots_message("SHELFSAVER_MEDIA_ROOTS"))
        if not os.path.isabs(path):
            raise FolderProblem(400, "Enter the full path of a folder.")
        folder = os.path.normpath(path)
        if not any(_within(folder, root) for root in roots):
            raise FolderProblem(400, _outside_message(roots))
    else:
        if path.startswith(("\\\\", "//")):
            raise FolderProblem(400, NOT_LOCAL)
        if not os.path.isabs(path) or not os.path.splitdrive(path)[0]:
            raise FolderProblem(400, "Enter the full path of a folder.")
        folder = os.path.normpath(path)
    if not os.path.isdir(folder):
        raise FolderProblem(400, "That folder can't be found.")
    try:
        with os.scandir(folder) as entries:
            listing = list(entries)
    except OSError:
        raise FolderProblem(403, "ShelfSaver isn't allowed to open that folder.")
    folders = []
    for entry in listing:
        try:
            if is_visible_folder(entry):
                folders.append({"name": entry.name, "path": os.path.join(folder, entry.name)})
        except OSError:
            continue  # no permission for this one: leave it out
    folders.sort(key=lambda f: f["name"].lower())
    parent = os.path.dirname(folder.rstrip("\\/"))
    if uses_roots(roots):
        at_top = _is_a_root(folder, roots)  # going "up" from a root returns to the root list
    else:
        at_top = os.path.splitdrive(folder)[1] in ("\\", "/") or parent == folder
    return {"path": folder, "parent": None if at_top else parent, "folders": folders}
