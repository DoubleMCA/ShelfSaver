r"""Cut a short section of a video and convert it at every quality level, to compare on a TV.

Writes "<name> Original.mkv", "<name> Low.mkv", "<name> Medium.mkv" and "<name> High.mkv"
into --out, then prints how big each one is. The source file is only ever read.

    python scripts/make_compare_clips.py --source "D:\Media\Movies\Example.mkv" ^
        --start 600 --seconds 30 --out "D:\Clips"
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from shelfsaver.config import load_config
from shelfsaver.encode import build_command, run_ffmpeg
from shelfsaver.decide import decide
from shelfsaver.models import Decision
from shelfsaver.probe import ProbeError, probe
from shelfsaver.quality import LEVELS


def clip_name(name: str, level: str) -> str:
    return f"{name} {level.capitalize()}.mkv"


def comparison_decision(info) -> Decision:
    """Comparison is the point, so convert even if the app would normally leave this file alone."""
    natural = decide(info, "medium", english_only=False)
    if natural.action == "convert":
        return natural
    return Decision("convert", "Comparison clip", (), "Forced for comparison")


def _is_inside(path: Path, folder: Path) -> bool:
    return path == folder or folder in path.parents


def _cut(source: Path, start: float, seconds: float, dest: Path, ffmpeg: str,
         ffprobe: str) -> None:
    """Stream-copy a section, so the Original clip is the source's own picture and sound.

    MP4 text subtitles (mov_text) can't be copied into MKV, so only in the clip they become srt.
    """
    subtitles = [s for s in probe(source, ffprobe).streams if s.kind == "subtitle"]
    cmd = [str(ffmpeg), "-y", "-v", "warning", "-ss", str(start), "-i", str(source),
           "-t", str(seconds), "-map", "0", "-map", "-0:d", "-c", "copy"]
    for n, sub in enumerate(subtitles):
        if sub.codec == "mov_text":
            cmd += [f"-c:s:{n}", "srt"]
    cmd.append(str(dest))
    proc = subprocess.run(cmd, capture_output=True, stdin=subprocess.DEVNULL, timeout=600)
    if proc.returncode != 0 or not dest.is_file() or dest.stat().st_size == 0:
        raise RuntimeError("Could not cut a clip from the source: "
                           + proc.stderr.decode("utf-8", "replace")[-400:])


def _print_table(rows: list) -> None:
    original = rows[0][1]
    print()
    print(f"{'Clip':<10} {'Size (MB)':>10} {'Saved':>8}")
    for label, size in rows:
        if size is None:
            print(f"{label:<10} {'failed':>10} {'':>8}")
            continue
        saved = "" if label == "Original" else f"{(1 - size / original) * 100:.0f}%"
        print(f"{label:<10} {size / 1_000_000:>10.1f} {saved:>8}")


def main(argv=None) -> int:
    config = load_config()
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--source", required=True, type=Path)
    ap.add_argument("--start", required=True, type=float, help="seconds into the video")
    ap.add_argument("--seconds", type=float, default=30)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--ffmpeg", default=config.ffmpeg)
    ap.add_argument("--ffprobe", default=config.ffprobe)
    args = ap.parse_args(argv)

    source = args.source.resolve()
    out = args.out.resolve()
    if not source.is_file():
        print(f"Can't find the source file: {source}", file=sys.stderr)
        return 2
    if args.start < 0 or args.seconds <= 0:
        print("Start must be 0 or more and seconds must be more than 0.", file=sys.stderr)
        return 2
    if _is_inside(out, source.parent):
        print("The clips folder must not be inside the folder the source video is in. "
              "Pick a different folder.", file=sys.stderr)
        return 2

    name = source.stem
    out.mkdir(parents=True, exist_ok=True)
    original = out / clip_name(name, "original")
    if original.resolve() == source:
        print("The clip would overwrite the source. Pick a different folder.", file=sys.stderr)
        return 2

    try:
        _cut(source, args.start, args.seconds, original, args.ffmpeg, args.ffprobe)
        info = probe(original, args.ffprobe)
    except (RuntimeError, ProbeError, OSError, subprocess.SubprocessError) as exc:
        original.unlink(missing_ok=True)
        print(exc, file=sys.stderr)
        return 1

    decision = comparison_decision(info)

    rows = [("Original", original.stat().st_size)]
    failed = False
    for level in LEVELS:
        dest = out / clip_name(name, level)
        cmd = build_command(info, decision, level, original, dest, args.ffmpeg)
        result = run_ffmpeg(cmd, info.duration, lambda f: None, lambda: False)
        if result.returncode != 0 or not dest.is_file():
            dest.unlink(missing_ok=True)
            print(f"{level.capitalize()} failed: {result.log_tail[-400:]}", file=sys.stderr)
            rows.append((level.capitalize(), None))
            failed = True
        else:
            rows.append((level.capitalize(), dest.stat().st_size))
    _print_table(rows)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
