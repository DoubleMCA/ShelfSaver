"""Predict how much converting an H.265 file would save, from a few short sections of it."""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable, Optional

from .encode import build_command, run_ffmpeg
from .encoders import DEFAULT_ENCODER, Encoder
from .jobobject import kill_with_app
from .models import Decision, MediaInfo
from .probe import ProbeError, probe

SECTION_SECONDS = 20.0
# Near the start, middle and end, clear of opening logos and end credits.
_POSITIONS = (0.15, 0.5, 0.85)
_CUT_TIMEOUT_SECONDS = 600


class SampleError(Exception):
    """A section could not be cut or converted, so there is no prediction."""


def section_starts(duration: float) -> list[tuple[float, float]]:
    """(start, seconds) of each section to try."""
    if duration < SECTION_SECONDS:
        return [(0.0, duration)]
    if duration < len(_POSITIONS) * SECTION_SECONDS * 2:
        return [((duration - SECTION_SECONDS) / 2, SECTION_SECONDS)]
    return [(max(0.0, duration * p - SECTION_SECONDS / 2), SECTION_SECONDS) for p in _POSITIONS]


def _cut(source, start: float, seconds: float, video_index: int, dest: Path, ffmpeg) -> None:
    """Stream-copy the main video of one section, untouched."""
    cmd = [str(ffmpeg), "-y", "-v", "error", "-ss", str(start), "-i", str(source),
           "-t", str(seconds), "-map", f"0:{video_index}", "-c", "copy", str(dest)]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE, creationflags=flags)
    except OSError as exc:
        raise SampleError(f"FFmpeg could not start: {exc}") from exc
    kill_with_app(proc)
    try:
        _, said = proc.communicate(timeout=_CUT_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        proc.kill()
        proc.communicate()
        raise SampleError("Cutting a section took too long") from exc
    if proc.returncode != 0 or not dest.is_file() or dest.stat().st_size == 0:
        raise SampleError("Could not cut a section: "
                          + said.decode("utf-8", "replace").strip()[-400:])


def _sizes(info: MediaInfo, level: str, source, folder: Path, ffmpeg, ffprobe,
           should_stop: Callable[[], bool], encoder: Encoder) -> Optional[tuple[int, int]]:
    video = next((s for s in info.streams if s.kind == "video" and not s.is_cover_art), None)
    if video is None:
        raise SampleError("The file has no main video track")
    copied = converted = 0
    for n, (start, seconds) in enumerate(section_starts(info.duration)):
        if should_stop():
            return None
        clip, out = folder / f"sample-{n}.mkv", folder / f"sample-{n}-new.mkv"
        _cut(source, start, seconds, video.index, clip, ffmpeg)
        try:
            clip_info = probe(clip, ffprobe)
        except ProbeError as exc:
            raise SampleError("A cut section could not be read") from exc
        # The sample must use the same encoder as the real conversion, or the predicted saving
        # would describe a different encoder's output.
        cmd = build_command(clip_info, Decision("convert", "Sample", ()), level, clip, out, ffmpeg,
                            encoder)
        result = run_ffmpeg(cmd, clip_info.duration, lambda fraction: None, should_stop)
        if result.stopped:
            return None
        if result.returncode != 0 or not out.is_file():
            raise SampleError("FFmpeg could not convert a section:\n" + result.log_tail[-400:])
        copied += clip.stat().st_size
        converted += out.stat().st_size
    return copied, converted


def predict_saving(info: MediaInfo, level: str, source, folder, ffmpeg, ffprobe,
                   should_stop: Callable[[], bool],
                   encoder: Encoder = DEFAULT_ENCODER) -> Optional[float]:
    """The share of the whole file converting would save, or None when stopped.

    Only the video is measured, so removed soundtracks never make a file look worth converting.
    """
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    try:
        sizes = _sizes(info, level, source, folder, ffmpeg, ffprobe, should_stop, encoder)
    finally:
        for leftover in folder.glob("sample-*"):
            leftover.unlink(missing_ok=True)
    if sizes is None:
        return None
    copied, converted = sizes
    if copied == 0 or not info.size:
        raise SampleError("The sections came out empty")
    video_bytes = min(info.size, (info.video_bitrate or 0) * info.duration / 8)
    return (1 - converted / copied) * video_bytes / info.size
