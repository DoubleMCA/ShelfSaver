"""Check a converted file before it is allowed to replace the original."""
from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

from .models import Decision, MediaInfo, VerifyResult
from .probe import ProbeError, ScanStopped, probe, scan_streams
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
HDR_TRANSFERS = ("smpte2084", "arib-std-b67")


def _language(stream) -> str:
    return (stream.language or "und").strip().lower() or "und"


def _transfer(value):
    return None if value in (None, "", "unknown") else value


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
    if _transfer(source.color_transfer) != _transfer(output.color_transfer):
        return False
    if source.color_transfer in HDR_TRANSFERS:
        if source.color_primaries != output.color_primaries:
            return False
        if not output.bit_depth or output.bit_depth <= 8:
            return False
    return True


def _main_video(info: MediaInfo):
    return next((s for s in info.streams if s.kind == "video" and not s.is_cover_art), None)


def _kept_pairs(source: MediaInfo, output: MediaInfo, decision: Decision):
    """(source index, output index) for the main video and each kept audio stream."""
    removed = set(decision.remove_audio)
    sv, ov = _main_video(source), _main_video(output)
    if sv is None or ov is None:
        return None
    src_audio = [s.index for s in source.streams if s.kind == "audio"
                 and not s.is_cover_art and s.index not in removed]
    out_audio = [s.index for s in output.streams if s.kind == "audio" and not s.is_cover_art]
    if len(src_audio) != len(out_audio):
        return None
    return [(sv.index, ov.index)] + list(zip(src_audio, out_audio))


def _stream_lengths_ok(source: MediaInfo, output: MediaInfo, out_path, decision,
                       ffprobe, should_stop):
    """True/False, or None when stopped.

    Each kept stream must start and end where its source stream does (both measured
    from the earliest packet of the kept streams, so a source that starts at a
    non-zero time is fine) and must not have a bigger hole between packets.
    """
    pairs = _kept_pairs(source, output, decision)
    if pairs is None:
        return False
    try:
        src = scan_streams(source.path, ffprobe, should_stop, strict=False)
        out = scan_streams(out_path, ffprobe, should_stop, strict=True)
    except ScanStopped:
        return None
    except ProbeError:
        return False
    if any(s not in src or o not in out for s, o in pairs):
        return False
    allowed = max(1.0, 0.002 * source.duration)
    src_origin = min(src[s].start for s, _ in pairs)
    out_origin = min(out[o].start for _, o in pairs)
    for s_idx, o_idx in pairs:
        s, o = src[s_idx], out[o_idx]
        if abs((s.start - src_origin) - (o.start - out_origin)) > allowed:
            return False
        if abs((s.end - src_origin) - (o.end - out_origin)) > allowed:
            return False
        if o.max_gap > s.max_gap + allowed:
            return False
    return True


def _smaller_ok(source: MediaInfo, output: MediaInfo) -> bool:
    return output.size <= source.size * (1 - MIN_SAVINGS)


def _plays(output: Path, video_index: int, ffmpeg, should_stop: Callable[[], bool]):
    """Decode the whole file. Returns True, False, or None when stopped."""
    cmd = [str(ffmpeg), "-v", "error", "-i", str(output),
           "-map", f"0:{video_index}", "-map", "0:a?", "-f", "null", "-"]
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
        while True:
            if should_stop():
                stopped = True
                break
            if proc.poll() is not None:
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

    checks["tracks"] = _tracks_ok(source, out, decision)
    checks["smaller"] = _smaller_ok(source, out)

    # Container duration is unreliable (a source that starts at a non-zero time reports
    # a longer duration), so length is judged from per-stream packet timing. It runs
    # even when the file is too big, so a truncated file is never excused as "not worth it".
    if checks["tracks"]:
        lengths = _stream_lengths_ok(source, out, output, decision, ffprobe, should_stop)
        if lengths is None:
            return VerifyResult(False, checks, "Stopped")
        checks["length"] = lengths

    for name in ("tracks", "length"):
        if not checks[name]:
            return VerifyResult(False, checks, REASONS[name])
    if not checks["smaller"]:
        return VerifyResult(False, checks, REASONS["smaller"], not_worth_it=True)

    video = _main_video(out)
    result = _plays(Path(output), video.index, ffmpeg, should_stop)
    if result is None:
        return VerifyResult(False, checks, "Stopped")
    checks["plays"] = result
    if not result:
        return VerifyResult(False, checks, REASONS["plays"])
    return VerifyResult(True, checks, "")
