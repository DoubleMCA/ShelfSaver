"""Check a converted file before it is allowed to replace the original."""
from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

from .models import Decision, MediaInfo, VerifyResult
from .probe import ProbeError, probe
from .quality import MIN_SAVINGS

CHECKS = ("length", "tracks", "smaller", "plays")
STOP_POLL_SECONDS = 0.2
STDERR_KEEP_BYTES = 64 * 1024

REASONS = {
    "length": "New file is a different length",
    "tracks": "New file is missing expected tracks",
    "smaller": "Would not save enough",
    "plays": "New file failed the playback check",
}


def _language(stream) -> str:
    return (stream.language or "und").strip().lower() or "und"


def _transfer(value):
    return None if value in (None, "", "unknown") else value


def _length_ok(source: MediaInfo, output: MediaInfo) -> bool:
    allowed = max(1.0, 0.002 * source.duration)
    return abs(source.duration - output.duration) <= allowed


def _tracks_ok(source: MediaInfo, output: MediaInfo, decision: Decision) -> bool:
    removed = set(decision.remove_audio)
    for kind in ("audio", "subtitle"):
        expected = [
            _language(s) for s in source.streams
            if s.kind == kind and not s.is_cover_art
            and not (kind == "audio" and s.index in removed)
        ]
        actual = [
            _language(s) for s in output.streams
            if s.kind == kind and not s.is_cover_art
        ]
        if expected != actual:
            return False
    if decision.action == "convert":
        if output.video_codec != "hevc":
            return False
    elif output.video_codec != source.video_codec:
        return False
    return _transfer(source.color_transfer) == _transfer(output.color_transfer)


def _smaller_ok(source: MediaInfo, output: MediaInfo) -> bool:
    return output.size <= source.size * (1 - MIN_SAVINGS)


def _plays(output: Path, ffmpeg, should_stop: Callable[[], bool]):
    """Decode the whole file. Returns True, False, or None when stopped."""
    cmd = [str(ffmpeg), "-v", "error", "-i", str(output),
           "-map", "0:v:0", "-map", "0:a?", "-f", "null", "-"]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE, creationflags=flags)
    except OSError:
        return False
    seen = bytearray()

    def drain():
        # Always keep reading so a chatty decode can never fill the pipe and block.
        for chunk in iter(lambda: proc.stderr.read(4096), b""):
            if len(seen) < STDERR_KEEP_BYTES:
                seen.extend(chunk)

    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    stopped = False
    try:
        while proc.poll() is None:
            if should_stop():
                stopped = True
                break
            time.sleep(STOP_POLL_SECONDS)
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        proc.wait()
        reader.join(timeout=5)
        proc.stderr.close()
    if stopped:
        return None
    return proc.returncode == 0 and not seen.strip()


def verify_output(source: MediaInfo, output, decision: Decision, ffmpeg, ffprobe,
                  should_stop: Callable[[], bool] = lambda: False) -> VerifyResult:
    checks = {name: False for name in CHECKS}
    try:
        out = probe(output, ffprobe)
    except ProbeError:
        return VerifyResult(False, checks, "New file could not be read")

    checks["length"] = _length_ok(source, out)
    checks["tracks"] = _tracks_ok(source, out, decision)
    checks["smaller"] = _smaller_ok(source, out)

    cheap_failed = [n for n in CHECKS[:3] if not checks[n]]
    if cheap_failed:
        if cheap_failed == ["smaller"]:
            return VerifyResult(False, checks, REASONS["smaller"], not_worth_it=True)
        return VerifyResult(False, checks, REASONS[cheap_failed[0]])

    result = _plays(Path(output), ffmpeg, should_stop)
    if result is None:
        return VerifyResult(False, checks, "Stopped")
    checks["plays"] = result
    if not result:
        return VerifyResult(False, checks, REASONS["plays"])
    return VerifyResult(True, checks, "")
