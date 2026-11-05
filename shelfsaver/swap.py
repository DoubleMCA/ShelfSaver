"""Crash-safe replacement of an original with its converted file, and the holding folder.

Each step is one rename or delete on one volume, in an order that always leaves
at least one whole copy on disk:

    Case                     Step 1                  Step 2
    Same name, no holding    replace temp->original  (none)
    Same name, holding       move original->holding  move temp->original
    New extension, no hold   move temp->final        delete original
    New extension, holding   move temp->final        move original->holding

Progress is never recorded anywhere but the disk: `_swap_state` works out from
which files exist how far a plan got. Anything it does not recognize is left
exactly where it is.
"""
from __future__ import annotations

import ctypes
import logging
import os
import re
import stat
from contextlib import contextmanager
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Literal, Optional

logger = logging.getLogger(__name__)

_EXT_PREFIX = "\\\\?\\"
_EXT_THRESHOLD = 240

_NOT_STARTED = "not_started"
_MIDWAY = "midway"
_FINISHED = "finished"
_UNEXPECTED = "unexpected"

_FILE, _FOLDER, _LINK = "file", "folder", "link"

_BAD_NAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


class FileInUse(Exception):
    """Another program has the file open. Retrying later with the same plan is safe."""


class TargetExists(Exception):
    """A move would land on a different file that is already there. Nothing was overwritten."""


class UnexpectedSwapState(Exception):
    """The files on disk match no step of the plan, so they were left alone."""


@dataclass(frozen=True)
class SwapPlan:
    """Plain strings and numbers, so `dataclasses.asdict` output is JSON-serializable."""

    original: str
    temp: str
    final: str
    holding: Optional[str]
    expected_size: int


def ext_path(path) -> str:
    """Return the `\\\\?\\` form for paths over 240 characters, otherwise the path as given."""
    text = os.fspath(path)
    if text.startswith(_EXT_PREFIX):
        return text
    full = os.path.abspath(text)
    if len(full) <= _EXT_THRESHOLD:
        return text
    if full.startswith("\\\\"):
        return f"{_EXT_PREFIX}UNC\\{full[2:]}"
    return _EXT_PREFIX + full


def _volume_root(path) -> str:
    """Return the root of the volume holding `path`, including folder-mounted volumes."""
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


def work_root(library_path) -> Path:
    """Return `<volume root>\\ShelfSaver`, which holds `incoming\\` and `holding\\`."""
    return Path(_volume_root(library_path)) / "ShelfSaver"


def _safe_folder_name(library_name: str) -> str:
    name = _BAD_NAME_CHARS.sub("_", library_name).rstrip(". ")
    if not name or name.split(".")[0].upper() in _RESERVED_NAMES:
        name = f"_{name}"
    return name


def _present(path: str) -> bool:
    return os.path.lexists(ext_path(path))


def _free_holding_path(wanted: str) -> str:
    stem, extension = os.path.splitext(wanted)
    candidate = wanted
    number = 0
    while _present(candidate):
        number += 1
        candidate = f"{stem} ({number}){extension}"
    return candidate


def make_plan(
    original, temp, final_ext: str, library_path, library_name: str, holding_enabled: bool
) -> SwapPlan:
    """Plan a swap. An extension differing only in letter case keeps the original's name."""
    original = os.path.abspath(original)
    temp = os.path.abspath(temp)
    final = os.path.splitext(original)[0] + final_ext
    if os.path.normcase(final) == os.path.normcase(original):
        final = original

    holding = None
    if holding_enabled:
        relative = os.path.relpath(original, library_path)
        if relative == os.curdir or relative.split(os.sep)[0] == os.pardir:
            raise ValueError(f"{original} is not inside the library {library_path}")
        holding = _free_holding_path(
            os.path.join(
                work_root(library_path), "holding", _safe_folder_name(library_name), relative
            )
        )
    return SwapPlan(original, temp, final, holding, os.stat(ext_path(temp)).st_size)


def _keeps_name(plan: SwapPlan) -> bool:
    return os.path.normcase(plan.final) == os.path.normcase(plan.original)


def _has_size(path: str, size: int) -> bool:
    try:
        info = os.lstat(ext_path(path))
    except OSError:
        return False
    return stat.S_ISREG(info.st_mode) and info.st_size == size


def _same_file(first: str, second: str) -> bool:
    try:
        return os.path.samefile(ext_path(first), ext_path(second))
    except OSError:
        return False


def _swap_state(plan: SwapPlan) -> str:
    """Work out from the disk how far a plan got. Only a recognized state may be acted on."""
    temp = _present(plan.temp)
    original = _present(plan.original)
    held = plan.holding is not None and _present(plan.holding)
    size = plan.expected_size

    if _keeps_name(plan):
        if temp and original and not held:
            return _NOT_STARTED
        if plan.holding and not original and held and _has_size(plan.temp, size):
            return _MIDWAY
        if not temp and _has_size(plan.original, size) and held == bool(plan.holding):
            return _FINISHED
        return _UNEXPECTED

    final = _present(plan.final)
    if temp and original and not final and not held:
        return _NOT_STARTED
    if not temp and _has_size(plan.final, size):
        if original and not held and not _same_file(plan.original, plan.final):
            return _MIDWAY
        if not original and held == bool(plan.holding):
            return _FINISHED
    return _UNEXPECTED


def _target_taken(plan: SwapPlan) -> bool:
    """True when the swap has not begun, but a file sits where it would need to move one."""
    if not (_present(plan.temp) and _present(plan.original)):
        return False
    if not _keeps_name(plan) and _present(plan.final):
        return True
    return plan.holding is not None and _present(plan.holding)


def _rename(source: str, target: str) -> None:
    os.rename(ext_path(source), ext_path(target))


def _move_to_holding(plan: SwapPlan) -> None:
    os.makedirs(ext_path(os.path.dirname(plan.holding)), exist_ok=True)
    _rename(plan.original, plan.holding)


def _first_step(plan: SwapPlan) -> None:
    if not _keeps_name(plan):
        _rename(plan.temp, plan.final)
    elif plan.holding:
        _move_to_holding(plan)
    else:
        os.replace(ext_path(plan.temp), ext_path(plan.original))


def _last_step(plan: SwapPlan) -> None:
    if _keeps_name(plan):
        if plan.holding:
            _rename(plan.temp, plan.original)
    elif plan.holding:
        _move_to_holding(plan)
    else:
        os.remove(ext_path(plan.original))


def _undo_first_step(plan: SwapPlan) -> None:
    """Put things back so a retry starts clean. A failure here is still a recoverable state."""
    try:
        if not _keeps_name(plan):
            _rename(plan.final, plan.temp)
        elif plan.holding:
            _rename(plan.holding, plan.original)
    except OSError as exc:
        logger.warning("Could not undo the first swap step for %s: %s", plan.original, exc)


def _flush_to_disk(path: str) -> None:
    """Commit the new file's data, so a power cut after the swap cannot cost it."""
    with open(ext_path(path), "r+b") as f:
        os.fsync(f.fileno())


@contextmanager
def _swap_errors() -> Iterator[None]:
    try:
        yield
    except PermissionError as exc:
        raise FileInUse(str(exc)) from exc
    except FileExistsError as exc:
        raise TargetExists(str(exc)) from exc


def execute_swap(plan: SwapPlan) -> None:
    """Carry out the plan, or resume one a crash or earlier failure interrupted.

    After `FileInUse` the plan can simply be run again. It raises `TargetExists` if a move
    would land on a different file, and `UnexpectedSwapState` if the disk matches no step.
    """
    state = _swap_state(plan)
    if state == _FINISHED:
        return
    if state == _UNEXPECTED:
        if _target_taken(plan):
            raise TargetExists(f"A different file is in the way of the swap for {plan.original}")
        raise UnexpectedSwapState(f"Files on disk match no swap step for {plan.original}")
    if state == _NOT_STARTED and not _has_size(plan.temp, plan.expected_size):
        raise UnexpectedSwapState(f"The new file for {plan.original} is not the expected size")

    with _swap_errors():
        if state == _NOT_STARTED:
            _flush_to_disk(plan.temp)
            _first_step(plan)
        try:
            _last_step(plan)
        except OSError:
            _undo_first_step(plan)
            raise


def recover_swap(plan: SwapPlan) -> Literal["finished", "not_started", "unexpected"]:
    """Finish an interrupted swap, judged only from what is on disk. Safe to run repeatedly.

    - "finished": the new file is in place, either already or as a result of this call.
    - "not_started": the original is untouched and the temp file is still there.
    - "unexpected": the disk matches no step, so nothing was touched.

    Raises `FileInUse` if the remaining step is blocked; nothing changes, so run it again later.
    """
    state = _swap_state(plan)
    if state == _MIDWAY:
        with _swap_errors():
            _last_step(plan)
        return _FINISHED
    if state == _UNEXPECTED:
        logger.warning("Files on disk match no swap step for %s; left as is", plan.original)
    return state


def _may_be_a_link(path: Path) -> bool:
    """True for a reparse point, and for anything that cannot be checked."""
    try:
        attributes = os.lstat(ext_path(path)).st_file_attributes
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _holding_roots(library_paths: Iterable) -> list[Path]:
    """One holding folder per volume, leaving out any that really points somewhere else."""
    roots = {}
    for library_path in library_paths:
        work = work_root(library_path)
        roots[os.path.normcase(str(work))] = work
    safe = []
    for work in roots.values():
        holding = work / "holding"
        if _may_be_a_link(work) or _may_be_a_link(holding):
            logger.warning("Refusing to use %s because it may link somewhere else", holding)
        else:
            safe.append(holding)
    return safe


def _held_items(folder) -> Iterator[tuple[str, str, int]]:
    """Yield (path, kind, size) with folders after their contents. Never enters a link."""
    try:
        with os.scandir(ext_path(folder)) as entries:
            found = list(entries)
    except FileNotFoundError:
        return
    except OSError as exc:
        logger.warning("Could not read %s: %s", folder, exc)
        return
    for entry in found:
        info = entry.stat(follow_symlinks=False)
        if stat.S_ISREG(info.st_mode):
            # Includes files with other reparse tags, such as deduplicated ones. Deleting one
            # only ever removes that file.
            yield entry.path, _FILE, info.st_size
        elif info.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            yield entry.path, _LINK, 0
        else:
            yield from _held_items(entry.path)
            yield entry.path, _FOLDER, 0


def held_size(library_paths: Iterable) -> int:
    """Total bytes held on the volumes of these libraries."""
    return sum(
        size for root in _holding_roots(library_paths) for _, _, size in _held_items(root)
    )


def clear_holding(library_paths: Iterable) -> int:
    """Delete everything inside `holding\\` on these volumes and return the bytes freed.

    A link inside is removed without touching what it points to. A file that cannot be
    deleted is logged and stays held, and is not counted as freed.
    """
    freed = 0
    for root in _holding_roots(library_paths):
        for path, kind, size in _held_items(root):
            try:
                if kind == _FOLDER:
                    os.rmdir(ext_path(path))
                else:
                    os.unlink(ext_path(path))
                    freed += size
            except OSError as exc:
                logger.warning("Could not delete %s from the holding folder: %s", path, exc)
    return freed
