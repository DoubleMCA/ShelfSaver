import dataclasses
import random

import pytest

from shelfsaver.probe import probe
from shelfsaver.verify import verify_output
from tests.helpers import convert, make_video, strip

SIZE = "320x240"


def _src(tmp_path, name="src.mkv", seconds=4, **kw):
    kw.setdefault("crf", 10)
    return probe(make_video(tmp_path / name, seconds=seconds, size=SIZE, **kw), "ffprobe")


def _out(tmp_path, name="out.mkv", seconds=4, **kw):
    kw.setdefault("vcodec", "libx265")
    kw.setdefault("crf", 30)
    return make_video(tmp_path / name, seconds=seconds, size=SIZE, **kw)


@pytest.fixture
def pair(tmp_path):
    return _src(tmp_path), _out(tmp_path), convert(), "ffmpeg", "ffprobe"


def test_good_output_passes(pair):
    r = verify_output(*pair)
    assert r.passed and all(r.checks.values()) and not r.not_worth_it


def test_truncated_duration_fails_length(tmp_path):
    r = verify_output(_src(tmp_path, seconds=10), _out(tmp_path, seconds=5),
                      convert(), "ffmpeg", "ffprobe")
    assert not r.passed and r.checks["length"] is False and r.checks["plays"] is False


def test_missing_audio_fails_tracks(tmp_path):
    src = _src(tmp_path, audio=(("eng", ""), ("spa", "")))
    r = verify_output(src, _out(tmp_path, audio=(("eng", ""),)), convert(), "ffmpeg", "ffprobe")
    assert not r.passed and r.checks["tracks"] is False


def test_wrong_audio_language_fails_tracks(tmp_path):
    src = _src(tmp_path, audio=(("eng", ""),))
    r = verify_output(src, _out(tmp_path, audio=(("fre", ""),)), convert(), "ffmpeg", "ffprobe")
    assert r.checks["tracks"] is False


def test_missing_subtitle_fails_tracks(tmp_path):
    src = _src(tmp_path, subs=(("eng", ""),))
    r = verify_output(src, _out(tmp_path), convert(), "ffmpeg", "ffprobe")
    assert r.checks["tracks"] is False


def test_expected_removed_audio_passes(tmp_path):
    src = _src(tmp_path, audio=(("eng", ""), ("spa", "")))
    spa = [s.index for s in src.streams if s.kind == "audio" and s.language == "spa"]
    r = verify_output(src, _out(tmp_path, audio=(("eng", ""),)),
                      convert(remove_audio=tuple(spa)), "ffmpeg", "ffprobe")
    assert r.passed, r


def test_unexpected_removed_track_fails(tmp_path):
    src = _src(tmp_path, audio=(("eng", ""), ("spa", ""), ("fre", "")))
    spa = [s.index for s in src.streams if s.kind == "audio" and s.language == "spa"]
    r = verify_output(src, _out(tmp_path, audio=(("eng", ""),)),
                      convert(remove_audio=tuple(spa)), "ffmpeg", "ffprobe")
    assert not r.passed and r.checks["tracks"] is False


def test_output_not_hevc_fails_tracks(tmp_path):
    r = verify_output(_src(tmp_path), _out(tmp_path, vcodec="libx264"),
                      convert(), "ffmpeg", "ffprobe")
    assert r.checks["tracks"] is False


def test_lost_hdr_fails_tracks(tmp_path):
    src = _src(tmp_path, vcodec="libx265", hdr=True)
    assert src.color_transfer == "smpte2084"
    r = verify_output(src, _out(tmp_path), convert(), "ffmpeg", "ffprobe")
    assert not r.passed and r.checks["tracks"] is False


def test_kept_hdr_passes(tmp_path):
    src = _src(tmp_path, vcodec="libx265", hdr=True, crf=10)
    r = verify_output(src, _out(tmp_path, hdr=True, crf=35), convert(), "ffmpeg", "ffprobe")
    assert r.passed, r


def test_strip_only_keeps_source_codec(tmp_path):
    src = _src(tmp_path, audio=(("eng", ""), ("spa", "")))
    spa = [s.index for s in src.streams if s.kind == "audio" and s.language == "spa"]
    out = _out(tmp_path, vcodec="libx264", crf=30, audio=(("eng", ""),))
    r = verify_output(src, out, strip(remove_audio=tuple(spa)), "ffmpeg", "ffprobe")
    assert r.passed, r


def test_nine_percent_savings_is_not_worth_it(tmp_path):
    out = _out(tmp_path)
    src = dataclasses.replace(_src(tmp_path), size=int(out.stat().st_size / 0.91))
    r = verify_output(src, out, convert(), "ffmpeg", "ffprobe")
    assert not r.passed and r.not_worth_it and r.reason == "Would not save enough"
    assert r.checks["smaller"] is False and r.checks["plays"] is False


def test_ten_percent_savings_passes(tmp_path):
    out = _out(tmp_path)
    src = dataclasses.replace(_src(tmp_path), size=int(out.stat().st_size / 0.89))
    assert verify_output(src, out, convert(), "ffmpeg", "ffprobe").passed


def test_length_failure_with_small_output_is_not_flagged_not_worth_it(tmp_path):
    src = dataclasses.replace(_src(tmp_path, seconds=10), size=1)
    r = verify_output(src, _out(tmp_path, seconds=5), convert(), "ffmpeg", "ffprobe")
    assert not r.not_worth_it and r.checks["length"] is False


def test_corrupt_middle_fails_plays(tmp_path):
    src = _src(tmp_path, crf=10)
    out = _out(tmp_path, crf=18)
    data = bytearray(out.read_bytes())
    mid = len(data) // 2
    rng = random.Random(1)
    data[mid:mid + 4000] = bytes(rng.randrange(256) for _ in range(4000))
    out.write_bytes(bytes(data))
    src = dataclasses.replace(src, size=out.stat().st_size * 10)
    r = verify_output(src, out, convert(), "ffmpeg", "ffprobe")
    assert not r.passed
    assert r.checks["length"] and r.checks["tracks"] and r.checks["smaller"]
    assert r.checks["plays"] is False
    assert r.reason == "New file failed the playback check"


def test_unreadable_output_is_a_failed_result(tmp_path):
    bad = tmp_path / "bad.mkv"
    bad.write_bytes(b"not a video")
    r = verify_output(_src(tmp_path), bad, convert(), "ffmpeg", "ffprobe")
    assert not r.passed and not any(r.checks.values()) and r.reason


def test_missing_output_is_a_failed_result(tmp_path):
    r = verify_output(_src(tmp_path), tmp_path / "nope.mkv", convert(), "ffmpeg", "ffprobe")
    assert not r.passed


def test_stop_during_plays_check(pair):
    r = verify_output(*pair, should_stop=lambda: True)
    assert not r.passed and r.reason == "Stopped"
    assert r.checks["plays"] is False and not r.not_worth_it
