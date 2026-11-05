"""Crash-safe replacement and holding folder. Everything stays under tmp_path."""
from __future__ import annotations

import ctypes
import dataclasses
import json
import os
from contextlib import contextmanager
from ctypes import wintypes
from pathlib import Path
from types import SimpleNamespace

import pytest

from shelfsaver import swap
from shelfsaver.swap import (
    FileInUse,
    SwapPlan,
    TargetExists,
    UnexpectedSwapState,
    clear_holding,
    execute_swap,
    ext_path,
    held_size,
    is_volume_root,
    make_plan,
    recover_swap,
    work_root,
)

OLD = b"old-original-bytes"
NEW = b"new"
ALL_FOUR_CASES = [(".mkv", False), (".mkv", True), (".mp4", False), (".mp4", True)]
REAL_VOLUME_ROOT = swap._volume_root


class SimulatedCrash(Exception):
    """Not an OSError, so nothing in swap.py can treat it as a recoverable failure."""


@pytest.fixture(autouse=True)
def fake_volume(tmp_path, monkeypatch):
    """Point every volume-root lookup under tmp_path, so no real ShelfSaver folder is used."""
    volume = tmp_path / "vol"
    volume.mkdir()
    monkeypatch.setattr(swap, "_volume_root", lambda path: str(volume))
    return volume


@pytest.fixture
def library(fake_volume):
    path = fake_volume / "Media" / "Movies"
    path.mkdir(parents=True)
    return path


def write_file(path: Path, data: bytes) -> Path:
    os.makedirs(ext_path(path.parent), exist_ok=True)
    with open(ext_path(path), "wb") as f:
        f.write(data)
    return path


def read_file(path) -> bytes:
    with open(ext_path(path), "rb") as f:
        return f.read()


def exists(path) -> bool:
    return os.path.lexists(ext_path(path))


def prepare(library, final_ext=".mkv", holding=False, name="Movie.mkv", folder="Action"):
    original = write_file(library / folder / name, OLD)
    temp = write_file(work_root(library) / "incoming" / f"run1{final_ext}", NEW)
    return make_plan(str(original), str(temp), final_ext, str(library), "Movies", holding)


def assert_finished(plan: SwapPlan) -> None:
    """The state a clean run leaves behind."""
    assert read_file(plan.final) == NEW
    assert not exists(plan.temp)
    if plan.final != plan.original:
        assert not exists(plan.original)
    if plan.holding:
        assert read_file(plan.holding) == OLD


def assert_untouched(plan: SwapPlan) -> None:
    """The state before the swap started."""
    assert read_file(plan.original) == OLD
    assert read_file(plan.temp) == NEW
    if plan.final != plan.original:
        assert not exists(plan.final)
    if plan.holding:
        assert not exists(plan.holding)


def snapshot(root: Path) -> dict:
    found = {}
    for folder, _, files in os.walk(ext_path(root)):
        for name in files:
            full = os.path.join(folder, name)
            found[full] = read_file(full)
    return found


@contextmanager
def open_exclusive(path):
    """Hold the file open with no sharing, as a program streaming it might."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    generic_read, no_sharing, open_existing = 0x80000000, 0, 3
    handle = kernel32.CreateFileW(
        ext_path(path), generic_read, no_sharing, None, open_existing, 0, None
    )
    assert handle != wintypes.HANDLE(-1).value, ctypes.WinError(ctypes.get_last_error())
    try:
        yield
    finally:
        kernel32.CloseHandle(handle)


def crash_after(monkeypatch, allowed: int) -> None:
    """Let `allowed` file moves or deletes through, then simulate the power going out."""
    remaining = [allowed]

    def wrap(real):
        def guarded(*args, **kwargs):
            if remaining[0] == 0:
                raise SimulatedCrash()
            remaining[0] -= 1
            return real(*args, **kwargs)
        return guarded

    for name in ("rename", "replace", "remove", "unlink"):
        monkeypatch.setattr(swap.os, name, wrap(getattr(os, name)))


def fail_rename_from(monkeypatch, source: str, error: OSError) -> None:
    real = os.rename

    def guarded(src, dst, **kwargs):
        if src == ext_path(source):
            raise error
        return real(src, dst, **kwargs)

    monkeypatch.setattr(swap.os, "rename", guarded)


# --- clean runs ---


@pytest.mark.parametrize("final_ext,holding", ALL_FOUR_CASES)
def test_swap_ends_with_new_file_in_place(library, final_ext, holding):
    plan = prepare(library, final_ext, holding)
    execute_swap(plan)
    assert plan.final == str(library / "Action" / f"Movie{final_ext}")
    assert_finished(plan)
    assert recover_swap(plan) == "finished"


def test_holding_keeps_exact_original_bytes(library):
    original_bytes = bytes(range(256)) * 100
    plan = prepare(library, holding=True)
    Path(plan.original).write_bytes(original_bytes)
    execute_swap(plan)
    assert Path(plan.holding).read_bytes() == original_bytes


def test_held_path_keeps_relative_folders(library, fake_volume):
    plan = prepare(library, holding=True, folder=os.path.join("Action", "1990s"))
    expected = fake_volume / "ShelfSaver" / "holding" / "Movies" / "Action" / "1990s" / "Movie.mkv"
    assert plan.holding == str(expected)
    execute_swap(plan)
    assert expected.read_bytes() == OLD


def test_second_original_with_same_name_does_not_overwrite_held_one(library):
    first = prepare(library, holding=True)
    execute_swap(first)
    Path(first.original).write_bytes(b"second-original")
    temp = write_file(work_root(library) / "incoming" / "run2.mkv", b"second-new")
    second = make_plan(first.original, str(temp), ".mkv", str(library), "Movies", True)
    assert second.holding != first.holding
    assert Path(second.holding).name == "Movie (1).mkv"
    execute_swap(second)
    assert Path(first.holding).read_bytes() == OLD
    assert Path(second.holding).read_bytes() == b"second-original"
    assert Path(second.original).read_bytes() == b"second-new"


def test_extension_differing_only_in_case_is_the_same_name(library):
    plan = prepare(library, ".mkv", False, name="Movie.MKV")
    assert plan.final == plan.original
    execute_swap(plan)
    assert read_file(plan.original) == NEW
    assert os.listdir(library / "Action") == ["Movie.MKV"]


def test_plan_survives_a_json_round_trip(library):
    plan = prepare(library, ".mp4", True)
    assert SwapPlan(**json.loads(json.dumps(dataclasses.asdict(plan)))) == plan
    assert plan.expected_size == len(NEW)


def test_no_holding_path_when_holding_is_off(library):
    assert prepare(library, holding=False).holding is None


def test_new_file_is_flushed_to_disk_before_anything_moves(library, monkeypatch):
    plan = prepare(library, ".mp4", False)
    events = []
    real_fsync, real_rename = os.fsync, os.rename
    monkeypatch.setattr(swap.os, "fsync", lambda fd: (events.append("fsync"), real_fsync(fd))[1])
    monkeypatch.setattr(
        swap.os, "rename", lambda a, b: (events.append("rename"), real_rename(a, b))[1]
    )
    execute_swap(plan)
    assert events == ["fsync", "rename"]


# --- crashes ---


@pytest.mark.parametrize("crash_after_step", [0, 1, 2])
@pytest.mark.parametrize("case", ALL_FOUR_CASES)
def test_crash_at_any_step_never_loses_both_and_recovers(
    library, monkeypatch, case, crash_after_step
):
    final_ext, holding = case
    plan = prepare(library, final_ext, holding)

    with monkeypatch.context() as patch:
        crash_after(patch, crash_after_step)
        try:
            execute_swap(plan)
        except SimulatedCrash:
            pass

    places = [plan.original, plan.temp, plan.final, plan.holding]
    on_disk = [read_file(p) for p in places if p and exists(p)]
    assert OLD in on_disk or NEW in on_disk

    outcome = recover_swap(plan)
    if crash_after_step == 0:
        assert outcome == "not_started"
        assert_untouched(plan)
    else:
        assert outcome == "finished"
        assert_finished(plan)
    assert recover_swap(plan) == outcome  # idempotent


@pytest.mark.parametrize("case", [c for c in ALL_FOUR_CASES if c != (".mkv", False)])
def test_crash_during_recovery_is_safe_to_recover_again(library, monkeypatch, case):
    final_ext, holding = case
    plan = prepare(library, final_ext, holding)
    with monkeypatch.context() as patch:
        crash_after(patch, 1)
        with pytest.raises(SimulatedCrash):
            execute_swap(plan)
    with monkeypatch.context() as patch:
        crash_after(patch, 0)
        with pytest.raises(SimulatedCrash):
            recover_swap(plan)
    assert recover_swap(plan) == "finished"
    assert_finished(plan)


@pytest.mark.parametrize("case", [c for c in ALL_FOUR_CASES if c != (".mkv", False)])
def test_retrying_execute_after_a_crash_resumes_the_swap(library, monkeypatch, case):
    final_ext, holding = case
    plan = prepare(library, final_ext, holding)
    with monkeypatch.context() as patch:
        crash_after(patch, 1)
        with pytest.raises(SimulatedCrash):
            execute_swap(plan)
    execute_swap(plan)
    assert_finished(plan)


UNEXPECTED_STATES = [
    pytest.param(".mkv", False, {}, id="same-name-everything-gone"),
    pytest.param(".mkv", False, {"temp": NEW}, id="same-name-original-vanished"),
    pytest.param(".mkv", False, {"original": OLD}, id="same-name-temp-gone-old-size"),
    pytest.param(".mkv", True, {}, id="holding-everything-gone"),
    pytest.param(".mkv", True, {"temp": NEW}, id="holding-temp-only"),
    pytest.param(".mkv", True, {"holding": OLD}, id="holding-only-held-copy"),
    pytest.param(".mkv", True, {"temp": b"short", "holding": OLD}, id="holding-temp-wrong-size"),
    pytest.param(".mkv", True, {"original": OLD, "holding": OLD}, id="holding-new-wrong-size"),
    pytest.param(".mkv", True, {"original": NEW}, id="holding-held-copy-gone"),
    pytest.param(".mp4", False, {}, id="ext-everything-gone"),
    pytest.param(".mp4", False, {"original": OLD}, id="ext-only-original"),
    pytest.param(".mp4", False, {"temp": NEW}, id="ext-only-temp"),
    pytest.param(".mp4", False, {"original": OLD, "final": b"short"}, id="ext-final-wrong-size"),
    pytest.param(".mp4", False, {"final": b"short"}, id="ext-only-final-wrong-size"),
    pytest.param(".mp4", True, {}, id="ext-holding-everything-gone"),
    pytest.param(".mp4", True, {"original": OLD, "final": b"short"}, id="ext-holding-final-short"),
    pytest.param(".mp4", True, {"final": NEW}, id="ext-holding-held-copy-gone"),
    pytest.param(
        ".mp4", True, {"original": OLD, "final": NEW, "holding": OLD}, id="ext-all-three-placed"
    ),
]


@pytest.mark.parametrize("final_ext,holding,files", UNEXPECTED_STATES)
def test_unexpected_disk_state_leaves_everything_where_it_is(
    library, fake_volume, final_ext, holding, files
):
    plan = prepare(library, final_ext, holding)
    os.remove(plan.original)
    os.remove(plan.temp)
    for role, data in files.items():
        write_file(Path(getattr(plan, role)), data)
    before = snapshot(fake_volume)

    assert recover_swap(plan) == "unexpected"
    assert snapshot(fake_volume) == before

    with pytest.raises(UnexpectedSwapState):
        execute_swap(plan)
    assert snapshot(fake_volume) == before


@pytest.mark.parametrize("holding", [False, True])
def test_recovery_will_not_touch_a_foreign_file_at_the_final_path(library, fake_volume, holding):
    plan = prepare(library, ".mp4", holding)
    write_file(Path(plan.final), b"somebody else's file")
    before = snapshot(fake_volume)
    assert recover_swap(plan) == "unexpected"
    assert snapshot(fake_volume) == before


def interrupted_after_first_step(plan, monkeypatch) -> None:
    with monkeypatch.context() as patch:
        crash_after(patch, 1)
        with pytest.raises(SimulatedCrash):
            execute_swap(plan)


def test_original_is_not_deleted_when_final_is_the_same_file(library, monkeypatch):
    plan = prepare(library, ".mp4", False)
    interrupted_after_first_step(plan, monkeypatch)
    os.remove(plan.original)
    try:
        os.link(plan.final, plan.original)
    except OSError:
        pytest.skip("this volume has no hardlinks")
    assert recover_swap(plan) == "unexpected"
    assert read_file(plan.original) == NEW
    assert read_file(plan.final) == NEW


def test_original_is_not_deleted_when_the_same_file_check_fails(library, monkeypatch):
    plan = prepare(library, ".mp4", False)
    interrupted_after_first_step(plan, monkeypatch)

    def failing_samefile(first, second):
        raise OSError(21, "device not ready")

    monkeypatch.setattr(swap.os.path, "samefile", failing_samefile)
    assert recover_swap(plan) == "unexpected"
    assert read_file(plan.original) == OLD


def foreign_final(plan) -> None:
    """Temp gone, with somebody else's file of exactly the expected size at the final path."""
    os.remove(plan.temp)
    data = b"X" * plan.expected_size
    write_file(Path(plan.final), data)


@pytest.mark.parametrize("holding", [False, True])
def test_foreign_file_of_the_same_size_never_costs_the_original(library, fake_volume, holding):
    plan = prepare(library, ".mp4", holding)
    foreign_final(plan)
    before = snapshot(fake_volume)

    assert recover_swap(plan) == "unexpected"
    assert snapshot(fake_volume) == before
    with pytest.raises(UnexpectedSwapState):
        execute_swap(plan)
    assert snapshot(fake_volume) == before
    assert read_file(plan.original) == OLD


@pytest.mark.parametrize("holding", [False, True])
def test_foreign_file_of_the_same_size_is_not_reported_finished(library, fake_volume, holding):
    plan = prepare(library, ".mp4", holding)
    foreign_final(plan)
    if holding:
        write_file(Path(plan.holding), OLD)
    os.remove(plan.original)
    assert recover_swap(plan) == "unexpected"


@pytest.mark.parametrize("holding", [False, True])
def test_same_name_original_of_matching_size_is_not_reported_finished(library, holding):
    plan = prepare(library, ".mkv", holding)
    os.remove(plan.temp)
    os.remove(plan.original)
    write_file(Path(plan.original), b"X" * plan.expected_size)
    if holding:
        write_file(Path(plan.holding), OLD)
    assert recover_swap(plan) == "unexpected"
    assert read_file(plan.original) == b"X" * plan.expected_size


def test_foreign_temp_of_the_same_size_is_refused(library, fake_volume):
    plan = prepare(library, ".mp4", False)
    os.remove(plan.temp)
    write_file(Path(plan.temp), b"X" * plan.expected_size)
    before = snapshot(fake_volume)
    with pytest.raises(UnexpectedSwapState):
        execute_swap(plan)
    assert snapshot(fake_volume) == before


def test_unavailable_file_id_means_identity_is_not_confirmed(library, monkeypatch):
    real_lstat = os.lstat

    def lstat_without_file_id(path, **kwargs):
        info = real_lstat(path, **kwargs)
        return SimpleNamespace(st_mode=info.st_mode, st_size=info.st_size, st_ino=0)

    monkeypatch.setattr(swap.os, "lstat", lstat_without_file_id)
    plan = prepare(library, ".mp4", False)
    assert plan.temp_file_id == 0
    with pytest.raises(UnexpectedSwapState):
        execute_swap(plan)  # never start a swap that could not be recovered
    assert_untouched(plan)

    os.rename(plan.temp, plan.final)
    assert recover_swap(plan) == "unexpected"
    assert read_file(plan.original) == OLD
    assert read_file(plan.final) == NEW


@pytest.mark.parametrize("final_ext,holding", ALL_FOUR_CASES)
def test_file_id_survives_the_swap(library, final_ext, holding):
    plan = prepare(library, final_ext, holding)
    assert plan.temp_file_id != 0
    execute_swap(plan)
    assert os.stat(plan.final).st_ino == plan.temp_file_id


def test_large_file_id_survives_a_json_round_trip(library):
    refs_sized_id = 2**128 - 1
    plan = dataclasses.replace(prepare(library), temp_file_id=refs_sized_id)
    loaded = SwapPlan(**json.loads(json.dumps(dataclasses.asdict(plan))))
    assert loaded.temp_file_id == refs_sized_id
    assert isinstance(loaded.temp_file_id, int)


def test_original_vanishing_mid_swap_leaves_the_new_file_in_the_library(library, monkeypatch):
    plan = prepare(library, ".mp4", False)
    monkeypatch.setattr(swap, "_flush_to_disk", lambda path: os.remove(plan.original))
    execute_swap(plan)
    assert_finished(plan)


def test_original_vanishing_before_it_can_be_held_keeps_the_new_file(library, monkeypatch):
    plan = prepare(library, ".mp4", True)
    monkeypatch.setattr(swap, "_flush_to_disk", lambda path: os.remove(plan.original))
    with pytest.raises(UnexpectedSwapState):
        execute_swap(plan)
    assert read_file(plan.final) == NEW
    assert not exists(plan.temp)


def test_temp_vanishing_after_the_original_is_held_restores_the_original(library, monkeypatch):
    plan = prepare(library, ".mkv", True)
    monkeypatch.setattr(swap, "_flush_to_disk", lambda path: os.remove(plan.temp))
    with pytest.raises(FileNotFoundError):
        execute_swap(plan)
    assert read_file(plan.original) == OLD
    assert not exists(plan.holding)


@pytest.mark.parametrize("holding,outcome", [(False, "finished"), (True, "unexpected")])
def test_original_vanishing_during_recovery(library, monkeypatch, holding, outcome):
    plan = prepare(library, ".mp4", holding)
    interrupted_after_first_step(plan, monkeypatch)
    real_state = swap._swap_state

    def state_then_vanish(checked):
        monkeypatch.setattr(swap, "_swap_state", real_state)
        found = real_state(checked)
        os.remove(checked.original)
        return found

    monkeypatch.setattr(swap, "_swap_state", state_then_vanish)
    assert recover_swap(plan) == outcome
    assert read_file(plan.final) == NEW


# --- files in use and failures part way through ---


@pytest.mark.parametrize("final_ext,holding", ALL_FOUR_CASES)
def test_open_file_raises_file_in_use_and_keeps_original(library, final_ext, holding):
    plan = prepare(library, final_ext, holding)
    with open_exclusive(plan.original):
        with pytest.raises(FileInUse):
            execute_swap(plan)
    assert_untouched(plan)

    execute_swap(plan)  # the worker retries later with the same plan
    assert_finished(plan)


@pytest.mark.parametrize(
    "error,raised", [(PermissionError(13, "in use"), FileInUse), (OSError(112, "no room"), OSError)]
)
def test_failure_moving_new_file_into_place_restores_the_original(
    library, monkeypatch, error, raised
):
    plan = prepare(library, ".mkv", True)
    with monkeypatch.context() as patch:
        fail_rename_from(patch, plan.temp, error)
        with pytest.raises(raised):
            execute_swap(plan)
    assert_untouched(plan)


@pytest.mark.parametrize("holding", [False, True])
def test_failed_undo_stays_recoverable(library, monkeypatch, holding):
    plan = prepare(library, ".mp4", holding)
    with monkeypatch.context() as patch:
        fail_rename_from(patch, plan.final, PermissionError(13, "scanner has it open"))
        with open_exclusive(plan.original):
            with pytest.raises(FileInUse):
                execute_swap(plan)
    assert read_file(plan.final) == NEW
    assert read_file(plan.original) == OLD
    assert not exists(plan.temp)

    assert recover_swap(plan) == "finished"
    assert_finished(plan)


def test_recovery_reports_file_in_use_and_can_run_again(library, monkeypatch):
    plan = prepare(library, ".mp4", False)
    with monkeypatch.context() as patch:
        crash_after(patch, 1)
        with pytest.raises(SimulatedCrash):
            execute_swap(plan)
    with open_exclusive(plan.original):
        with pytest.raises(FileInUse):
            recover_swap(plan)
    assert read_file(plan.original) == OLD
    assert read_file(plan.final) == NEW

    assert recover_swap(plan) == "finished"
    assert_finished(plan)


def test_incomplete_temp_is_refused_before_anything_moves(library, fake_volume):
    plan = prepare(library, ".mp4", True)
    Path(plan.temp).write_bytes(b"truncated")
    before = snapshot(fake_volume)
    assert recover_swap(plan) == "not_started"
    with pytest.raises(UnexpectedSwapState):
        execute_swap(plan)
    assert snapshot(fake_volume) == before


def test_extension_change_refuses_existing_different_file(library):
    plan = prepare(library, ".mp4", False)
    Path(plan.final).write_bytes(b"somebody else's file")
    with pytest.raises(TargetExists):
        execute_swap(plan)
    assert Path(plan.final).read_bytes() == b"somebody else's file"
    assert read_file(plan.original) == OLD
    assert read_file(plan.temp) == NEW


@pytest.mark.parametrize("final_ext", [".mkv", ".mp4"])
def test_occupied_holding_path_is_refused(library, final_ext):
    plan = prepare(library, final_ext, True)
    write_file(Path(plan.holding), b"already held")
    with pytest.raises(TargetExists):
        execute_swap(plan)
    assert read_file(plan.holding) == b"already held"
    assert read_file(plan.original) == OLD
    assert read_file(plan.temp) == NEW


def test_target_appearing_at_the_last_moment_is_refused(library, monkeypatch):
    plan = prepare(library, ".mp4", False)
    real_fsync = os.fsync

    def create_final_then_fsync(fd):
        write_file(Path(plan.final), b"just arrived")
        return real_fsync(fd)

    monkeypatch.setattr(swap.os, "fsync", create_final_then_fsync)
    with pytest.raises(TargetExists):
        execute_swap(plan)
    assert read_file(plan.final) == b"just arrived"
    assert read_file(plan.original) == OLD
    assert read_file(plan.temp) == NEW


# --- paths ---


@pytest.mark.parametrize("name", ["Movie [2019] 'x' 100%.mkv", "Amélie ü.mkv", "日本語.mkv"])
@pytest.mark.parametrize("holding", [False, True])
def test_unusual_names(library, name, holding):
    plan = prepare(library, ".mkv", holding, name=name)
    execute_swap(plan)
    assert Path(plan.original).name == name
    assert_finished(plan)


def long_folder(depth: int = 8) -> str:
    return os.path.join(*[f"folder-{i:02d}-" + "x" * 40 for i in range(depth)])


@pytest.mark.parametrize("final_ext,holding", ALL_FOUR_CASES)
def test_path_over_260_chars(library, final_ext, holding):
    plan = prepare(library, final_ext, holding, folder=long_folder())
    assert len(plan.original) > 260
    execute_swap(plan)
    assert_finished(plan)
    assert recover_swap(plan) == "finished"


def test_ext_path_leaves_short_paths_alone(tmp_path):
    short = str(tmp_path / "a.mkv")
    assert ext_path(short) == short


def test_ext_path_accepts_path_objects(tmp_path):
    assert ext_path(tmp_path / "a.mkv") == str(tmp_path / "a.mkv")


def test_ext_path_prefixes_paths_over_240_chars():
    long_path = "C:\\" + "a" * 240 + "\\b.mkv"
    assert ext_path(long_path) == "\\\\?\\" + long_path


def test_ext_path_threshold_is_240_chars():
    exactly_240 = "C:\\" + "a" * 237
    assert ext_path(exactly_240) == exactly_240
    assert ext_path(exactly_240 + "a") == "\\\\?\\" + exactly_240 + "a"


def test_ext_path_normalizes_forward_slashes_when_prefixing():
    long_path = "C:/" + "a" * 240 + "/b.mkv"
    assert ext_path(long_path) == "\\\\?\\C:\\" + "a" * 240 + "\\b.mkv"


def test_ext_path_is_idempotent():
    long_path = "C:\\" + "a" * 240 + "\\b.mkv"
    assert ext_path(ext_path(long_path)) == ext_path(long_path)


def test_ext_path_handles_network_style_paths():
    long_path = "\\\\server\\share\\" + "a" * 240 + "\\b.mkv"
    assert ext_path(long_path) == "\\\\?\\UNC\\server\\share\\" + "a" * 240 + "\\b.mkv"


# --- work root ---


def test_work_root_is_shelfsaver_at_the_volume_root(library, fake_volume):
    assert work_root(library) == fake_volume / "ShelfSaver"


def test_volume_root_lookup_only_reads_the_drive_letter():
    assert REAL_VOLUME_ROOT("C:\\Some\\Folder\\That\\Need\\Not\\Exist").upper() == "C:\\"


def test_volume_root_of_a_missing_drive_stays_on_that_drive():
    letters = [c for c in "ZYXWVUTSRQPONMLKJIHGFED" if not os.path.exists(f"{c}:\\")]
    if not letters:
        pytest.skip("every drive letter is in use")
    assert REAL_VOLUME_ROOT(f"{letters[0]}:\\Movies").upper() == f"{letters[0]}:\\"


def test_offline_library_counts_and_clears_nothing(tmp_path, monkeypatch):
    missing_volume = tmp_path / "not-connected"
    monkeypatch.setattr(swap, "_volume_root", lambda path: str(missing_volume))
    offline = missing_volume / "Movies"
    assert held_size([offline]) == 0
    assert clear_holding([offline]) == 0
    assert not missing_volume.exists()


def test_is_volume_root(library, fake_volume):
    assert is_volume_root(fake_volume)
    assert is_volume_root(str(fake_volume) + os.sep)
    assert not is_volume_root(library)


def test_is_volume_root_with_the_real_lookup(monkeypatch):
    monkeypatch.setattr(swap, "_volume_root", REAL_VOLUME_ROOT)
    assert is_volume_root("C:\\")
    assert not is_volume_root("C:\\Some\\Folder\\That\\Need\\Not\\Exist")


@pytest.mark.parametrize("holding", [False, True])
def test_library_at_the_volume_root_is_refused(fake_volume, holding):
    original = write_file(fake_volume / "Movie.mkv", OLD)
    temp = write_file(fake_volume / "ShelfSaver" / "incoming" / "run.mkv", NEW)
    with pytest.raises(ValueError):
        make_plan(str(original), str(temp), ".mkv", str(fake_volume), "Movies", holding)


@pytest.mark.parametrize("holding", [False, True])
def test_original_inside_the_work_folder_is_refused(fake_volume, holding):
    held_library = fake_volume / "ShelfSaver" / "holding" / "Movies"
    original = write_file(held_library / "Movie.mkv", OLD)
    temp = write_file(fake_volume / "ShelfSaver" / "incoming" / "run.mkv", NEW)
    with pytest.raises(ValueError):
        make_plan(str(original), str(temp), ".mkv", str(held_library), "Movies", holding)


def test_folder_merely_named_like_the_work_folder_is_allowed(fake_volume):
    library = fake_volume / "ShelfSaver2"
    original = write_file(library / "Movie.mkv", OLD)
    temp = write_file(fake_volume / "ShelfSaver" / "incoming" / "run.mkv", NEW)
    assert make_plan(str(original), str(temp), ".mkv", str(library), "Movies", False)


def test_holding_folder_that_cannot_be_checked_is_refused(library, monkeypatch):
    held = hold(library, "a.mkv", b"12345")
    real_lstat = os.lstat

    def lstat(path, **kwargs):
        if str(path).endswith("holding"):
            raise OSError(21, "device not ready")
        return real_lstat(path, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(swap.os, "lstat", lstat)
        assert clear_holding([str(library)]) == 0
    assert held.read_bytes() == b"12345"


@pytest.mark.parametrize(
    "library_name,folder",
    [
        ("..", "_"),
        ("..\\..\\Windows", ".._.._Windows"),
        ("TV: Kids", "TV_ Kids"),
        ("NUL", "_NUL"),
        ("Movies. ", "Movies"),
    ],
)
def test_library_name_cannot_escape_or_break_the_holding_folder(
    library, fake_volume, library_name, folder
):
    original = write_file(library / "Movie.mkv", OLD)
    temp = write_file(fake_volume / "ShelfSaver" / "incoming" / "run.mkv", NEW)
    plan = make_plan(str(original), str(temp), ".mkv", str(library), library_name, True)
    assert plan.holding == str(fake_volume / "ShelfSaver" / "holding" / folder / "Movie.mkv")


def test_original_outside_the_library_is_refused(library, fake_volume):
    original = write_file(fake_volume / "Elsewhere" / "Movie.mkv", OLD)
    temp = write_file(fake_volume / "ShelfSaver" / "incoming" / "run.mkv", NEW)
    with pytest.raises(ValueError):
        make_plan(str(original), str(temp), ".mkv", str(library), "Movies", True)


# --- held_size and clear_holding ---


def hold(library, relative: str, data: bytes) -> Path:
    return write_file(work_root(library) / "holding" / "Movies" / relative, data)


def make_junction(link: Path, target: Path) -> None:
    import _winapi

    _winapi.CreateJunction(str(target), str(link))


def test_held_size_adds_up_everything_held(library):
    hold(library, "a.mkv", b"12345")
    hold(library, os.path.join("sub", "b.mkv"), b"123")
    assert held_size([str(library)]) == 8


def test_held_size_is_zero_with_no_holding_folder(library):
    assert held_size([str(library)]) == 0


def test_libraries_sharing_a_volume_are_counted_and_cleared_once(library, fake_volume):
    other = fake_volume / "Media" / "TV"
    other.mkdir()
    hold(library, "a.mkv", b"12345")
    assert held_size([str(library), str(other)]) == 5
    assert clear_holding([str(library), str(other)]) == 5


def test_clear_holding_only_deletes_inside_holding(library):
    in_library = write_file(library / "Movie.mkv", OLD)
    incoming = write_file(work_root(library) / "incoming" / "run.mkv", NEW)
    hold(library, "a.mkv", b"12345")
    hold(library, os.path.join("sub", "deep", "b.mkv"), b"123")

    assert clear_holding([str(library)]) == 8

    assert in_library.read_bytes() == OLD
    assert incoming.read_bytes() == NEW
    holding = work_root(library) / "holding"
    assert holding.is_dir()
    assert os.listdir(holding) == []
    assert held_size([str(library)]) == 0


def test_clear_holding_handles_paths_over_260_chars(library):
    held = hold(library, os.path.join(long_folder(), "a.mkv"), b"12345")
    assert len(str(held)) > 260
    assert held_size([str(library)]) == 5
    assert clear_holding([str(library)]) == 5
    assert os.listdir(work_root(library) / "holding") == []


def test_clear_holding_does_not_follow_a_junction_out_of_holding(library, fake_volume):
    precious = write_file(fake_volume / "Precious" / "keep.mkv", OLD)
    hold(library, "a.mkv", b"12345")
    make_junction(work_root(library) / "holding" / "Movies" / "escape", precious.parent)

    assert held_size([str(library)]) == 5
    assert clear_holding([str(library)]) == 5

    assert precious.read_bytes() == OLD
    assert not exists(work_root(library) / "holding" / "Movies" / "escape")


def test_clear_holding_does_not_follow_a_symlink_out_of_holding(library, fake_volume):
    precious = write_file(fake_volume / "Precious" / "keep.mkv", OLD)
    held_dir = hold(library, "a.mkv", b"12345").parent
    try:
        os.symlink(precious, held_dir / "file-link.mkv")
        os.symlink(precious.parent, held_dir / "dir-link", target_is_directory=True)
    except OSError:
        pytest.skip("creating symlinks needs Developer Mode on this machine")

    assert held_size([str(library)]) == 5
    assert clear_holding([str(library)]) == 5

    assert precious.read_bytes() == OLD


@pytest.mark.parametrize("linked", ["holding", "work_root"])
def test_clear_holding_refuses_when_the_folder_itself_is_a_junction(library, fake_volume, linked):
    precious = write_file(fake_volume / "Precious" / "holding" / "keep.mkv", OLD)
    work = work_root(library)
    if linked == "holding":
        work.mkdir()
        make_junction(work / "holding", precious.parent)
    else:
        make_junction(work, precious.parent.parent)

    assert held_size([str(library)]) == 0
    assert clear_holding([str(library)]) == 0

    assert precious.read_bytes() == OLD


def test_clear_holding_skips_a_file_it_cannot_delete_and_counts_only_what_it_freed(library):
    stuck = hold(library, os.path.join("sub", "stuck.mkv"), b"123")
    hold(library, "a.mkv", b"12345")
    with open_exclusive(stuck):
        assert clear_holding([str(library)]) == 5
    assert stuck.read_bytes() == b"123"
    assert held_size([str(library)]) == 3
