"""Library scanning and watching. Every file lives under tmp_path."""
from __future__ import annotations

import os
import shutil
import threading
import time

import pytest

from shelfsaver import swap
from shelfsaver.config import Config
from shelfsaver.db import Database
from shelfsaver.scanner import Scanner
from tests.helpers import make_video

SIZE = "640x360"
UNREADABLE = "Couldn't read this file"


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class Env:
    def __init__(self, tmp_path, monkeypatch, stable=0.0):
        self.tmp_path = tmp_path
        self.volume = tmp_path / "vol"
        self.root = self.volume / "Movies"
        self.root.mkdir(parents=True)
        monkeypatch.setattr(swap, "_volume_root", lambda path: str(self.volume))
        self.db = Database(tmp_path / "data" / "test.db")
        self.lib = self.db.add_library("Movies", str(self.root), "high", False)
        self.clock = Clock()
        self.scanner = Scanner(self.db, Config(data_dir=tmp_path / "data"),
                               stable_seconds=stable, clock=self.clock)
        self.f = None

    def bloated(self, rel="big.mkv"):
        return make_video(self.root / rel, seconds=2, size=SIZE, crf=1)

    def efficient(self, rel="small.mkv"):
        return make_video(self.root / rel, seconds=2, size=SIZE, vcodec="libx265", crf=28)

    def row(self, path):
        return self.db.get_file_by_path(str(path))

    def reason(self, path):
        return self.row(path).reason

    def status(self, path):
        row = self.row(path)
        return row.status if row else None

    def remove_root(self):
        shutil.move(str(self.root), str(self.tmp_path / "gone"))

    def restore_root(self):
        shutil.move(str(self.tmp_path / "gone"), str(self.root))

    def library(self):
        return self.db.list_libraries()[0]


@pytest.fixture
def env(tmp_path, monkeypatch):
    e = Env(tmp_path, monkeypatch)
    e.bloated("seed.mkv")
    e.scanner.scan_library(e.lib.id)
    e.f = e.row(e.root / "seed.mkv")
    return e


@pytest.fixture
def clock_env(tmp_path, monkeypatch):
    return Env(tmp_path, monkeypatch, stable=60.0)


def test_scan_queues_bloated_and_skips_x265(env):
    big, small = env.bloated(), env.efficient()
    env.scanner.scan_library(env.lib.id)
    assert env.status(big) == "queued"
    assert env.row(big).codec == "h264"
    assert env.status(small) == "skipped"
    assert env.reason(small) == "Already x265"


def test_scan_ignores_work_root_when_library_is_a_drive_root(env, monkeypatch):
    # Defence: a whole-volume library must not pick up ShelfSaver's own folders.
    drive = env.tmp_path / "drive"
    monkeypatch.setattr(swap, "_volume_root", lambda path: str(drive))
    lib = env.db.add_library("Drive", str(drive), "high", False)
    make_video(drive / "ShelfSaver" / "holding" / "Movies" / "old.mkv", seconds=2,
               size=SIZE, crf=1)
    make_video(drive / "ShelfSaver" / "incoming" / "new.mkv", seconds=2, size=SIZE, crf=1)
    keep = make_video(drive / "keep.mkv", seconds=2, size=SIZE, crf=1)
    env.scanner.scan_library(lib.id)
    paths = [f.path for f in env.db.files_in_library(lib.id)]
    assert paths == [str(keep)]


@pytest.mark.parametrize("rel", ["D/VIDEO_TS/VTS_01_1.VOB", "D/BDMV/STREAM/00001.m2ts",
                                 "a.mkv.partial", "b.mkv.part", "c.mkv.!qB",
                                 "D/VIDEO_TS/clip.mkv"])
def test_excluded_paths(env, rel):
    target = env.root / rel.replace("/", os.sep)
    if rel.endswith((".partial", ".part", ".!qB", ".VOB")):
        _write_junk(target)
    else:
        make_video(target, seconds=2, size=SIZE, crf=1)
    env.scanner.scan_library(env.lib.id)
    assert env.row(target) is None


def _write_junk(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * 1000)


def test_growing_file_not_queued_until_stable(clock_env):
    e = clock_env
    target = e.bloated()
    e.scanner.scan_library(e.lib.id)
    assert e.row(target) is None
    e.clock.advance(30)
    e.scanner.scan_library(e.lib.id)
    assert e.row(target) is None
    with open(target, "ab") as grow:  # it changed, so the 60 seconds start again
        grow.write(b"\0" * 10)
    e.clock.advance(45)
    e.scanner.scan_library(e.lib.id)
    assert e.row(target) is None
    e.clock.advance(61)
    e.scanner.check_pending()
    assert e.status(target) == "queued"


def test_watcher_picks_up_dropped_file(tmp_path, monkeypatch):
    e = Env(tmp_path, monkeypatch)
    e.scanner.pending_interval = 0.2
    stop = threading.Event()
    thread = threading.Thread(target=e.scanner.start, args=(stop,))
    thread.start()
    try:
        time.sleep(1.0)  # let the first scan and the watch start
        target = e.bloated("dropped.mkv")
        deadline = time.time() + 10
        while time.time() < deadline and e.status(target) is None:
            time.sleep(0.2)
        assert e.status(target) == "queued"
    finally:
        stop.set()
        thread.join(timeout=10)
    assert not thread.is_alive()


def test_start_ends_promptly_when_stopped(tmp_path, monkeypatch):
    e = Env(tmp_path, monkeypatch)
    stop = threading.Event()
    thread = threading.Thread(target=e.scanner.start, args=(stop,))
    thread.start()
    time.sleep(0.5)
    stop.set()
    thread.join(timeout=5)
    assert not thread.is_alive()


def test_refresh_watches_follows_libraries(tmp_path, monkeypatch):
    e = Env(tmp_path, monkeypatch)
    stop = threading.Event()
    thread = threading.Thread(target=e.scanner.start, args=(stop,))
    thread.start()
    try:
        time.sleep(0.5)
        other = tmp_path / "other"
        other.mkdir()
        lib2 = e.db.add_library("Other", str(other), "high", False)
        e.scanner.refresh_watches()
        assert len(e.scanner._watches) == 2
        e.db.remove_library(lib2.id)
        e.scanner.refresh_watches()
        assert len(e.scanner._watches) == 1
    finally:
        stop.set()
        thread.join(timeout=10)
    assert not thread.is_alive()


def test_offline_root_marks_library_and_keeps_rows(env):
    env.remove_root()
    env.scanner.scan_library(env.lib.id)
    assert env.db.list_libraries()[0].status == "offline"
    assert env.db.get_file(env.f.id).status == "queued"


def test_root_returning_recovers(env):
    env.remove_root()
    env.scanner.scan_library(env.lib.id)
    env.restore_root()
    new = env.bloated("later.mkv")
    env.scanner.scan_library(env.lib.id)
    assert env.library().status == "ok"
    assert env.status(new) == "queued"
    assert env.db.get_file(env.f.id).status == "queued"


def test_deleted_file_is_missing_when_root_is_present(env):
    os.remove(env.f.path)
    env.scanner.scan_library(env.lib.id)
    assert env.db.get_file(env.f.id).status == "missing"


def test_missing_file_returning_is_queued_again(env):
    os.remove(env.f.path)
    env.scanner.scan_library(env.lib.id)
    env.bloated("seed.mkv")
    env.scanner.scan_library(env.lib.id)
    assert env.db.get_file(env.f.id).status == "queued"


@pytest.mark.parametrize("status", ["working", "waiting_in_use"])
def test_files_owned_by_the_worker_are_left_alone(env, status):
    env.db.set_file_status(env.f.id, status, "busy")
    os.remove(env.f.path)
    env.scanner.scan_library(env.lib.id)
    row = env.db.get_file(env.f.id)
    assert (row.status, row.reason) == (status, "busy")


def test_file_in_a_swap_is_left_alone(env):
    run = env.db.start_run(env.f.id, env.f.size, "H.264", None, "why")
    env.db.update_run(run.id, state="swapping")
    env.db.set_file_status(env.f.id, "failed", "swap kept")
    os.remove(env.f.path)
    env.scanner.scan_library(env.lib.id)
    assert env.db.get_file(env.f.id).status == "failed"
    make_video(env.f.path, seconds=3, size=SIZE, crf=1)  # changed, but still owned
    env.scanner.scan_library(env.lib.id)
    assert env.db.get_file(env.f.id).status == "failed"


def test_converted_file_is_not_requeued(env):
    path = env.root / "movie.mp4"  # a new path and extension, as after a conversion
    make_video(path, seconds=2, size=SIZE, crf=1)
    st = os.stat(path)
    env.db.record_converted(env.f.id, str(path), st.st_size, st.st_mtime, "hevc", "mp4",
                            SIZE)
    os.remove(env.f.path) if os.path.exists(env.f.path) else None
    env.scanner.scan_library(env.lib.id)
    assert env.db.get_file(env.f.id).status == "converted"
    # Even if the file looks bloated and changes, it is never converted again.
    make_video(path, seconds=3, size=SIZE, crf=1)
    env.scanner.scan_library(env.lib.id)
    assert env.db.get_file(env.f.id).status == "converted"
    env.scanner.reevaluate(env.lib.id)
    assert env.db.get_file(env.f.id).status == "converted"


def test_unchanged_row_is_not_probed_again(env, monkeypatch):
    from shelfsaver import scanner as scanner_module
    calls = []
    monkeypatch.setattr(scanner_module, "probe", lambda *a: calls.append(a))
    env.scanner.scan_library(env.lib.id)
    assert calls == []


def test_level_change_reevaluates_skipped_not_converted(env):
    mid = env.root / "mid.mkv"
    make_video(mid, seconds=2, size=SIZE, crf=30)  # fairly small for its size
    env.scanner.scan_library(env.lib.id)
    assert env.status(mid) == "skipped"
    assert env.reason(mid) == "Already small"
    done = env.root / "done.mkv"
    make_video(done, seconds=2, size=SIZE, crf=30)
    st = os.stat(done)
    row = env.db.upsert_file(env.lib.id, str(done), st.st_size, st.st_mtime, "converted")
    env.db.update_library(env.lib.id, level="low")
    env.scanner.reevaluate(env.lib.id)
    assert env.db.get_file(row.id).status == "converted"


def test_reevaluate_requeues_a_skipped_file_that_is_now_worth_converting(env):
    big = env.root / "x.mkv"
    make_video(big, seconds=2, size=SIZE, crf=1)
    st = os.stat(big)
    env.db.upsert_file(env.lib.id, str(big), st.st_size, st.st_mtime, "skipped",
                       "Already small")
    env.scanner.reevaluate(env.lib.id)
    assert env.status(big) == "queued"


def test_english_only_strips_converted_file_with_extra_audio(env):
    path = env.root / "multi.mkv"
    make_video(path, seconds=2, size=SIZE, vcodec="libx265", crf=28,
               audio=(("eng", "English"), ("spa", "Spanish")))
    st = os.stat(path)
    row = env.db.upsert_file(env.lib.id, str(path), st.st_size, st.st_mtime, "converted")
    env.scanner.reevaluate(env.lib.id)
    assert env.db.get_file(row.id).status == "converted"  # English-only is off
    env.db.update_library(env.lib.id, english_only=True)
    env.scanner.reevaluate(env.lib.id)
    assert env.db.get_file(row.id).status == "queued"


def test_hardlinked_file_is_skipped(env):
    a = env.bloated("a.mkv")
    b = env.root / "b.mkv"
    os.link(a, b)
    env.scanner.scan_library(env.lib.id)
    assert env.reason(a) == "Linked to another copy"
    assert env.reason(b) == "Linked to another copy"


def test_unreadable_file_fails(env):
    bad = env.root / "bad.mkv"
    _write_junk(bad)
    env.scanner.scan_library(env.lib.id)
    row = env.row(bad)
    assert (row.status, row.reason) == ("failed", UNREADABLE)
    env.scanner.scan_library(env.lib.id)
    assert env.status(bad) == "failed"


def test_locked_file_is_tried_again_not_failed(env):
    from tests.test_worker import exclusive_lock
    path = env.bloated("busy.mkv")
    with exclusive_lock(path):
        env.scanner.scan_library(env.lib.id)
        assert env.row(path) is None
    env.scanner.check_pending()
    assert env.status(path) == "queued"


def test_one_bad_file_does_not_stop_the_scan(env, monkeypatch):
    from shelfsaver import scanner as scanner_module
    real = scanner_module.probe

    def flaky(path, ffprobe):
        if "boom" in str(path):
            raise RuntimeError("unexpected")
        return real(path, ffprobe)

    monkeypatch.setattr(scanner_module, "probe", flaky)
    env.bloated("boom.mkv")
    ok = env.bloated("zzz.mkv")
    env.scanner.scan_library(env.lib.id)
    assert env.status(ok) == "queued"
    assert env.status(env.root / "boom.mkv") is None
