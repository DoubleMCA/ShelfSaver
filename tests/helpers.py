"""Test helpers shared across test files: generated media and MediaInfo builders."""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from shelfsaver.models import Decision, MediaInfo, Stream

_cache_dir = None
_cache: dict = {}


def cache_dir() -> Path:
    global _cache_dir
    if _cache_dir is None:
        _cache_dir = Path(tempfile.mkdtemp(prefix="shelfsaver-media-"))
    return _cache_dir


def clear_cache() -> None:
    global _cache_dir
    if _cache_dir is not None:
        shutil.rmtree(_cache_dir, ignore_errors=True)
    _cache_dir = None
    _cache.clear()


def _run(args):
    subprocess.run(args, check=True, capture_output=True, timeout=300)


def make_video(path, seconds=3, size="1920x1080", vcodec="libx264", crf=10,
               audio=(("eng", "English"),), subs=(), interlaced=False,
               hdr=False, cover_art=False):
    """Generate a small test video at `path` (generated once per session per parameter set).

    audio: (language, title) pairs; subs: (language, title) pairs.
    """
    path = Path(path)
    audio, subs = tuple(map(tuple, audio)), tuple(map(tuple, subs))
    key = hashlib.sha1(json.dumps(
        [path.suffix, seconds, size, vcodec, crf, audio, subs, interlaced, hdr, cover_art]
    ).encode()).hexdigest()
    cached = _cache.get(key)
    if cached is None:
        cached = cache_dir() / f"{key}{path.suffix}"
        _build(cached, seconds, size, vcodec, crf, audio, subs, interlaced, hdr, cover_art)
        _cache[key] = cached
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(cached, path)
    return path


def _build(out, seconds, size, vcodec, crf, audio, subs, interlaced, hdr, cover_art):
    work = cache_dir()
    args = ["ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", f"testsrc2=size={size}:rate=25:duration={seconds}"]
    n_in = 1
    for _ in audio:
        args += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}"]
    audio_first = 1
    sub_first = n_in = 1 + len(audio)
    for i, _ in enumerate(subs):
        srt = work / f"sub{i}-{out.stem}.srt"
        srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8")
        args += ["-i", str(srt)]
        n_in += 1
    cover = None
    if cover_art:
        cover = work / "cover.jpg"
        if not cover.exists():
            _run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                  "testsrc2=size=64x64:rate=1:duration=1", "-frames:v", "1", str(cover)])
        if out.suffix == ".mp4":
            args += ["-i", str(cover)]
            cover_in = n_in
            n_in += 1

    args += ["-map", "0:v"]
    for i in range(len(audio)):
        args += ["-map", f"{audio_first + i}:a"]
    for i in range(len(subs)):
        args += ["-map", f"{sub_first + i}:s"]
    if cover and out.suffix == ".mp4":
        args += ["-map", f"{cover_in}:v"]

    args += ["-c:v:0", vcodec]
    if vcodec in ("libx264", "libx265"):
        args += ["-crf", str(crf)]
    vf = []
    if interlaced:
        vf.append("interlace=scan=tff")
        if vcodec == "libx264":
            args += ["-flags", "+ilme+ildct"]
    if vf:
        args += ["-filter:v:0", ",".join(vf)]
    if hdr:
        args += ["-pix_fmt", "yuv420p10le"]
        if vcodec == "libx265":
            # Color tags must go in the bitstream; ffmpeg's own flags don't stick for x265.
            args += ["-x265-params",
                     "colorprim=bt2020:transfer=smpte2084:colormatrix=bt2020nc:"
                     "master-display=G(13250,34500)B(7500,3000)R(34000,16000)"
                     "WP(15635,16450)L(10000000,1):max-cll=1000,400:log-level=error"]
        else:
            args += ["-color_primaries", "bt2020", "-color_trc", "smpte2084",
                     "-colorspace", "bt2020nc"]
    else:
        args += ["-pix_fmt", "yuv420p"]
    args += ["-c:a", "aac", "-b:a", "96k"]
    for i, (lang, title) in enumerate(audio):
        args += [f"-metadata:s:a:{i}", f"language={lang}"]
        if title:
            args += [f"-metadata:s:a:{i}", f"title={title}"]
    if subs:
        args += ["-c:s", "mov_text" if out.suffix == ".mp4" else "srt"]
        for i, (lang, title) in enumerate(subs):
            args += [f"-metadata:s:s:{i}", f"language={lang}"]
            if title:
                args += [f"-metadata:s:s:{i}", f"title={title}"]
    if cover and out.suffix == ".mp4":
        args += ["-c:v:1", "copy", "-disposition:v:1", "attached_pic"]
    elif cover:
        args += ["-attach", str(cover), "-metadata:s:t", "mimetype=image/jpeg"]
    args.append(str(out))
    _run(args)


def info(**overrides) -> MediaInfo:
    """A plausible 1080p H.264 MediaInfo; override any field."""
    fields = dict(
        path="movie.mkv", size=2_000_000_000, duration=7200.0, ext=".mkv",
        video_codec="h264", width=1920, height=1080, video_bitrate=30_000_000,
        bit_depth=8, color_transfer=None, color_primaries=None, colorspace=None,
        field_order="progressive", dolby_vision=False, streams=(),
    )
    fields.update(overrides)
    return MediaInfo(**fields)


def audio(*tracks) -> tuple:
    """Streams: a video at index 0, then audio tracks of (language, title, is_default)."""
    streams = [Stream(0, "video", "h264", None, None, True, False)]
    for i, (lang, title, default) in enumerate(tracks, start=1):
        streams.append(Stream(i, "audio", "aac", lang, title, default, False))
    return tuple(streams)


def eng_spa() -> tuple:
    return audio(("eng", None, True), ("spa", None, False))


def convert(**overrides) -> Decision:
    fields = dict(action="convert", reason="Worth converting", remove_audio=(), why="")
    fields.update(overrides)
    return Decision(**fields)


def strip(**overrides) -> Decision:
    fields = dict(action="strip_only", reason="Remove extra audio", remove_audio=(), why="")
    fields.update(overrides)
    return Decision(**fields)
