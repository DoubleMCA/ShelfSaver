import dataclasses
import io
import random
import subprocess

import pytest

from shelfsaver.probe import ProbeError, StreamSpan, probe
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


def _mux(tmp_path, name, video, video_t, audio, audio_t):
    """Copy video (limited to video_t seconds) and audio (to audio_t) into one mkv."""
    out = tmp_path / name
    subprocess.run(["ffmpeg", "-y", "-v", "error",
                    "-t", str(video_t), "-i", str(video), "-t", str(audio_t), "-i", str(audio),
                    "-map", "0:v", "-map", "1:a", "-c", "copy", str(out)], check=True)
    return out


def _probed_with_size(src, size):
    return dataclasses.replace(src, size=size)


def test_short_video_with_full_audio_fails_length(tmp_path):
    base = _out(tmp_path, "base.mkv")
    src = _src(tmp_path)
    out = _mux(tmp_path, "o.mkv", base, 2, base, 4)
    r = verify_output(_probed_with_size(src, out.stat().st_size * 10), out, convert(),
                      "ffmpeg", "ffprobe")
    assert not r.passed and r.checks["length"] is False and r.checks["plays"] is False


def test_short_audio_with_full_video_fails_length(tmp_path):
    base = _out(tmp_path, "base.mkv")
    src = _src(tmp_path)
    out = _mux(tmp_path, "o.mkv", base, 4, base, 2)
    r = verify_output(_probed_with_size(src, out.stat().st_size * 10), out, convert(),
                      "ffmpeg", "ffprobe")
    assert not r.passed and r.checks["length"] is False


def test_source_with_differing_stream_lengths_passes_when_output_matches(tmp_path):
    h264 = make_video(tmp_path / "h264.mkv", seconds=4, size=SIZE, crf=10)
    src_file = _mux(tmp_path, "src.mkv", h264, 4, h264, 2)
    src = probe(src_file, "ffprobe")
    h265 = _out(tmp_path, "h265.mkv")
    out = _mux(tmp_path, "o.mkv", h265, 4, h265, 2)
    r = verify_output(src, out, convert(), "ffmpeg", "ffprobe")
    assert r.passed, r


def test_stop_during_length_scan(pair, monkeypatch):
    import shelfsaver.verify as v
    monkeypatch.setattr(v, "_plays", lambda *a, **k: pytest.fail("plays must not run"))
    r = verify_output(*pair, should_stop=lambda: True)
    assert r.reason == "Stopped" and not r.passed and r.checks["plays"] is False


def _cover_first_with_noise(tmp_path, noisy=True):
    """mkv whose first video stream is cover art and whose HEVC packets are damaged."""
    base = _out(tmp_path, "base.mkv", crf=18)
    cover = tmp_path / "cover.jpg"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                    "testsrc2=size=64x64:rate=1:duration=1", "-frames:v", "1", str(cover)],
                   check=True)
    out = tmp_path / "cover_first.mkv"
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(cover), "-i", str(base),
           "-map", "0:v", "-map", "1:v", "-map", "1:a", "-c", "copy",
           "-disposition:v:0", "attached_pic"]
    if noisy:
        cmd += ["-bsf:v:1", "noise=amount=400"]
    subprocess.run(cmd + [str(out)], check=True)
    return out


def test_cover_first_file_with_damaged_video_fails_plays(tmp_path):
    out = _cover_first_with_noise(tmp_path)
    streams = probe(out, "ffprobe").streams
    assert streams[0].is_cover_art and streams[0].kind == "video"
    src = dataclasses.replace(_src(tmp_path), size=out.stat().st_size * 10)
    r = verify_output(src, out, convert(), "ffmpeg", "ffprobe")
    assert r.checks["length"] and r.checks["tracks"] and r.checks["smaller"]
    assert r.checks["plays"] is False and not r.passed
    assert r.reason == "New file failed the playback check"
    assert r.log  # what FFmpeg said while decoding, so the failure explains itself


def test_cover_first_file_with_good_video_passes(tmp_path):
    out = _cover_first_with_noise(tmp_path, noisy=False)
    src = dataclasses.replace(_src(tmp_path), size=out.stat().st_size * 10)
    r = verify_output(src, out, convert(), "ffmpeg", "ffprobe")
    assert r.passed and r.log == ""


def test_ffmpeg_that_will_not_start_gives_a_reason(tmp_path, monkeypatch):
    from shelfsaver import verify as v

    def no_ffmpeg(*a, **k):
        raise OSError("no such program")

    monkeypatch.setattr(v.subprocess, "Popen", no_ffmpeg)
    assert v._plays(tmp_path / "a.mkv", 0, "ffmpeg", lambda: False) == (
        False, "FFmpeg could not start: no such program")


def test_silent_decode_failure_names_the_exit_code(tmp_path, monkeypatch):
    from shelfsaver import verify as v

    class Quiet:
        returncode = 3
        stderr = io.BytesIO(b"")

        def poll(self):
            return 3

        def wait(self, timeout=None):
            return 3

    monkeypatch.setattr(v, "kill_with_app", lambda proc: None)
    monkeypatch.setattr(v.subprocess, "Popen", lambda *a, **k: Quiet())
    plays, log = v._plays(tmp_path / "a.mkv", 0, "ffmpeg", lambda: False)
    assert plays is False and "3" in log


def test_changed_primaries_on_hdr_source_fails_tracks(tmp_path):
    src = _src(tmp_path, vcodec="libx265", hdr=True)
    out = probe(_out(tmp_path, hdr=True, crf=35), "ffprobe")
    assert out.color_transfer == "smpte2084"
    bad_src = dataclasses.replace(src, color_primaries="bt709")
    r = verify_output(bad_src, out.path, convert(), "ffmpeg", "ffprobe")
    assert r.checks["tracks"] is False


def test_eight_bit_output_for_hdr_source_fails_tracks(tmp_path, monkeypatch):
    import shelfsaver.verify as v
    src = _src(tmp_path, vcodec="libx265", hdr=True)
    real = v.probe
    monkeypatch.setattr(v, "probe", lambda p, f: dataclasses.replace(real(p, f), bit_depth=8))
    r = verify_output(src, _out(tmp_path, hdr=True, crf=35), convert(), "ffmpeg", "ffprobe")
    assert r.checks["tracks"] is False


def test_unreadable_output_is_a_failed_result(tmp_path):
    bad = tmp_path / "bad.mkv"
    bad.write_bytes(b"not a video")
    r = verify_output(_src(tmp_path), bad, convert(), "ffmpeg", "ffprobe")
    assert not r.passed and not any(r.checks.values()) and r.reason


def test_missing_output_is_a_failed_result(tmp_path):
    r = verify_output(_src(tmp_path), tmp_path / "nope.mkv", convert(), "ffmpeg", "ffprobe")
    assert not r.passed


def test_stop_during_plays_check(pair, monkeypatch):
    import shelfsaver.verify as v
    monkeypatch.setattr(v, "_stream_lengths_ok", lambda *a, **k: True)
    r = verify_output(*pair, should_stop=lambda: True)
    assert not r.passed and r.reason == "Stopped"
    assert r.checks["plays"] is False and not r.not_worth_it


def test_failed_scan_fails_length(pair, monkeypatch):
    import shelfsaver.verify as v

    def boom(*a, **k):
        raise ProbeError("scan failed")
    monkeypatch.setattr(v, "scan_streams", boom)
    r = verify_output(*pair)
    assert not r.passed and r.checks["length"] is False and r.checks["plays"] is False
    assert r.reason == "New file is a different length"


def _filtered(tmp_path, name, base, vf=None, af=None):
    """Re-encode `base` keeping original timestamps, dropping frames by select/aselect."""
    out = tmp_path / name
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(base), "-fps_mode", "passthrough"]
    cmd += ["-vf", vf, "-c:v", "libx265", "-crf", "30"] if vf else ["-c:v", "copy"]
    cmd += ["-af", af, "-c:a", "aac"] if af else ["-c:a", "copy"]
    subprocess.run(cmd + [str(out)], check=True)
    return out


def _long_pair(tmp_path):
    src = _src(tmp_path, seconds=20)
    base = _out(tmp_path, "base.mkv", seconds=20)
    return src, base


def _check_fails_length(tmp_path, src, out):
    src = dataclasses.replace(src, size=out.stat().st_size * 10)
    r = verify_output(src, out, convert(), "ffmpeg", "ffprobe")
    assert not r.passed and r.checks["length"] is False, r
    assert r.checks["tracks"] and r.checks["smaller"]


def test_late_start_video_fails_length(tmp_path):
    src, base = _long_pair(tmp_path)
    _check_fails_length(tmp_path, src, _filtered(tmp_path, "o.mkv", base, vf="select='gte(t,10)'"))


def test_mid_file_video_hole_fails_length(tmp_path):
    src, base = _long_pair(tmp_path)
    out = _filtered(tmp_path, "o.mkv", base, vf="select='lt(t,5)+gt(t,15)'")
    _check_fails_length(tmp_path, src, out)


def test_audio_hole_fails_length(tmp_path):
    src, base = _long_pair(tmp_path)
    out = _filtered(tmp_path, "o.mkv", base, af="aselect='lt(t,5)+gt(t,15)'")
    _check_fails_length(tmp_path, src, out)


def test_unfiltered_long_output_passes(tmp_path):
    src, base = _long_pair(tmp_path)
    src = dataclasses.replace(src, size=base.stat().st_size * 10)
    assert verify_output(src, base, convert(), "ffmpeg", "ffprobe").passed


def test_transport_stream_source_with_start_offset_passes(tmp_path):
    h264 = make_video(tmp_path / "h.mkv", seconds=4, size=SIZE, crf=10)
    ts = tmp_path / "src.ts"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(h264), "-c", "copy", str(ts)],
                   check=True)
    src = probe(ts, "ffprobe")
    assert src.video_codec == "h264"
    out = _out(tmp_path)
    src = dataclasses.replace(src, size=out.stat().st_size * 10)
    r = verify_output(src, out, convert(), "ffmpeg", "ffprobe")
    assert r.passed, r


def test_large_source_start_offset_passes(tmp_path):
    h264 = make_video(tmp_path / "h.mkv", seconds=4, size=SIZE, crf=10)
    off = tmp_path / "off.mkv"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(h264), "-c", "copy",
                    "-output_ts_offset", "3600", str(off)], check=True)
    out = _out(tmp_path)
    src = dataclasses.replace(probe(off, "ffprobe"), size=out.stat().st_size * 10)
    r = verify_output(src, out, convert(), "ffmpeg", "ffprobe")
    assert r.passed, r


@pytest.mark.parametrize("ext", [".mkv", ".mp4"])
def test_damaged_bytes_never_pass(tmp_path, ext):
    out = _out(tmp_path, "d" + ext, crf=18)
    data = bytearray(out.read_bytes())
    mid = len(data) // 2
    rng = random.Random(1234)
    data[mid:mid + 4000] = bytes(rng.randrange(256) for _ in range(4000))
    out.write_bytes(bytes(data))
    src = dataclasses.replace(_src(tmp_path), size=out.stat().st_size * 10)
    r = verify_output(src, out, convert(), "ffmpeg", "ffprobe")
    assert not r.passed


def _fake_spans(monkeypatch, src_span, out_span):
    import shelfsaver.verify as v
    monkeypatch.setattr(v, "scan_streams",
                        lambda path, *a, **k: src_span if "src" in str(path) else out_span)


@pytest.mark.parametrize("duration,diff,expected", [
    (100.0, 0.9, True), (100.0, 1.5, False),       # 1 s floor
    (10000.0, 15.0, True), (10000.0, 25.0, False),  # 0.2% term (20 s)
])
def test_tolerance_floor_and_percentage(tmp_path, monkeypatch, duration, diff, expected):
    import shelfsaver.verify as v
    src = dataclasses.replace(_src(tmp_path), path="src.mkv", duration=duration)
    out = probe(_out(tmp_path), "ffprobe")
    vid = v._main_video(src).index
    aud = next(s.index for s in src.streams if s.kind == "audio")
    ovid = v._main_video(out).index
    oaud = next(s.index for s in out.streams if s.kind == "audio")
    span = StreamSpan(0.0, duration, 0.04)
    short = StreamSpan(0.0, duration - diff, 0.04)
    _fake_spans(monkeypatch, {vid: span, aud: span}, {ovid: short, oaud: span})
    assert v._stream_lengths_ok(src, out, "out.mkv", convert(), "ffprobe", lambda: False) is expected


def test_hdr_transfer_list_includes_arib_std_b67(tmp_path):
    import shelfsaver.verify as v
    base = _src(tmp_path)
    src = dataclasses.replace(base, color_transfer="arib-std-b67", color_primaries="bt2020")
    good = dataclasses.replace(probe(_out(tmp_path), "ffprobe"), color_transfer="arib-std-b67",
                               color_primaries="bt2020", bit_depth=10)
    assert v._tracks_ok(src, good, convert())
    assert not v._tracks_ok(src, dataclasses.replace(good, bit_depth=8), convert())
    assert not v._tracks_ok(src, dataclasses.replace(good, color_primaries="bt709"), convert())


def test_truncated_output_is_never_excused_as_not_worth_it(tmp_path):
    src = dataclasses.replace(_src(tmp_path, seconds=10), size=10_000_000)
    r = verify_output(src, _out(tmp_path, seconds=5), convert(), "ffmpeg", "ffprobe")
    assert r.checks["smaller"] and not r.not_worth_it and r.checks["length"] is False


def test_full_decode_is_tied_to_the_app(tmp_path, monkeypatch):
    from shelfsaver import verify as v
    tied = []
    monkeypatch.setattr(v, "kill_with_app", lambda proc: tied.append(proc.pid))
    path = make_video(tmp_path / "a.mkv", seconds=1, size="320x180")
    assert v._plays(path, 0, "ffmpeg", lambda: False) == (True, "")
    assert len(tied) == 1


CUT_SHORT = "The original seems to be cut short, so it was left alone"


def _cut_in_half(path):
    data = path.read_bytes()
    path.write_bytes(data[:len(data) // 2])
    return path


def _encode(source, out):
    """What the real conversion does: rebuild from whatever the source's demuxer can read."""
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(source), "-map", "0",
                    "-c:v", "libx265", "-crf", "30", "-x265-params", "log-level=error",
                    "-c:a", "copy", "-c:s", "copy", str(out)], check=True)
    return out


def test_source_that_stops_before_its_stated_length_fails(tmp_path):
    source = _cut_in_half(make_video(tmp_path / "src.mkv", seconds=20, size=SIZE, crf=10))
    src = probe(source, "ffprobe")
    assert src.duration == pytest.approx(20, abs=1)  # the header still says 20 seconds
    out = _encode(source, tmp_path / "o.mkv")
    src = dataclasses.replace(src, size=out.stat().st_size * 10)
    r = verify_output(src, out, convert(), "ffmpeg", "ffprobe")
    assert not r.passed and r.checks["length"] is False and r.checks["plays"] is False
    assert r.reason == CUT_SHORT and not r.not_worth_it


def test_source_with_a_start_offset_that_stops_early_fails(tmp_path):
    whole = make_video(tmp_path / "h.mkv", seconds=20, size=SIZE, crf=10)
    source = tmp_path / "src.mkv"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(whole), "-c", "copy",
                    "-output_ts_offset", "3600", str(source)], check=True)
    _cut_in_half(source)
    out = _encode(source, tmp_path / "o.mkv")
    src = dataclasses.replace(probe(source, "ffprobe"), size=out.stat().st_size * 10)
    r = verify_output(src, out, convert(), "ffmpeg", "ffprobe")
    assert not r.passed and r.reason == CUT_SHORT


def test_lone_long_subtitle_does_not_count_as_a_cut_short_source(tmp_path):
    # The last subtitle starts long after the picture ends, and itself lasts a long time.
    subs = tmp_path / "late.srt"
    subs.write_text("1\n00:01:40,000 --> 00:02:10,000\nLate\n", encoding="utf-8")
    plain = make_video(tmp_path / "h.mkv", seconds=4, size=SIZE, crf=10)
    source = tmp_path / "src.mkv"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(plain), "-i", str(subs),
                    "-map", "0", "-map", "1", "-c", "copy", "-c:s", "srt",
                    "-metadata:s:s:0", "language=eng", str(source)], check=True)
    src = probe(source, "ffprobe")
    assert src.duration > 100
    out = _encode(source, tmp_path / "o.mkv")
    src = dataclasses.replace(src, size=out.stat().st_size * 10)
    r = verify_output(src, out, convert(), "ffmpeg", "ffprobe")
    assert r.passed, r


def test_removed_longer_audio_does_not_count_as_a_cut_short_source(tmp_path):
    long_av = make_video(tmp_path / "long.mkv", seconds=20, size=SIZE, crf=10)
    source = tmp_path / "src.mkv"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-t", "4", "-i", str(long_av),
                    "-i", str(long_av), "-map", "0:v", "-map", "0:a", "-map", "1:a",
                    "-c", "copy", "-metadata:s:a:0", "language=eng",
                    "-metadata:s:a:1", "language=spa", str(source)], check=True)
    src = probe(source, "ffprobe")
    assert src.duration > 15
    spa = next(s.index for s in src.streams if s.language == "spa")
    out = _out(tmp_path)
    src = dataclasses.replace(src, size=out.stat().st_size * 10)
    r = verify_output(src, out, convert(remove_audio=(spa,)), "ffmpeg", "ffprobe")
    assert r.passed, r


@pytest.mark.parametrize("duration,span_end,cut_short", [
    (100.0, 98.5, False), (100.0, 97.5, True),     # 98% of a longer file
    (10.0, 9.1, False), (10.0, 8.9, True),         # the 1 s floor on a short one
    (0.0, 50.0, False),                            # no stated length to compare with
])
def test_how_far_short_a_source_can_be(tmp_path, monkeypatch, duration, span_end, cut_short):
    import shelfsaver.verify as v
    src = dataclasses.replace(_src(tmp_path), path="src.mkv", duration=duration)
    out = probe(_out(tmp_path), "ffprobe")
    vid = v._main_video(src).index
    aud = next(s.index for s in src.streams if s.kind == "audio")
    ovid = v._main_video(out).index
    oaud = next(s.index for s in out.streams if s.kind == "audio")
    span = StreamSpan(0.0, span_end, 0.04)
    _fake_spans(monkeypatch, {vid: span, aud: span}, {ovid: span, oaud: span})
    src = dataclasses.replace(src, size=out.size * 10)
    monkeypatch.setattr(v, "_plays", lambda *a, **k: (True, ""))
    r = verify_output(src, out.path, convert(), "ffmpeg", "ffprobe")
    assert (r.reason == CUT_SHORT) is cut_short and r.passed is not cut_short
