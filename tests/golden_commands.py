"""The ffmpeg commands ShelfSaver builds, over a wide matrix of inputs, for regression checking.

`tests/golden_nvenc_commands.json` holds what the NVENC command looked like *before* encoders
became pluggable (generated from the original `encode.build_command`). Anything that changes how
the NVENC command is built must either leave it identical or update that file on purpose, with the
reason in the commit message. This is what makes "adding QSV did not change NVENC" a checked fact
rather than a belief.

Run `python -m tests.golden_commands` to regenerate the file (only ever on purpose).
"""
from __future__ import annotations

import itertools
import json
from pathlib import Path

from shelfsaver.models import Decision, Stream
from tests.helpers import info

GOLDEN_FILE = Path(__file__).with_name("golden_nvenc_commands.json")

HDR = {"color_primaries": "bt2020", "color_transfer": "smpte2084", "colorspace": "bt2020nc"}

# Stream layouts: each is (label, source extension, streams, decision remove_audio).
_VIDEO = Stream(0, "video", "h264", is_default=True)
_ENG = Stream(1, "audio", "aac", "eng", is_default=True)
_SPA = Stream(2, "audio", "aac", "spa")
LAYOUTS = {
    "plain": (".mkv", (_VIDEO, _ENG), ()),
    "plain-mp4": (".mp4", (_VIDEO, _ENG), ()),
    "spanish-removed": (".mkv", (_VIDEO, _ENG, _SPA), (2,)),
    "mov_text-subs": (".mp4", (_VIDEO, _ENG, Stream(2, "subtitle", "mov_text", "eng")), ()),
    "cover-art": (".mkv", (_VIDEO, _ENG, Stream(2, "video", "mjpeg", is_cover_art=True)), ()),
    "pgs-subs": (".mkv", (_VIDEO, _ENG, Stream(2, "subtitle", "hdmv_pgs_subtitle", "eng")), ()),
}


def cases():
    """Yield (name, kwargs for build_command) for every case in the matrix."""
    from shelfsaver.encode import output_extension

    def case(name, layout, level, depth, field, color, action):
        ext, streams, removed = LAYOUTS[layout]
        media = info(ext=ext, bit_depth=depth, field_order=field, streams=streams, **color)
        decision = Decision(action, "test", removed)
        out = Path("out" + output_extension(media, decision))
        return name, dict(info=media, decision=decision, level=level, source=Path("in.mkv"),
                          output=out, ffmpeg="ffmpeg")

    # Every encoder-relevant property, crossed fully, on the two plain layouts.
    for level, depth, field, color_name, layout in itertools.product(
            ("low", "medium", "high"), (8, 10), ("progressive", "tff"), ("sdr", "hdr"),
            ("plain", "plain-mp4")):
        yield case(f"{layout}/{level}/{depth}bit/{field}/{color_name}", layout, level, depth,
                   field, HDR if color_name == "hdr" else {}, "convert")
    # Every stream layout and both actions at one fixed setting.
    for layout, action in itertools.product(LAYOUTS, ("convert", "strip_only")):
        yield case(f"layout/{layout}/{action}", layout, "medium", 8, "progressive", {}, action)
    # Missing optional facts must not produce stray arguments.
    yield case("unknown-depth", "plain", "medium", None, None, {}, "convert")
    yield case("unknown-colour", "plain", "medium", 8, "unknown",
               {"color_primaries": "unknown", "color_transfer": "unknown", "colorspace": "unknown"},
               "convert")


def build_all(build_command=None) -> dict:
    """Every case's command, as lists of strings, keyed by case name."""
    if build_command is None:
        from shelfsaver.encode import build_command
    return {name: [str(a) for a in build_command(**kwargs)] for name, kwargs in cases()}


if __name__ == "__main__":
    GOLDEN_FILE.write_text(json.dumps(build_all(), indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {len(build_all())} cases to {GOLDEN_FILE}")
