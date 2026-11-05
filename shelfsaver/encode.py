"""Build and run the ffmpeg conversion command."""
from __future__ import annotations

import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .models import Decision, MediaInfo, Stream
from .quality import LEVELS

LOG_TAIL_BYTES = 8 * 1024
KILL_AFTER_SECONDS = 10

MP4_EXTENSIONS = (".mp4", ".m4v")
MP4_AUDIO = frozenset({"aac", "ac3", "eac3", "mp3", "opus", "flac", "alac"})
MP4_SUBTITLES = frozenset({"mov_text"})


@dataclass
class EncodeResult:
    returncode: int
    stopped: bool
    log_tail: str


def _mapped_streams(info: MediaInfo, decision: Decision) -> list:
    """Streams that go into the output, in source order."""
    removed = set(decision.remove_audio)
    return [
        s for s in info.streams
        if s.kind != "data" and not (s.kind == "audio" and s.index in removed)
    ]


def output_extension(info: MediaInfo, decision: Decision) -> str:
    ext = (info.ext or "").lower()
    if ext == ".mkv":
        return ".mkv"
    if ext in MP4_EXTENSIONS:
        for s in _mapped_streams(info, decision):
            if s.kind == "subtitle" and s.codec not in MP4_SUBTITLES:
                return ".mkv"
            if s.kind == "audio" and s.codec not in MP4_AUDIO:
                return ".mkv"
        return ext
    return ".mkv"


def _is_main_video(stream: Stream) -> bool:
    return stream.kind == "video" and not stream.is_cover_art


def build_command(info: MediaInfo, decision: Decision, level: str,
                  source: Path, output: Path, ffmpeg: str) -> list:
    source, output = Path(source), Path(output)
    if source == output:
        raise ValueError("Output must not be the source file")
    out_ext = output.suffix.lower()
    to_mp4 = out_ext in MP4_EXTENSIONS
    converting = decision.action == "convert"
    streams = _mapped_streams(info, decision)

    cmd = [str(ffmpeg), "-y", "-v", "warning", "-i", str(source)]
    for s in streams:
        cmd += ["-map", f"0:{s.index}"]

    # Output index of each mapped stream within its own kind.
    counters: dict = {}
    main_video_out = None if streams else 0
    subs_to_srt = []
    other_video = []
    for s in streams:
        n = counters.get(s.kind, 0)
        counters[s.kind] = n + 1
        if main_video_out is None and _is_main_video(s):
            main_video_out = n
        elif s.kind == "video":
            other_video.append(n)
        if s.kind == "subtitle" and s.codec == "mov_text" and not to_mp4:
            subs_to_srt.append(n)

    encoding = converting and main_video_out is not None
    if encoding:
        for n in other_video:
            cmd += [f"-c:v:{n}", "copy"]
    else:
        cmd += ["-c:v", "copy"]
    cmd += ["-c:a", "copy", "-c:s", "copy", "-c:t", "copy"]
    for n in subs_to_srt:
        cmd += [f"-c:s:{n}", "srt"]

    if encoding:
        v = main_video_out
        deep = (info.bit_depth or 8) > 8
        cmd += [
            f"-c:v:{v}", "hevc_nvenc", "-preset", "p7", "-tune", "hq",
            "-rc", "vbr", "-cq", str(LEVELS[level].cq), "-b:v", "0",
            "-spatial-aq", "1", "-temporal-aq", "1", "-rc-lookahead", "32",
        ]
        if deep:
            cmd += [f"-pix_fmt:v:{v}", "p010le", f"-profile:v:{v}", "main10"]
        else:
            cmd += [f"-pix_fmt:v:{v}", "yuv420p", f"-profile:v:{v}", "main"]
        for flag, value in (("-color_primaries", info.color_primaries),
                            ("-color_trc", info.color_transfer),
                            ("-colorspace", info.colorspace)):
            if value and value != "unknown":
                cmd += [f"{flag}:v:{v}", value]
        if info.field_order not in (None, "", "progressive", "unknown"):
            cmd += [f"-filter:v:{v}", "bwdif"]
        if to_mp4:
            cmd += [f"-tag:v:{v}", "hvc1"]

    cmd += ["-map_metadata", "0", "-map_chapters", "0",
            "-max_muxing_queue_size", "4096", str(output)]
    return cmd


def run_ffmpeg(cmd: list, duration: float, on_progress: Callable[[float], None],
               should_stop: Callable[[], bool]) -> EncodeResult:
    full = list(cmd[:1]) + ["-progress", "pipe:1", "-nostats"] + list(cmd[1:])
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.Popen(full, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            stdin=subprocess.DEVNULL, creationflags=flags)
    tail = bytearray()
    lock = threading.Lock()

    def drain_stderr():
        for chunk in iter(lambda: proc.stderr.read(4096), b""):
            with lock:
                tail.extend(chunk)
                del tail[:-LOG_TAIL_BYTES]

    reader = threading.Thread(target=drain_stderr, daemon=True)
    reader.start()

    stopped = False
    try:
        for raw in proc.stdout:
            line = raw.decode("utf-8", "replace").strip()
            if line.startswith("out_time_us="):
                try:
                    us = int(line.split("=", 1)[1])
                except ValueError:
                    us = -1
                if us >= 0 and duration > 0:
                    on_progress(min(1.0, us / 1_000_000 / duration))
            if should_stop():
                stopped = True
                break
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=KILL_AFTER_SECONDS)
            except subprocess.TimeoutExpired:
                proc.kill()
        proc.wait()
        reader.join(timeout=5)
        proc.stdout.close()
        proc.stderr.close()
    with lock:
        text = bytes(tail).decode("utf-8", "replace")
    return EncodeResult(proc.returncode, stopped, text)
