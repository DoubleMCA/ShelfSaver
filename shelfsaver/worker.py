"""The conversion pipeline: one file at a time, and never at the cost of an original.

The original stays untouched until the new file has passed every check and a verified copy
sits on the library's volume. From the moment a run is saved in state `swapping`, its temp
file can be the only copy of the new file, so nothing here deletes it unless the original
has been confirmed whole and unchanged where it started.
"""
from __future__ import annotations

import dataclasses
import fnmatch
import hashlib
import json
import logging
import os
import shutil
import stat
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .decide import decide
from .encode import EncodeResult, build_command, output_extension, run_ffmpeg
from .models import Decision, FileRow, Library, MediaInfo, Run
from .probe import ProbeError, probe
from .swap import (
    FileInUse,
    SwapPlan,
    TargetExists,
    UnexpectedSwapState,
    _may_be_a_link,
    execute_swap,
    ext_path,
    is_volume_root,
    make_plan,
    recover_swap,
    work_root,
)
from .verify import verify_output

logger = logging.getLogger(__name__)

GB = 1024 ** 3
SPACE_RESERVE_BYTES = 5 * GB
COPY_CHUNK_BYTES = 8 * 1024 * 1024
IN_USE_RETRY_SECONDS = 600
IDLE_WAIT_SECONDS = 1.0
BLOCKED_WAIT_SECONDS = 30.0
ENCODER_FAILURE_LIMIT = 3

SWAP_STATES = ("swapping", "waiting_in_use")

NO_SCRATCH_SET = "Choose a scratch disk in Settings"
NO_SCRATCH_FOUND = "The scratch disk can't be found"
NO_ROOM_SCRATCH = "Not enough room on the scratch disk"
NO_ROOM_LIBRARY = "Not enough room on the library drive"
BREAKER_MESSAGE = "Paused: conversions keep failing. Check the graphics card and logs."

WAITING_IN_USE = "Waiting, file is in use"
WHOLE_DRIVE = "This library is a whole drive. Choose a folder on the drive instead."
UNREADABLE = "Couldn't read this file"
CONVERT_FAILED = "The conversion failed"
SOMETHING_WRONG = "Something went wrong with this file"
SWAP_KEPT = ("Couldn't finish replacing this file. Nothing was deleted. "
             "Retry it, or it will be tried again at the next start.")
TARGET_TAKEN = "A different file is already where the new file needs to go"
STOPPED_NOTE = "Stopped before finishing, so it will start again"
INTERRUPTED_NOTE = "Interrupted by a restart, so it will start again"
CHANGED_NOTE = "The original changed during the conversion, so it will start again"

_FORMAT_NAMES = {
    "h264": "H.264", "hevc": "x265", "mpeg2video": "MPEG-2", "mpeg1video": "MPEG-1",
    "mpeg4": "MPEG-4", "msmpeg4v3": "DivX", "vc1": "VC-1", "wmv3": "WMV", "av1": "AV1",
    "vp8": "VP8", "vp9": "VP9",
}

_now = time.monotonic


def free_bytes(path) -> int:
    return shutil.disk_usage(ext_path(path)).free


def _list_folder(path) -> list[os.DirEntry]:
    with os.scandir(ext_path(path)) as entries:
        return list(entries)


def _format_name(codec: Optional[str]) -> str:
    return _FORMAT_NAMES.get(codec or "", (codec or "unknown").upper())


def _file_details(info: MediaInfo) -> dict:
    return {
        "codec": info.video_codec,
        "container": info.ext.lstrip("."),
        "resolution": f"{info.width}x{info.height}" if info.width else None,
    }


def _stream_record(stream) -> dict:
    return {"index": stream.index, "kind": stream.kind, "codec": stream.codec,
            "language": stream.language, "title": stream.title}


def _tracks_json(info: MediaInfo, decision: Decision) -> str:
    gone = set(decision.remove_audio)

    def is_removed(stream) -> bool:
        return stream.kind == "data" or stream.index in gone

    return json.dumps({
        "kept": [_stream_record(s) for s in info.streams if not is_removed(s)],
        "removed": [_stream_record(s) for s in info.streams if is_removed(s)],
    })


class _Stopped(Exception):
    """A pause or shutdown interrupted a copy."""


def _copy_file(source, target, on_fraction: Callable[[float], None],
               should_stop: Callable[[], bool]) -> None:
    """Copy contents only, so a read-only flag can't follow the file, and commit them to disk."""
    total = os.stat(ext_path(source)).st_size
    done = 0
    with open(ext_path(source), "rb") as reader, open(ext_path(target), "wb") as writer:
        while chunk := reader.read(COPY_CHUNK_BYTES):
            if should_stop():
                raise _Stopped()
            writer.write(chunk)
            done += len(chunk)
            if total:
                on_fraction(min(1.0, done / total))
        writer.flush()
        os.fsync(writer.fileno())


def _sha256(path, should_stop: Callable[[], bool]) -> str:
    digest = hashlib.sha256()
    with open(ext_path(path), "rb") as reader:
        while chunk := reader.read(COPY_CHUNK_BYTES):
            if should_stop():
                raise _Stopped()
            digest.update(chunk)
    return digest.hexdigest()


def _matches(path, size: int, mtime: float) -> bool:
    """True if the file still has this size and time. Raises `OSError` if it can't be read."""
    info = os.stat(ext_path(path))
    return info.st_size == size and info.st_mtime == mtime


def _load_swap(text: Optional[str]) -> tuple[SwapPlan, int, float]:
    """Read a saved plan. Raises `ValueError`, `TypeError` or `KeyError` if it is damaged."""
    data = json.loads(text or "")
    if not isinstance(data, dict):
        raise ValueError("The saved plan is not a record")
    names = [f.name for f in dataclasses.fields(SwapPlan)]
    plan = SwapPlan(**{name: data[name] for name in names})
    for path in (plan.original, plan.temp, plan.final):
        if not isinstance(path, str):
            raise ValueError("The saved plan has a bad path")
    return plan, int(data["original_size"]), float(data["original_mtime"])


def _dump_swap(plan: SwapPlan, original_size: int, original_mtime: float) -> str:
    return json.dumps({**dataclasses.asdict(plan), "original_size": original_size,
                       "original_mtime": original_mtime})


class _RunFailed(Exception):
    """The run can't succeed. The message is the plain sentence for the file's row."""


@dataclass
class _Job:
    """One file on its way through the pipeline."""
    file: FileRow
    library: Library
    size: int
    mtime: float
    info: MediaInfo
    decision: Decision
    run: Run
    scratch_folder: Path
    output: Path
    temp: Optional[Path] = None
    log_tail: str = ""
    swap_saved: bool = False


class Worker:
    def __init__(self, db, config):
        self.db = db
        self.config = config
        self._lock = threading.Lock()  # guards everything status() and retry() touch
        self._status = self._idle_fields()
        self._step_started = 0.0
        self._paused = threading.Event()
        self._pause_message: Optional[str] = None
        self._stop: Optional[threading.Event] = None
        self._next_swap_try: dict[int, float] = {}  # run id -> when a waiting run is next due
        self._swap_retry_asked: set[int] = set()  # run ids the owner asked to try now
        self._parked: Optional[_Job] = None  # converted and verified, waiting for library room
        self._failed_encodes: list[int] = []  # file ids of the current failures in a row

    # ---- Controls and status, safe to call from any thread ----

    def status(self) -> dict:
        with self._lock:
            current = dict(self._status)
            if self._paused.is_set():
                current["state"] = "paused"
                current["message"] = self._pause_message
        return current

    def pause(self, reason: Optional[str] = None) -> None:
        with self._lock:
            self._pause_message = reason
            self._paused.set()
        logger.info("Paused: %s", reason or "at the owner's request")

    def resume(self) -> None:
        with self._lock:
            self._pause_message = None
            self._failed_encodes = []
            self._paused.clear()
        logger.info("Resumed")

    def retry(self, file_id: int) -> None:
        """Try a failed file again. A run that still holds a new file carries on from there."""
        held = [r.id for r in self.db.active_runs(SWAP_STATES) if r.file_id == file_id]
        if held:
            with self._lock:
                self._swap_retry_asked.update(held)
            return
        file = self.db.get_file(file_id)
        if file is not None and file.status == "failed":
            self.db.requeue([file_id])

    # ---- Status helpers ----

    @staticmethod
    def _idle_fields() -> dict:
        return {"state": "idle", "file": None, "step": None, "percent": None,
                "eta_seconds": None, "message": None}

    def _set_status(self, state: str, file: Optional[str] = None, step: Optional[str] = None,
                    message: Optional[str] = None) -> None:
        with self._lock:
            self._status = {**self._idle_fields(), "state": state, "file": file, "step": step,
                            "message": message}
            self._step_started = _now()

    def _set_idle(self) -> None:
        self._set_status("idle")

    def _begin_step(self, job: _Job, step: str) -> None:
        logger.info("%s: %s", step, job.file.path)
        self._set_status("working", job.file.path, step)
        self.db.update_run(job.run.id, step=step)

    def _on_progress(self, fraction: float) -> None:
        with self._lock:
            self._status["percent"] = round(fraction * 100, 1)
            elapsed = _now() - self._step_started
            self._status["eta_seconds"] = (
                round(elapsed / fraction * (1 - fraction)) if fraction > 0 else None
            )

    def _should_stop(self) -> bool:
        return self._paused.is_set() or (self._stop is not None and self._stop.is_set())

    # ---- The main loop ----

    def run_forever(self, stop: threading.Event) -> None:
        self._stop = stop
        try:
            while not stop.is_set():
                try:
                    worked = self.process_one()
                except Exception:
                    logger.exception("The worker hit an unexpected error")
                    worked = False
                if not worked:
                    blocked = self.status()["state"] == "blocked"
                    stop.wait(BLOCKED_WAIT_SECONDS if blocked else IDLE_WAIT_SECONDS)
        finally:
            self._stop = None

    def process_one(self) -> bool:
        """Do the next piece of work. False means there was nothing to do right now."""
        if self._paused.is_set():
            return False
        worked = self._advance_due_swaps()
        if self._parked is not None:
            return self._guarded(self._parked, self._place_new_file) or worked

        scratch = self._usable_scratch()
        if scratch is None:
            return worked
        file = self._next_file()
        if file is None:
            self._set_idle()
            return worked
        try:
            finished = self._process_file(file, scratch)
        except Exception:
            # Once a run exists `_guarded` handles errors, so no new file can be at stake here.
            logger.exception("Unexpected error with %s", file.path)
            self.db.set_file_status(file.id, "failed", SOMETHING_WRONG)
            finished = True
        if finished:
            self._set_idle()
        return finished or worked

    def _usable_scratch(self) -> Optional[Path]:
        setting = self.db.get_setting("scratch_dir")
        if not setting:
            self._set_status("no_scratch", message=NO_SCRATCH_SET)
            return None
        if not os.path.isdir(ext_path(setting)):
            self._set_status("no_scratch", message=NO_SCRATCH_FOUND)
            return None
        return Path(setting)

    def _next_file(self) -> Optional[FileRow]:
        """The next queued file on a reachable library that has no swap still pending."""
        unreachable = [lib.id for lib in self.db.list_libraries()
                       if not os.path.isdir(ext_path(lib.path))]
        while True:
            file = self.db.next_queued(unreachable)
            if file is None:
                return None
            pending = [r for r in self.db.active_runs(SWAP_STATES) if r.file_id == file.id]
            if not pending:
                return file
            # Something requeued it, but a second run could fight the pending swap's new file.
            if pending[0].state == "waiting_in_use":
                self.db.set_file_status(file.id, "waiting_in_use", WAITING_IN_USE)
            else:
                self.db.set_file_status(file.id, "failed", SWAP_KEPT)

    # ---- Steps 1 and 2: decide whether to work on it at all ----

    def _process_file(self, file: FileRow, scratch: Path) -> bool:
        library = next((lib for lib in self.db.list_libraries() if lib.id == file.library_id),
                       None)
        if library is None:
            self.db.set_file_status(file.id, "failed", "Its library no longer exists")
            return True
        if is_volume_root(library.path):
            logger.error("%s is in a library that is a whole drive", file.path)
            self.db.set_file_status(file.id, "failed", WHOLE_DRIVE)
            return True

        logger.info("Inspecting: %s", file.path)
        self._set_status("working", file.path)
        try:
            info_stat = os.stat(ext_path(file.path))
        except FileNotFoundError:
            logger.info("No longer there: %s", file.path)
            self.db.set_file_status(file.id, "missing")
            return True
        except OSError as exc:
            logger.error("Could not read %s: %s", file.path, exc)
            self.db.set_file_status(file.id, "failed", UNREADABLE)
            return True
        size, mtime = info_stat.st_size, info_stat.st_mtime
        try:
            info = probe(ext_path(file.path), self.config.ffprobe)
        except ProbeError as exc:
            logger.error("Could not read %s: %s", file.path, exc)
            self.db.upsert_file(library.id, file.path, size, mtime, "failed", UNREADABLE)
            return True

        decision = decide(info, library.level, library.english_only,
                          nlink=info_stat.st_nlink,
                          read_only=not info_stat.st_mode & stat.S_IWRITE)
        details = _file_details(info)
        if decision.action == "skip":
            logger.info("Skipping %s: %s", file.path, decision.reason)
            self.db.upsert_file(library.id, file.path, size, mtime, "skipped", decision.reason,
                                **details)
            return True

        try:
            scratch_room = free_bytes(scratch)
        except OSError as exc:
            logger.warning("Could not check the room on %s: %s", scratch, exc)
            self._set_status("no_scratch", message=NO_SCRATCH_FOUND)
            return False
        if scratch_room < 2 * size + SPACE_RESERVE_BYTES:
            logger.info("Not enough room on the scratch disk for %s", file.path)
            self._set_status("blocked", file.path, message=NO_ROOM_SCRATCH)
            return False

        run = self.db.start_run(file.id, size, _format_name(info.video_codec),
                                info.video_bitrate, decision.why)
        self.db.upsert_file(library.id, file.path, size, mtime, "working", **details)
        folder = scratch / "ShelfSaver" / f"run-{run.id}"
        job = _Job(file, library, size, mtime, info, decision, run, folder,
                   folder / f"output{output_extension(info, decision)}")
        return self._guarded(job, self._convert_and_check)

    # ---- Failure handling shared by every step of a run ----

    def _guarded(self, job: _Job, step: Callable[[_Job], bool]) -> bool:
        """Run pipeline steps so that no failure can leave a run half-handled."""
        finished = True
        try:
            finished = step(job)
        except _Stopped:
            logger.info("Stopped, and will start again: %s", job.file.path)
            self._end_and_requeue(job, STOPPED_NOTE)
        except _RunFailed as exc:
            logger.error("%s: %s\n%s", job.file.path, exc, job.log_tail)
            self._fail_run(job, str(exc))
        except Exception:
            logger.exception("Unexpected error with %s", job.file.path)
            if job.swap_saved:
                self._keep_swapping(job.run.id, job.file.id, "an unexpected error")
            else:
                self._fail_run(job, SOMETHING_WRONG)
        if finished:
            self._set_idle()
        return finished

    def _delete_run_files(self, job: _Job) -> None:
        self._parked = None
        if job.temp is not None:
            self._delete_temp(job.temp)
        self._delete_scratch_folder(job.scratch_folder)

    @staticmethod
    def _delete_temp(path) -> None:
        try:
            os.unlink(ext_path(path))
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning("Could not delete %s: %s", path, exc)

    @staticmethod
    def _delete_scratch_folder(folder) -> None:
        try:
            shutil.rmtree(ext_path(folder))
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning("Could not delete %s: %s", folder, exc)

    def _end_run(self, run_id: int, state: str, error: Optional[str]) -> None:
        self.db.update_run(run_id, state=state, error=error, finished_at=time.time())

    def _fail_run(self, job: _Job, reason: str) -> None:
        self._end_run(job.run.id, "failed", reason)
        self.db.set_file_status(job.file.id, "failed", reason)
        self._delete_run_files(job)

    def _end_and_requeue(self, job: _Job, note: str) -> None:
        self._end_run(job.run.id, "failed", note)
        self.db.requeue([job.file.id])
        self._delete_run_files(job)

    # ---- Steps 3 and 4: convert on the scratch disk and check the result ----

    def _convert_and_check(self, job: _Job) -> bool:
        os.makedirs(ext_path(job.scratch_folder), exist_ok=True)
        source_copy = job.scratch_folder / f"source{job.info.ext}"

        self._begin_step(job, "Copying")
        try:
            _copy_file(job.file.path, source_copy, self._on_progress, self._should_stop)
        except OSError as exc:
            logger.error("Could not copy %s to the scratch disk: %s", job.file.path, exc)
            raise _RunFailed("Couldn't copy this file to the scratch disk") from exc

        self._begin_step(job, "Converting")
        cmd = build_command(job.info, job.decision, job.library.level, source_copy, job.output,
                            self.config.ffmpeg)
        try:
            result = run_ffmpeg(cmd, job.info.duration, self._on_progress, self._should_stop)
        except OSError as exc:
            result = EncodeResult(-1, False, f"FFmpeg could not start: {exc}")
        job.log_tail = result.log_tail
        self.db.update_run(job.run.id, log_tail=result.log_tail)

        if result.returncode != 0:
            if result.stopped or self._should_stop():
                raise _Stopped()
            self._encoder_failed(job, result)
            return True
        with self._lock:
            self._failed_encodes = []

        self._begin_step(job, "Checking")
        try:
            source_info = probe(source_copy, self.config.ffprobe)
        except ProbeError as exc:
            raise _RunFailed("The copy on the scratch disk could not be read") from exc
        verdict = verify_output(source_info, job.output, job.decision, self.config.ffmpeg,
                                self.config.ffprobe, self._should_stop)
        self.db.update_run(job.run.id, checks_json=json.dumps(verdict.checks))
        if not verdict.passed:
            if verdict.reason == "Stopped":
                raise _Stopped()
            if verdict.not_worth_it:
                logger.info("Not worth replacing %s: %s", job.file.path, verdict.reason)
                self._end_run(job.run.id, "not_worth_it", verdict.reason)
                self.db.set_file_status(job.file.id, "skipped", verdict.reason)
                self._delete_run_files(job)
                return True
            raise _RunFailed(verdict.reason)

        now_size = os.stat(ext_path(job.output)).st_size
        try:
            out_info = probe(job.output, self.config.ffprobe)
            now_format, now_bitrate = _format_name(out_info.video_codec), out_info.video_bitrate
        except ProbeError:  # it just passed the checks, so this only costs a history detail
            now_format, now_bitrate = None, None
        self.db.update_run(
            job.run.id, now_size=now_size, now_format=now_format, now_bitrate=now_bitrate,
            tracks_json=_tracks_json(job.info, job.decision),
        )
        return self._place_new_file(job)

    def _encoder_failed(self, job: _Job, result: EncodeResult) -> None:
        logger.error("FFmpeg exited with %s on %s\n%s", result.returncode, job.file.path,
                     result.log_tail)
        self._fail_run(job, CONVERT_FAILED)
        with self._lock:
            self._failed_encodes.append(job.file.id)
            failed = list(self._failed_encodes)
        if len(failed) >= ENCODER_FAILURE_LIMIT:
            logger.error("Conversions keep failing, so pausing")
            self.db.requeue(failed)
            self.pause(BREAKER_MESSAGE)

    # ---- Steps 5 to 8: bring the new file to the library's volume and swap it in ----

    def _library_has_room(self, job: _Job) -> bool:
        needed = os.stat(ext_path(job.output)).st_size + SPACE_RESERVE_BYTES
        try:
            return free_bytes(job.library.path) >= needed
        except OSError as exc:
            logger.warning("Could not check the room on %s: %s", job.library.path, exc)
            return False

    def _place_new_file(self, job: _Job) -> bool:
        if not self._library_has_room(job):
            # Keep the finished conversion: it is the expensive part, and nothing is at risk.
            logger.info("Not enough room on the library drive for %s", job.file.path)
            self._parked = job
            self._set_status("blocked", job.file.path, message=NO_ROOM_LIBRARY)
            return False
        self._parked = None

        self._begin_step(job, "Replacing")
        incoming = work_root(job.library.path) / "incoming"
        job.temp = incoming / f"{job.run.id}{job.output.suffix}"
        try:
            os.makedirs(ext_path(incoming), exist_ok=True)
            _copy_file(job.output, job.temp, self._on_progress, self._should_stop)
            same = (_sha256(job.output, self._should_stop)
                    == _sha256(job.temp, self._should_stop))
        except OSError as exc:
            logger.error("Could not copy the new file for %s: %s", job.file.path, exc)
            raise _RunFailed("Couldn't copy the new file to the library drive") from exc
        if not same:
            raise _RunFailed("The new file was damaged while copying to the library drive")
        if self._should_stop():
            raise _Stopped()

        try:
            unchanged = _matches(job.file.path, job.size, job.mtime)
        except OSError:
            unchanged = False
        if not unchanged:
            logger.info("The original changed during the conversion: %s", job.file.path)
            self._end_and_requeue(job, CHANGED_NOTE)
            return True

        holding = self.db.get_setting("holding_enabled") == "1"
        try:
            plan = make_plan(job.file.path, job.temp, job.output.suffix, job.library.path,
                             job.library.name, holding)
        except (ValueError, OSError) as exc:
            logger.error("Could not plan the replacement of %s: %s", job.file.path, exc)
            raise _RunFailed("Couldn't plan how to replace this file") from exc

        # Written before anything moves, so a crash at any later point can be picked up.
        self.db.update_run(job.run.id, state="swapping", now_path=plan.final,
                           swap_json=_dump_swap(plan, job.size, job.mtime))
        job.swap_saved = True
        self._advance_swap(next(r for r in self.db.active_runs(("swapping",))
                                if r.id == job.run.id))
        return True

    def _advance_due_swaps(self) -> bool:
        """Try every swap that is due. An attempt that can't proceed never holds up other work."""
        now = _now()
        with self._lock:
            asked = set(self._swap_retry_asked)
            self._swap_retry_asked.clear()
            times = dict(self._next_swap_try)
        tried = False
        for run in self.db.active_runs(SWAP_STATES):
            due = run.id in asked or (run.state == "waiting_in_use"
                                      and now >= times.get(run.id, now))
            if not due:
                continue
            tried = True
            try:
                self._advance_swap(run)
            except Exception:
                logger.exception("Unexpected error retrying the swap for run %s", run.id)
        if tried:
            self._set_idle()
        return tried

    def _advance_swap(self, run: Run) -> None:
        """Move a saved swap as far as it will go, and record what happened. Never raises for
        anything a swap can report."""
        try:
            plan, size, mtime = _load_swap(run.swap_json)
        except (ValueError, TypeError, KeyError) as exc:
            self._keep_swapping(run.id, run.file_id, f"a damaged saved plan ({exc})")
            return

        self._set_status("working", plan.original, "Replacing")
        logger.info("Replacing: %s", plan.original)
        try:
            outcome = recover_swap(plan)
            if outcome == "not_started":
                if not _matches(plan.original, size, mtime):
                    self._original_changed(run, plan)
                    return
                execute_swap(plan)
                outcome = "finished"
        except FileInUse:
            self._wait_for_file(run, plan)
        except TargetExists as exc:
            self._target_taken(run, plan, size, mtime, exc)
        except (UnexpectedSwapState, OSError) as exc:
            self._keep_swapping(run.id, run.file_id, str(exc))
        else:
            if outcome == "finished":
                self._record_done(run, plan)
            else:
                self._keep_swapping(run.id, run.file_id, "files on disk that match no swap step")

    def _wait_for_file(self, run: Run, plan: SwapPlan) -> None:
        logger.info("In use, will try again later: %s", plan.original)
        with self._lock:
            self._next_swap_try[run.id] = _now() + IN_USE_RETRY_SECONDS
        self.db.update_run(run.id, state="waiting_in_use", step="Replacing")
        self.db.set_file_status(run.file_id, "waiting_in_use", WAITING_IN_USE)
        # The verified copy is on the library's volume now, so free the scratch disk.
        self._delete_run_scratch(run)

    def _keep_swapping(self, run_id: int, file_id: int, why: str) -> None:
        """The temp file may be the only copy of the new file, so nothing is cleaned up."""
        logger.error("Keeping run %s for a later attempt, because of %s", run_id, why)
        self.db.update_run(run_id, state="swapping", error=SWAP_KEPT)
        self.db.set_file_status(file_id, "failed", SWAP_KEPT)

    def _target_taken(self, run: Run, plan: SwapPlan, size: int, mtime: float, exc) -> None:
        try:
            intact = _matches(plan.original, size, mtime)
        except OSError:
            intact = False
        if not intact:
            self._keep_swapping(run.id, run.file_id, f"a blocked swap ({exc})")
            return
        logger.error("Something is in the way of the swap for %s: %s", plan.original, exc)
        self._end_run(run.id, "failed", TARGET_TAKEN)
        self.db.set_file_status(run.file_id, "failed", TARGET_TAKEN)
        self._clean_up_swap(run, plan)

    def _original_changed(self, run: Run, plan: SwapPlan) -> None:
        logger.info("The original changed before it could be replaced: %s", plan.original)
        self._end_run(run.id, "failed", CHANGED_NOTE)
        self.db.requeue([run.file_id])
        self._clean_up_swap(run, plan)

    def _clean_up_swap(self, run: Run, plan: SwapPlan) -> None:
        self._delete_temp(plan.temp)
        self._delete_run_scratch(run)
        self._forget_swap(run.id)

    def _forget_swap(self, run_id: int) -> None:
        with self._lock:
            self._next_swap_try.pop(run_id, None)
            self._swap_retry_asked.discard(run_id)

    def _delete_run_scratch(self, run: Run) -> None:
        setting = self.db.get_setting("scratch_dir")
        if setting:
            self._delete_scratch_folder(Path(setting) / "ShelfSaver" / f"run-{run.id}")

    # ---- Step 9: record the outcome ----

    def _record_done(self, run: Run, plan: SwapPlan) -> None:
        now_size = run.now_size if run.now_size is not None else plan.expected_size
        saved = (run.was_size or 0) - now_size
        # The row goes first: a crash before the run is marked just repeats this harmlessly.
        self._update_row_after_swap(run, plan, now_size)
        self.db.update_run(run.id, state="done", step="Done", finished_at=time.time(),
                           error=None, now_path=plan.final, now_size=now_size,
                           saved_bytes=saved)
        self._delete_run_scratch(run)
        self._forget_swap(run.id)
        logger.info("Finished %s, saving %s bytes", plan.final, saved)

    def _update_row_after_swap(self, run: Run, plan: SwapPlan, now_size: int) -> None:
        """Store the new file's size and time, so a later scan knows it needs no converting."""
        file = self.db.get_file(run.file_id)
        if file is None:
            return
        try:
            info = os.stat(ext_path(plan.final))
            size, mtime = info.st_size, info.st_mtime
        except OSError as exc:
            logger.warning("Could not read the finished file %s: %s", plan.final, exc)
            size, mtime = now_size, file.mtime
        try:
            details = _file_details(probe(ext_path(plan.final), self.config.ffprobe))
        except ProbeError as exc:
            logger.warning("Could not inspect the finished file %s: %s", plan.final, exc)
            details = {"codec": file.codec, "container": Path(plan.final).suffix.lstrip("."),
                       "resolution": file.resolution}
        self.db.record_converted(file.id, plan.final, size, mtime, **details)

    # ---- Startup ----

    def recover(self) -> None:
        """Pick up after a crash or restart. One bad run or folder can't stop the rest."""
        self._parked = None
        for run in self.db.active_runs(SWAP_STATES):
            try:
                self._advance_swap(run)
            except Exception:
                logger.exception("Could not recover run %s", run.id)

        for run in self.db.active_runs(("running",)):
            self._end_run(run.id, "failed", INTERRUPTED_NOTE)
        still_swapping = {r.file_id for r in self.db.active_runs(SWAP_STATES)}
        working = [f.id for f in self.db.files_with_status("working")
                   if f.id not in still_swapping]
        if working:
            logger.info("Starting %s interrupted files again", len(working))
            self.db.requeue(working)

        self._delete_leftover_run_folders()
        self._delete_orphan_incoming_files()
        self._set_idle()

    def _delete_leftover_run_folders(self) -> None:
        setting = self.db.get_setting("scratch_dir")
        if not setting:
            return
        folder = Path(setting) / "ShelfSaver"
        try:
            if _may_be_a_link(folder):
                logger.warning("Refusing to clean %s because it may link elsewhere", folder)
                return
            entries = _list_folder(folder)
        except FileNotFoundError:
            return
        except OSError as exc:
            logger.warning("Could not read %s: %s", folder, exc)
            return
        for entry in entries:
            if not fnmatch.fnmatch(entry.name, "run-*"):
                continue
            try:
                # A link never counts as a run folder, so what it points at is never touched.
                if not _may_be_a_link(entry.path) and entry.is_dir(follow_symlinks=False):
                    self._delete_scratch_folder(entry.path)
            except OSError as exc:
                logger.warning("Could not clean %s: %s", entry.path, exc)

    def _delete_orphan_incoming_files(self) -> None:
        active = {str(r.id) for r in self.db.active_runs(("running", *SWAP_STATES))}
        folders = {os.path.normcase(str(work_root(lib.path) / "incoming")):
                   work_root(lib.path) / "incoming" for lib in self.db.list_libraries()}
        for folder in folders.values():
            try:
                if _may_be_a_link(folder):
                    logger.warning("Refusing to clean %s because it may link elsewhere", folder)
                    continue
                entries = _list_folder(folder)
            except FileNotFoundError:
                continue
            except OSError as exc:
                logger.warning("Could not read %s: %s", folder, exc)
                continue
            for entry in entries:
                try:
                    if (entry.is_file(follow_symlinks=False)
                            and Path(entry.name).stem not in active):
                        logger.info("Removing a leftover file: %s", entry.path)
                        self._delete_temp(entry.path)
                except OSError as exc:
                    logger.warning("Could not clean %s: %s", entry.path, exc)
