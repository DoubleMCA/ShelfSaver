"""The conversion worker. Every file stays under tmp_path, including the fake library volume."""
from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from shelfsaver import swap, worker as worker_module
from shelfsaver.config import Config
from shelfsaver.db import Database
from shelfsaver.encode import EncodeResult
from shelfsaver.probe import probe
from shelfsaver.swap import FileInUse, TargetExists, UnexpectedSwapState
from shelfsaver.worker import GB, IN_USE_RETRY_SECONDS, Worker
from tests.helpers import make_video

SIZE = "640x360"
LOTS_OF_ROOM = 10_000 * GB
BREAKER_MESSAGE = "Paused: conversions keep failing. Check the graphics card and logs."


class SimulatedCrash(Exception):
    """Not an OSError, so nothing can mistake it for an ordinary disk failure."""


def source_video(path, **kw):
    """Big enough for its resolution that `decide` always wants to convert it."""
    kw.setdefault("seconds", 2)
    kw.setdefault("size", SIZE)
    kw.setdefault("crf", 1)
    return make_video(path, **kw)


class Env:
    def __init__(self, tmp_path, monkeypatch):
        self.monkeypatch = monkeypatch
        self.tmp_path = tmp_path
        self.volume = tmp_path / "vol"
        self.lib = self.volume / "Media" / "Movies"
        self.lib.mkdir(parents=True)
        self.scratch = tmp_path / "scratch"
        self.scratch.mkdir()
        monkeypatch.setattr(swap, "_volume_root", lambda path: str(self.volume))

        self.db = Database(tmp_path / "data" / "test.db")
        self.db.set_setting("scratch_dir", str(self.scratch))
        self.library = self.db.add_library("Movies", str(self.lib), "high", False)
        self.config = Config(data_dir=tmp_path / "data")
        self.worker = Worker(self.db, self.config)

        self.free = {"scratch": LOTS_OF_ROOM, "library": LOTS_OF_ROOM}
        monkeypatch.setattr(worker_module, "free_bytes", self._free_bytes)
        self.encodes = 0
        self.encode_output = None  # kwargs for make_video; None means a good smaller file
        monkeypatch.setattr(worker_module, "run_ffmpeg", self._fake_encode)
        self.ids = []
        self.lib_file = None
        self.original_bytes = None

    def _free_bytes(self, path):
        inside_scratch = Path(path).is_relative_to(self.scratch)
        return self.free["scratch" if inside_scratch else "library"]

    def _fake_encode(self, cmd, duration, on_progress, should_stop):
        self.encodes += 1
        kw = self.encode_output or dict(vcodec="libx265", crf=35, seconds=2, size=SIZE)
        make_video(Path(cmd[-1]), **kw)
        on_progress(1.0)
        return EncodeResult(0, False, "encoder said hello")

    def use_encode(self, fake):
        self.monkeypatch.setattr(worker_module, "run_ffmpeg", fake)

    def use_good_encode(self):
        self.use_encode(self._fake_encode)

    def fail_encodes(self):
        def failing(cmd, duration, on_progress, should_stop):
            self.encodes += 1
            return EncodeResult(1, False, "hardware says no")
        self.use_encode(failing)

    def queue(self, path, row_size=None):
        st = os.stat(path)
        row = self.db.upsert_file(self.library.id, str(path), row_size or st.st_size,
                                  st.st_mtime, "queued")
        self.ids.append(row.id)
        self.lib_file = Path(path)
        self.original_bytes = Path(path).read_bytes()
        return row

    def queue_video(self, name="movie.mkv", **kw):
        return self.queue(source_video(self.lib / name, **kw))

    def status(self, file_id=None):
        return self.db.get_file(file_id or self.ids[0]).status

    def file(self, file_id=None):
        return self.db.get_file(file_id or self.ids[0])

    def run_row(self, file_id=None):
        return self.db.file_details(file_id or self.ids[0])["runs"][0]

    def held(self, name):
        return self.volume / "ShelfSaver" / "holding" / "Movies" / name

    @property
    def incoming(self):
        return self.volume / "ShelfSaver" / "incoming"

    def incoming_files(self):
        return sorted(p.name for p in self.incoming.iterdir()) if self.incoming.exists() else []

    def scratch_runs(self):
        folder = self.scratch / "ShelfSaver"
        return sorted(p.name for p in folder.iterdir()) if folder.exists() else []

    def restarted_worker(self):
        """A fresh worker, as after the app restarts."""
        self.worker = Worker(self.db, self.config)
        return self.worker

    def crash_before_swap(self):
        """Run a file up to the moment the swap would start, then die."""
        def crash(plan):
            raise SimulatedCrash("power cut")
        self.monkeypatch.setattr(worker_module, "execute_swap", crash)
        self.worker.process_one()
        self.monkeypatch.setattr(worker_module, "execute_swap", swap.execute_swap)


@pytest.fixture
def env(tmp_path, monkeypatch):
    return Env(tmp_path, monkeypatch)


# ---- End to end on the real graphics card ----

def real_encoder(monkeypatch):
    from shelfsaver.encode import run_ffmpeg
    monkeypatch.setattr(worker_module, "run_ffmpeg", run_ffmpeg)


@pytest.mark.gpu
def test_end_to_end_converts_and_holds_original(env, monkeypatch):
    real_encoder(monkeypatch)
    f = env.queue(make_video(env.lib / "movie [2019] 'ü'.mkv"))
    env.worker.process_one()
    row = env.db.get_file(f.id)
    assert row.status == "converted" and probe(row.path, "ffprobe").video_codec == "hevc"
    assert env.held("movie [2019] 'ü'.mkv").read_bytes() == env.original_bytes
    assert env.db.stats()["saved_bytes"] > 0
    assert env.scratch_runs() == [] and env.incoming_files() == []


@pytest.mark.gpu
def test_end_to_end_strip_only_removes_spanish(env, monkeypatch):
    real_encoder(monkeypatch)
    env.db.update_library(env.library.id, english_only=True)
    f = env.queue(make_video(env.lib / "two.mkv", seconds=2, size="320x240", vcodec="libx265",
                             crf=40, audio=(("eng", "English"), ("spa", "Spanish"))))
    env.worker.process_one()
    row = env.db.get_file(f.id)
    assert row.status == "converted"
    info = probe(row.path, "ffprobe")
    assert [s.language for s in info.streams if s.kind == "audio"] == ["eng"]
    tracks = json.loads(env.run_row()["tracks_json"])
    assert [t["language"] for t in tracks["removed"]] == ["spa"]
    assert "eng" in [t["language"] for t in tracks["kept"]]


@pytest.mark.gpu
def test_end_to_end_avi_becomes_mkv(env, monkeypatch):
    real_encoder(monkeypatch)
    f = env.queue(make_video(env.lib / "old.avi", seconds=2))
    env.worker.process_one()
    row = env.db.get_file(f.id)
    assert row.status == "converted", row.reason
    assert row.path == str(env.lib / "old.mkv") and Path(row.path).exists()
    assert not (env.lib / "old.avi").exists()
    assert env.held("old.avi").read_bytes() == env.original_bytes
    assert row.size == Path(row.path).stat().st_size
    assert row.mtime == Path(row.path).stat().st_mtime


# ---- The pipeline with a fake encoder and real files ----

def test_success_records_the_outcome(env):
    f = env.queue_video()
    assert env.worker.process_one() is True
    row, run = env.file(), env.run_row()
    new_size = Path(row.path).stat().st_size
    assert row.status == "converted" and row.reason is None
    assert (row.size, row.mtime) == (new_size, Path(row.path).stat().st_mtime)
    assert row.codec == "hevc"
    assert run["state"] == "done" and run["finished_at"]
    assert run["was_size"] == len(env.original_bytes) and run["now_size"] == new_size
    assert run["saved_bytes"] == run["was_size"] - run["now_size"] > 0
    assert (run["was_format"], run["now_format"]) == ("H.264", "x265")
    assert run["now_path"] == row.path
    assert json.loads(run["checks_json"]) == {
        "length": True, "tracks": True, "smaller": True, "plays": True}
    assert [t["kind"] for t in json.loads(run["tracks_json"])["kept"]] == ["video", "audio"]
    assert env.held("movie.mkv").read_bytes() == env.original_bytes
    assert env.scratch_runs() == [] and env.incoming_files() == []
    assert env.worker.status()["state"] == "idle"
    assert f.id == row.id


def test_nothing_queued_returns_false_and_is_idle(env):
    assert env.worker.process_one() is False
    assert env.worker.status() == {"state": "idle", "file": None, "step": None, "percent": None,
                                   "eta_seconds": None, "message": None}


def test_holding_off_replaces_without_keeping_a_copy(env):
    env.db.set_setting("holding_enabled", "0")
    env.queue_video()
    env.worker.process_one()
    assert env.status() == "converted"
    assert not env.held("movie.mkv").exists()
    assert probe(env.lib_file, "ffprobe").video_codec == "hevc"


def test_decide_skip_records_reason_and_moves_on(env):
    f = env.queue(make_video(env.lib / "x265.mkv", seconds=2, size=SIZE, vcodec="libx265", crf=35))
    assert env.worker.process_one() is True
    row = env.db.get_file(f.id)
    assert (row.status, row.reason) == ("skipped", "Already x265")
    assert env.encodes == 0
    assert env.db.file_details(f.id)["runs"] == []
    assert env.lib_file.read_bytes() == env.original_bytes
    assert env.worker.status()["state"] == "idle"


def test_ffmpeg_gets_only_the_fixed_scratch_names(env):
    seen = []

    def spy(cmd, duration, on_progress, should_stop):
        seen.append(list(cmd))
        return env._fake_encode(cmd, duration, on_progress, should_stop)

    env.use_encode(spy)
    f = env.queue_video(name="odd [name] 'ü' & more.mkv")
    env.worker.process_one()
    run_folder = env.scratch / "ShelfSaver" / f"run-{env.run_row(f.id)['id']}"
    assert str(run_folder / "source.mkv") in seen[0]
    assert seen[0][-1] == str(run_folder / "output.mkv")
    assert not any("odd" in part for part in seen[0])


def test_verify_failure_keeps_original_and_records_reason(env):
    env.encode_output = dict(vcodec="libx265", crf=35, seconds=8, size=SIZE)
    env.queue_video()
    env.worker.process_one()
    row, run = env.file(), env.run_row()
    assert (row.status, row.reason) == ("failed", "New file is a different length")
    assert env.lib_file.read_bytes() == env.original_bytes
    assert run["state"] == "failed" and run["finished_at"]
    assert json.loads(run["checks_json"])["length"] is False
    assert "encoder said hello" in run["log_tail"]
    assert env.scratch_runs() == [] and env.incoming_files() == []


def test_not_worth_it_is_skipped_not_failed(env):
    # A lightly compressed source, and a "converted" file that came out far bigger.
    env.encode_output = dict(vcodec="libx265", crf=1, seconds=2, size=SIZE)
    env.queue_video(crf=20)
    env.worker.process_one()
    row, run = env.file(), env.run_row()
    assert (row.status, row.reason) == ("skipped", "Would not save enough")
    assert run["state"] == "not_worth_it"
    assert env.lib_file.read_bytes() == env.original_bytes
    assert env.scratch_runs() == []


def test_original_changed_during_convert_is_requeued_not_overwritten(env):
    def changes_original(cmd, duration, on_progress, should_stop):
        env.lib_file.write_bytes(b"newer")
        return env._fake_encode(cmd, duration, on_progress, should_stop)

    env.use_encode(changes_original)
    env.queue_video()
    env.worker.process_one()
    assert env.lib_file.read_bytes() == b"newer" and env.status() == "queued"
    assert env.scratch_runs() == [] and env.incoming_files() == []
    assert env.run_row()["state"] == "failed"


def test_low_scratch_space_blocks_without_failing(env):
    f = env.queue_video()
    env.free["scratch"] = 2 * f.size + 5 * GB - 1
    assert env.worker.process_one() is False
    status = env.worker.status()
    assert status["state"] == "blocked"
    assert status["message"] == "Not enough room on the scratch disk"
    assert env.status() == "queued" and env.encodes == 0
    assert env.scratch_runs() == []

    env.free["scratch"] = 2 * f.size + 5 * GB
    assert env.worker.process_one() is True
    assert env.status() == "converted"
    assert env.worker.status()["state"] == "idle"


def test_scratch_disk_that_stops_answering_means_waiting_not_failing(env, monkeypatch):
    def unplugged(path):
        raise OSError("drive not ready")

    monkeypatch.setattr(worker_module, "free_bytes", unplugged)
    env.queue_video()
    assert env.worker.process_one() is False
    assert env.worker.status()["state"] == "no_scratch"
    assert env.status() == "queued"


def test_low_library_space_blocks_and_deletes_nothing(env):
    env.queue_video()
    env.free["library"] = 5 * GB
    assert env.worker.process_one() is False
    status = env.worker.status()
    assert status["state"] == "blocked"
    assert status["message"] == "Not enough room on the library drive"
    assert env.lib_file.read_bytes() == env.original_bytes
    assert env.incoming_files() == []
    run = env.run_row()
    assert (env.scratch / "ShelfSaver" / f"run-{run['id']}" / "output.mkv").exists()
    assert env.status() != "failed"

    # Still no room: nothing changes, and no second conversion is started.
    assert env.worker.process_one() is False
    assert env.encodes == 1

    env.free["library"] = 6 * GB
    assert env.worker.process_one() is True
    assert env.status() == "converted" and env.encodes == 1
    assert env.worker.status()["state"] == "idle"


def test_sha_mismatch_fails_and_keeps_original(env, monkeypatch):
    real_hash = worker_module._sha256
    calls = []

    def damaged(path, should_stop):
        calls.append(path)
        return "damaged" if len(calls) == 2 else real_hash(path, should_stop)

    monkeypatch.setattr(worker_module, "_sha256", damaged)
    env.queue_video()
    env.worker.process_one()
    row = env.file()
    assert row.status == "failed" and "damaged" in row.reason
    assert env.lib_file.read_bytes() == env.original_bytes
    assert env.incoming_files() == [] and env.scratch_runs() == []
    assert not env.held("movie.mkv").exists()


def test_library_at_a_volume_root_fails_before_any_work(env):
    drive_root = env.db.add_library("Whole", str(env.volume), "high", False)
    path = source_video(env.volume / "at-root.mkv")
    st = path.stat()
    f = env.db.upsert_file(drive_root.id, str(path), st.st_size, st.st_mtime, "queued")
    assert env.worker.process_one() is True
    row = env.db.get_file(f.id)
    assert row.status == "failed" and "whole drive" in row.reason
    assert env.encodes == 0 and env.db.file_details(f.id)["runs"] == []


def test_make_plan_refusal_fails_the_run_and_cleans_up(env, monkeypatch):
    def refuse(*args, **kwargs):
        raise ValueError("not allowed")

    monkeypatch.setattr(worker_module, "make_plan", refuse)
    env.queue_video()
    env.worker.process_one()
    assert env.status() == "failed"
    assert env.lib_file.read_bytes() == env.original_bytes
    assert env.incoming_files() == [] and env.scratch_runs() == []


def test_missing_file_becomes_missing_when_the_library_is_there(env):
    f = env.queue_video()
    env.lib_file.unlink()
    assert env.worker.process_one() is True
    assert env.status(f.id) == "missing"


def test_unreadable_file_fails_with_a_plain_reason(env):
    path = env.lib / "broken.mkv"
    path.write_bytes(b"not a video")
    f = env.queue(path)
    assert env.worker.process_one() is True
    row = env.db.get_file(f.id)
    assert (row.status, row.reason) == ("failed", "Couldn't read this file")
    assert path.read_bytes() == b"not a video"


def test_offline_library_is_left_alone_and_other_libraries_continue(env):
    other_root = env.volume / "Media" / "Gone"
    other_root.mkdir()
    gone = env.db.add_library("Gone", str(other_root), "high", False)
    path = source_video(other_root / "away.mkv")
    st = path.stat()
    away = env.db.upsert_file(gone.id, str(path), st.st_size * 100, st.st_mtime, "queued")
    shutil.rmtree(other_root)

    f = env.queue_video()
    assert env.worker.process_one() is True
    assert env.status(f.id) == "converted"
    assert env.db.get_file(away.id).status == "queued"
    assert env.worker.process_one() is False
    assert env.db.get_file(away.id).status == "queued"


# ---- The three-failure breaker ----

def test_three_encoder_failures_pause_and_requeue(env):
    for name in ("a.mkv", "b.mkv", "c.mkv", "d.mkv"):
        env.queue_video(name=name)
    env.fail_encodes()
    [env.worker.process_one() for _ in range(3)]
    status = env.worker.status()
    assert status["state"] == "paused" and status["message"] == BREAKER_MESSAGE
    assert all(env.db.get_file(i).status == "queued" for i in env.ids)
    assert env.encodes == 3
    assert env.worker.process_one() is False and env.encodes == 3
    assert env.scratch_runs() == []
    assert any("hardware says no" in r["log_tail"] for r in env.db.history())


def test_an_encoder_failure_alone_fails_just_that_file(env):
    env.queue_video()
    env.fail_encodes()
    env.worker.process_one()
    row = env.file()
    assert row.status == "failed" and row.reason
    assert "hardware" not in row.reason
    assert env.worker.status()["state"] == "idle"
    assert env.lib_file.read_bytes() == env.original_bytes


def test_a_successful_encode_resets_the_breaker(env):
    for name in ("a.mkv", "b.mkv", "good.mkv", "c.mkv", "d.mkv"):
        env.queue_video(name=name)
    env.fail_encodes()
    env.worker.process_one()
    env.worker.process_one()
    env.use_good_encode()
    env.worker.process_one()
    env.fail_encodes()
    env.worker.process_one()
    env.worker.process_one()
    assert env.worker.status()["state"] == "idle"


def test_bad_file_failures_do_not_trip_the_breaker(env):
    env.encode_output = dict(vcodec="libx265", crf=35, seconds=8, size=SIZE)
    for name in ("a.mkv", "b.mkv", "c.mkv", "d.mkv"):
        env.queue_video(name=name)
    for _ in range(4):
        env.worker.process_one()
    assert env.worker.status()["state"] == "idle"
    assert all(env.db.get_file(i).status == "failed" for i in env.ids)


def test_a_stop_is_not_an_encoder_failure(env):
    def stopped(cmd, duration, on_progress, should_stop):
        return EncodeResult(255, True, "")

    env.use_encode(stopped)
    for name in ("a.mkv", "b.mkv", "c.mkv"):
        env.queue_video(name=name)
    for _ in range(3):
        env.worker.process_one()
    assert env.worker.status()["state"] == "idle"
    assert env.db.get_file(env.ids[0]).status == "queued"


def test_ffmpeg_that_will_not_start_counts_as_an_encoder_failure(env):
    def missing(cmd, duration, on_progress, should_stop):
        raise FileNotFoundError("ffmpeg")

    env.use_encode(missing)
    for name in ("a.mkv", "b.mkv", "c.mkv"):
        env.queue_video(name=name)
    for _ in range(3):
        env.worker.process_one()
    assert env.worker.status()["state"] == "paused"


def test_resume_gives_the_breaker_a_fresh_count(env):
    for name in ("a.mkv", "b.mkv", "c.mkv"):
        env.queue_video(name=name)
    env.fail_encodes()
    for _ in range(3):
        env.worker.process_one()
    env.worker.resume()
    assert env.worker.status()["state"] == "idle"
    assert env.worker.status()["message"] is None
    env.worker.process_one()
    assert env.worker.status()["state"] == "idle"


# ---- A file in use ----

def test_in_use_waits_then_finishes_on_retry(env, monkeypatch):
    clock = SimpleNamespace(now=1000.0)
    monkeypatch.setattr(worker_module, "_now", lambda: clock.now)
    f = env.queue_video()
    # Python opens files without delete sharing, so a real rename fails while this is open.
    with open(env.lib_file, "rb"):
        env.worker.process_one()
        row, run = env.file(), env.run_row()
        assert (row.status, row.reason) == ("waiting_in_use", "Waiting, file is in use")
        assert run["state"] == "waiting_in_use"
        assert env.incoming_files() == [f"{run['id']}.mkv"]
        assert env.scratch_runs() == []
        assert env.lib_file.read_bytes() == env.original_bytes

        clock.now += IN_USE_RETRY_SECONDS
        env.worker.process_one()
        assert env.status() == "waiting_in_use"

    clock.now += IN_USE_RETRY_SECONDS - 1
    assert env.worker.process_one() is False
    assert env.status() == "waiting_in_use"
    clock.now += 1
    assert env.worker.process_one() is True
    assert env.status(f.id) == "converted"
    assert env.run_row()["state"] == "done"
    assert env.incoming_files() == []
    assert env.encodes == 1
    assert IN_USE_RETRY_SECONDS == 600


def test_owner_retry_tries_a_waiting_file_right_away(env):
    f = env.queue_video()
    with open(env.lib_file, "rb"):
        env.worker.process_one()
    env.worker.retry(f.id)
    assert env.status() == "waiting_in_use"  # the worker thread does the swap, not the caller
    assert env.worker.process_one() is True
    assert env.status() == "converted"


def test_other_work_continues_while_a_file_is_in_use(env):
    busy = env.queue_video(name="busy.mkv")
    other = env.queue(source_video(env.lib / "other.mkv"), row_size=1)
    with open(env.lib / "busy.mkv", "rb"):
        env.worker.process_one()
        assert env.status(busy.id) == "waiting_in_use"
        env.worker.process_one()
        assert env.status(other.id) == "converted"
        assert env.status(busy.id) == "waiting_in_use"


def test_original_changed_while_waiting_is_requeued_and_temp_dropped(env):
    f = env.queue_video()
    with open(env.lib_file, "rb"):
        env.worker.process_one()
    env.lib_file.write_bytes(b"newer")
    env.worker.retry(f.id)
    env.worker.process_one()
    assert env.lib_file.read_bytes() == b"newer"
    assert env.incoming_files() == []
    assert env.run_row()["state"] == "failed"
    # Requeued in the same pass, which then finds the new content isn't a video.
    assert env.file().reason == "Couldn't read this file"


def test_requeued_file_with_a_swap_pending_never_starts_a_second_run(env):
    f = env.queue_video()
    with open(env.lib_file, "rb"):
        env.worker.process_one()
        env.db.requeue([f.id])  # what a rescan could do
        env.worker.process_one()
        assert env.status() == "waiting_in_use"
    assert env.encodes == 1
    assert len(env.db.file_details(f.id)["runs"]) == 1


# ---- Swap failures: temp can be the only copy ----

@pytest.mark.parametrize("error", [UnexpectedSwapState("no match"), FileNotFoundError("gone"),
                                   OSError("disk trouble")])
def test_swap_trouble_keeps_temp_and_can_finish_on_retry(env, monkeypatch, caplog, error):
    def trouble(plan):
        raise error

    monkeypatch.setattr(worker_module, "execute_swap", trouble)
    f = env.queue_video()
    with caplog.at_level(logging.ERROR, logger="shelfsaver.worker"):
        env.worker.process_one()
    row, run = env.file(), env.run_row()
    assert run["state"] == "swapping"
    assert env.incoming_files() == [f"{run['id']}.mkv"]
    assert row.status == "failed" and row.reason and "no match" not in row.reason
    assert any(r.levelno == logging.ERROR for r in caplog.records)
    assert env.lib_file.read_bytes() == env.original_bytes

    # The queue carries on, and the owner's Retry finishes the same run without converting again.
    monkeypatch.setattr(worker_module, "execute_swap", swap.execute_swap)
    assert env.worker.process_one() is False
    assert env.run_row()["state"] == "swapping"
    env.worker.retry(f.id)
    assert env.worker.process_one() is True
    assert env.status(f.id) == "converted" and env.encodes == 1
    assert env.run_row()["state"] == "done"
    assert env.scratch_runs() == []


def test_target_exists_fails_the_run_when_the_original_is_intact(env, monkeypatch):
    def taken(plan):
        raise TargetExists("in the way")

    monkeypatch.setattr(worker_module, "execute_swap", taken)
    env.queue_video()
    env.worker.process_one()
    row, run = env.file(), env.run_row()
    assert row.status == "failed" and "in the way" not in row.reason
    assert run["state"] == "failed"
    assert env.lib_file.read_bytes() == env.original_bytes
    assert env.incoming_files() == [] and env.scratch_runs() == []


def test_real_file_in_the_way_of_a_new_extension_fails_and_overwrites_nothing(env):
    avi = env.queue(source_video(env.lib / "film.avi"))
    (env.lib / "film.mkv").write_bytes(b"someone else's file")
    env.worker.process_one()
    assert env.status(avi.id) == "failed"
    assert (env.lib / "film.mkv").read_bytes() == b"someone else's file"
    assert (env.lib / "film.avi").read_bytes() == env.original_bytes


def test_target_exists_keeps_temp_when_the_original_is_not_where_it_was(env, monkeypatch):
    def taken_after_moving(plan):
        os.replace(plan.original, plan.holding if plan.holding else plan.original + ".x")
        raise TargetExists("in the way")

    monkeypatch.setattr(worker_module, "execute_swap", taken_after_moving)
    os.makedirs(env.held("x").parent)
    env.queue_video()
    env.worker.process_one()
    run = env.run_row()
    assert run["state"] == "swapping"
    assert env.incoming_files() == [f"{run['id']}.mkv"]


def test_unexpected_crash_during_swap_keeps_everything_for_recovery(env, monkeypatch):
    def crash(plan):
        raise SimulatedCrash("boom")

    monkeypatch.setattr(swap, "_last_step", crash)
    env.queue_video()
    env.worker.process_one()  # must not raise
    assert env.run_row()["state"] == "swapping"
    assert len(env.incoming_files()) == 1
    assert env.held("movie.mkv").read_bytes() == env.original_bytes


def test_unexpected_error_before_the_swap_fails_the_file_not_the_worker(env, monkeypatch):
    def broken(*args, **kwargs):
        raise SimulatedCrash("bug")

    monkeypatch.setattr(worker_module, "build_command", broken)
    env.queue_video()
    assert env.worker.process_one() is True
    assert env.status() == "failed"
    assert env.run_row()["state"] == "failed"
    assert env.lib_file.read_bytes() == env.original_bytes
    assert env.scratch_runs() == []


# ---- Pause, resume and stop ----

def waits_for_stop(started):
    def fake(cmd, duration, on_progress, should_stop):
        started.set()
        while not should_stop():
            time.sleep(0.01)
        return EncodeResult(255, True, "")
    return fake


def test_pause_stops_current_and_requeues(env):
    started = threading.Event()
    env.use_encode(waits_for_stop(started))
    env.queue_video()
    thread = threading.Thread(target=env.worker.process_one)
    thread.start()
    assert started.wait(30)
    assert env.worker.status()["state"] == "working"
    assert env.worker.status()["step"] == "Converting"
    env.worker.pause("Taking a break")
    thread.join(30)
    assert not thread.is_alive()

    status = env.worker.status()
    assert status["state"] == "paused" and status["message"] == "Taking a break"
    assert status["file"] is None
    assert env.status() == "queued"
    assert env.scratch_runs() == [] and env.incoming_files() == []
    assert env.lib_file.read_bytes() == env.original_bytes
    assert env.run_row()["state"] == "failed"

    assert env.worker.process_one() is False
    env.worker.resume()
    env.use_good_encode()
    assert env.worker.process_one() is True
    assert env.status() == "converted"


def test_pause_during_checking_requeues_instead_of_failing(env):
    env.queue_video()
    real_verify = worker_module.verify_output

    def verify_with_pause(*args, **kwargs):
        env.worker.pause()
        return real_verify(*args, **kwargs)  # the real checks see the stop right away

    env.monkeypatch.setattr(worker_module, "verify_output", verify_with_pause)
    env.worker.process_one()
    assert env.status() == "queued"
    assert env.scratch_runs() == []


def test_pause_while_copying_to_the_library_removes_the_partial_temp(env, monkeypatch):
    real_copy = worker_module._copy_file

    def copy_then_pause(source, target, on_fraction, should_stop):
        real_copy(source, target, on_fraction, should_stop)
        if Path(target).parent == env.incoming:
            env.worker.pause()

    monkeypatch.setattr(worker_module, "_copy_file", copy_then_pause)
    env.queue_video()
    env.worker.process_one()
    assert env.status() == "queued"
    assert env.incoming_files() == [] and env.scratch_runs() == []


def test_nonzero_exit_during_a_pause_is_treated_as_the_pause(env):
    def dies_as_paused(cmd, duration, on_progress, should_stop):
        env.worker.pause()
        return EncodeResult(1, False, "terminated")  # exited before the stop was noticed

    env.use_encode(dies_as_paused)
    env.queue_video()
    env.worker.process_one()
    assert env.status() == "queued"


def test_stop_event_also_stops_current_work(env):
    started = threading.Event()
    env.use_encode(waits_for_stop(started))
    env.queue_video()
    stop = threading.Event()
    thread = threading.Thread(target=env.worker.run_forever, args=(stop,))
    thread.start()
    assert started.wait(30)
    stop.set()
    thread.join(30)
    assert not thread.is_alive()
    assert env.status() == "queued"
    assert env.scratch_runs() == []


def test_run_forever_sleeps_when_idle_and_stops_promptly(env):
    calls = []
    real = env.worker.process_one

    def counted():
        calls.append(1)
        return real()

    env.worker.process_one = counted
    stop = threading.Event()
    thread = threading.Thread(target=env.worker.run_forever, args=(stop,))
    thread.start()
    time.sleep(0.5)
    began = time.monotonic()
    stop.set()
    thread.join(10)
    assert not thread.is_alive()
    assert time.monotonic() - began < 1
    assert 1 <= len(calls) <= 5


def test_run_forever_survives_an_unexpected_error(env):
    calls = []

    def broken():
        calls.append(1)
        raise SimulatedCrash("bug")

    env.worker.process_one = broken
    env.monkeypatch.setattr(worker_module, "IDLE_WAIT_SECONDS", 0.01)
    stop = threading.Event()
    thread = threading.Thread(target=env.worker.run_forever, args=(stop,))
    thread.start()
    deadline = time.monotonic() + 10
    while len(calls) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    stop.set()
    thread.join(10)
    assert len(calls) >= 2


def test_status_can_be_read_while_working_and_shows_progress(env):
    seen = []

    def reports(cmd, duration, on_progress, should_stop):
        on_progress(0.5)
        seen.append(env.worker.status())
        return env._fake_encode(cmd, duration, on_progress, should_stop)

    env.use_encode(reports)
    f = env.queue_video()
    env.worker.process_one()
    assert seen[0]["state"] == "working" and seen[0]["step"] == "Converting"
    assert seen[0]["file"] == f.path and seen[0]["percent"] == 50
    assert seen[0]["eta_seconds"] is not None and seen[0]["eta_seconds"] >= 0


# ---- Retry ----

def test_retry_requeues_a_failed_file(env):
    f = env.queue_video()
    env.fail_encodes()
    env.worker.process_one()
    assert env.status() == "failed"
    env.worker.retry(f.id)
    assert env.status() == "queued" and env.file().reason is None


def test_retry_leaves_other_files_alone(env):
    env.queue_video()
    env.worker.process_one()
    assert env.status() == "converted"
    env.worker.retry(env.ids[0])
    assert env.status() == "converted"
    env.worker.retry(99999)  # no such file


# ---- Startup recovery ----

def test_recover_finishes_interrupted_swap(env, monkeypatch):
    def crash(plan):
        raise SimulatedCrash("power cut between the two renames")

    monkeypatch.setattr(swap, "_last_step", crash)
    f = env.queue_video()
    env.worker.process_one()
    assert env.held("movie.mkv").exists() and not env.lib_file.exists()
    monkeypatch.undo()
    monkeypatch.setattr(swap, "_volume_root", lambda path: str(env.volume))

    worker = Worker(env.db, env.config)
    worker.recover()
    row = env.db.get_file(f.id)
    assert row.status == "converted"
    assert row.size == env.lib_file.stat().st_size
    assert probe(env.lib_file, "ffprobe").video_codec == "hevc"
    assert env.held("movie.mkv").read_bytes() == env.original_bytes
    assert env.run_row()["state"] == "done" and env.db.stats()["saved_bytes"] > 0
    assert env.incoming_files() == [] and env.scratch_runs() == []


def test_recover_carries_out_a_swap_that_had_not_started(env):
    env.queue_video()
    env.crash_before_swap()
    assert env.run_row()["state"] == "swapping"
    env.restarted_worker().recover()
    assert env.status() == "converted"
    assert env.held("movie.mkv").read_bytes() == env.original_bytes


def test_recover_requeues_when_original_changed_while_the_app_was_down(env):
    env.queue_video()
    env.crash_before_swap()
    env.lib_file.write_bytes(b"newer")
    env.restarted_worker().recover()
    assert env.status() == "queued"
    assert env.lib_file.read_bytes() == b"newer"
    assert env.incoming_files() == []
    assert env.run_row()["state"] == "failed"


def test_recover_waiting_run_still_in_use_keeps_waiting(env):
    env.queue_video()
    env.crash_before_swap()
    with open(env.lib_file, "rb"):
        env.restarted_worker().recover()
        assert env.status() == "waiting_in_use"
        assert env.run_row()["state"] == "waiting_in_use"
        assert len(env.incoming_files()) == 1


def test_recover_unexpected_state_keeps_temp_and_later_retry_can_finish(env, monkeypatch, caplog):
    f = env.queue_video()
    env.crash_before_swap()
    temp_name = env.incoming_files()[0]
    away = env.tmp_path / "away.mkv"
    os.replace(env.lib_file, away)  # looks like an absent original

    with caplog.at_level(logging.ERROR, logger="shelfsaver.worker"):
        env.restarted_worker().recover()
    assert env.run_row()["state"] == "swapping"
    assert env.incoming_files() == [temp_name]
    assert env.status(f.id) == "failed" and env.file().reason
    assert any(r.levelno == logging.ERROR for r in caplog.records)

    # The drive comes back and the owner retries.
    os.replace(away, env.lib_file)
    env.worker.retry(f.id)
    assert env.worker.process_one() is True
    assert env.status(f.id) == "converted"


@pytest.mark.parametrize("error", [FileInUse("x"), TargetExists("x"), OSError("x")])
def test_recover_survives_every_error_from_recover_swap(env, monkeypatch, error):
    env.queue_video()
    env.crash_before_swap()
    temp_name = env.incoming_files()[0]

    def raising(plan):
        raise error

    monkeypatch.setattr(worker_module, "recover_swap", raising)
    env.restarted_worker().recover()
    assert env.lib_file.read_bytes() == env.original_bytes
    if not isinstance(error, TargetExists):
        assert env.incoming_files() == [temp_name]


@pytest.mark.parametrize("bad", ["not json", "[]", "{}", '{"original": 1}'])
def test_recover_survives_a_bad_plan(env, bad):
    first = env.queue_video(name="first.mkv")
    env.crash_before_swap()
    env.db.update_run(env.run_row(first.id)["id"], swap_json=bad)
    second = env.queue_video(name="second.mkv")
    env.crash_before_swap()

    env.restarted_worker().recover()
    assert env.run_row(first.id)["state"] == "swapping"
    assert env.status(first.id) == "failed"
    assert (env.lib / "first.mkv").exists()
    assert len(env.incoming_files()) == 1  # the bad plan's temp is kept; the good one finished
    assert env.status(second.id) == "converted"


def test_recover_requeues_working_and_cleans_only_run_folders(env):
    unrelated = env.scratch / "family-photos.zip"
    unrelated.write_bytes(b"keep")
    (env.scratch / "ShelfSaver").mkdir()
    also_unrelated = env.scratch / "ShelfSaver" / "notes.txt"
    also_unrelated.write_bytes(b"keep")
    (env.scratch / "ShelfSaver" / "run-41").mkdir()
    (env.scratch / "ShelfSaver" / "run-41" / "output.mkv").write_bytes(b"leftover")
    (env.scratch / "ShelfSaver" / "other-folder").mkdir()

    f = env.queue_video()
    run = env.db.start_run(f.id, f.size, "H.264", None, "why")
    env.db.set_file_status(f.id, "working")
    rowless = env.queue(source_video(env.lib / "no-run.mkv"))
    env.db.set_file_status(rowless.id, "working")

    env.incoming.mkdir(parents=True)
    partial = env.incoming / f"{run.id}.mkv"
    partial.write_bytes(b"half copied")
    orphan = env.incoming / "999.mkv"
    orphan.write_bytes(b"orphan")

    env.worker.recover()
    assert env.status(f.id) == "queued" and env.status(rowless.id) == "queued"
    assert env.run_row(f.id)["state"] == "failed"
    assert unrelated.read_bytes() == b"keep"
    assert also_unrelated.read_bytes() == b"keep"
    assert (env.scratch / "ShelfSaver" / "other-folder").is_dir()
    assert not (env.scratch / "ShelfSaver" / "run-41").exists()
    assert env.incoming_files() == []
    assert env.lib_file.exists()


def test_recover_keeps_incoming_files_of_runs_still_in_progress(env):
    env.queue_video()
    env.crash_before_swap()
    env.db.update_run(env.run_row()["id"], swap_json="not json")
    keep = env.incoming_files()
    (env.incoming / "12345.mkv").write_bytes(b"orphan")
    env.restarted_worker().recover()
    assert env.incoming_files() == keep


def test_recover_with_no_scratch_set_still_works(env):
    env.db.set_setting("scratch_dir", "")
    f = env.queue_video()
    env.db.set_file_status(f.id, "working")
    env.worker.recover()
    assert env.status(f.id) == "queued"


def test_recover_survives_a_library_on_a_drive_that_is_gone(env):
    env.db.add_library("Gone", str(env.tmp_path / "nowhere" / "lib"), "high", False)
    env.monkeypatch.setattr(worker_module, "_list_folder", _raising_list_folder)
    env.worker.recover()


def _raising_list_folder(path):
    raise OSError("drive not ready")


def test_recover_does_not_follow_a_link_named_like_a_run_folder(env):
    precious = env.tmp_path / "precious"
    precious.mkdir()
    (precious / "keep.txt").write_bytes(b"keep")
    (env.scratch / "ShelfSaver").mkdir()
    link = env.scratch / "ShelfSaver" / "run-7"
    try:
        os.symlink(precious, link, target_is_directory=True)
    except OSError:
        pytest.skip("this account can't create links")
    env.worker.recover()
    assert (precious / "keep.txt").read_bytes() == b"keep"


# ---- No scratch disk ----

def test_no_scratch_set_means_waiting_not_failing(env):
    env.db.set_setting("scratch_dir", "")
    env.queue_video()
    assert env.worker.process_one() is False
    assert env.worker.status()["state"] == "no_scratch"
    assert env.status() == "queued" and env.encodes == 0

    env.db.set_setting("scratch_dir", str(env.scratch))
    assert env.worker.process_one() is True
    assert env.status() == "converted"


def test_scratch_folder_gone_means_waiting_not_failing(env):
    env.db.set_setting("scratch_dir", str(env.tmp_path / "unplugged"))
    env.queue_video()
    assert env.worker.process_one() is False
    assert env.worker.status()["state"] == "no_scratch"
    assert env.status() == "queued"
