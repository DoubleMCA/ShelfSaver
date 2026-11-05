"""Decide what to do with a probed file."""
from __future__ import annotations

import re

from .models import Decision, MediaInfo
from .quality import LEVELS, resolution_bucket

_ENGLISH = {"eng", "en", "english"}
_DUB = re.compile(r"\bdub(bed)?\b", re.IGNORECASE)

_EFFICIENT_WHY = "Already stored in a modern, efficient format"


def _is_english(lang) -> bool:
    # Only the main part of the tag counts, so en-US and en_GB are English but enm is not.
    return bool(lang) and lang.strip().lower().replace("_", "-").split("-")[0] in _ENGLISH


def audio_to_remove(streams) -> tuple:
    """Stream indexes of non-English audio that are safe to drop; () when unsure."""
    audio = [s for s in streams if s.kind == "audio"]
    if len(audio) < 2:
        return ()
    if any(not s.language or s.language.strip().lower() == "und" for s in audio):
        return ()
    if any(s.title and _DUB.search(s.title) for s in audio):
        return ()
    if not _is_english(audio[0].language):
        return ()
    if any(s.is_default and not _is_english(s.language) for s in audio):
        return ()
    return tuple(s.index for s in audio if not _is_english(s.language))


def _skip(reason: str, why: str) -> Decision:
    return Decision("skip", reason, (), why)


def decide(info: MediaInfo, level: str, english_only: bool,
           nlink: int = 1, read_only: bool = False) -> Decision:
    if nlink > 1:
        return _skip("Linked to another copy",
                     "Another file shares this one's data, so changing it would affect both")
    if read_only:
        return _skip("Read-only file", "The file is marked read-only")
    if not info.video_codec:
        return _skip("No video", "The file has no video track")
    if info.dolby_vision:
        return _skip("Dolby Vision, left as is",
                     "Converting would lose the Dolby Vision picture")

    removal = audio_to_remove(info.streams) if english_only else ()

    skip = None
    if info.video_codec == "hevc":
        skip = ("Already x265", _EFFICIENT_WHY)
    elif info.video_codec in ("av1", "vp9"):
        skip = ("Already an efficient format", _EFFICIENT_WHY)
    else:
        mbps = LEVELS[level].min_mbps[resolution_bucket(info.width)]
        if info.video_bitrate is not None and info.video_bitrate < mbps * 1_000_000:
            skip = ("Already small", "Already uses little data for its resolution")

    if skip:
        if removal:
            return Decision("strip_only", skip[0], removal,
                            "Only the extra-language audio tracks can be removed")
        return _skip(*skip)
    return Decision("convert", "Worth converting", removal,
                    "Uses far more data than it needs at this resolution")
