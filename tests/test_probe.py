import pytest

from shelfsaver.probe import ProbeError, parse_probe, probe
from tests.helpers import make_video


def test_probe_reads_languages_and_codec(tmp_path):
    p = make_video(tmp_path / "a.mkv", audio=[("eng", "Main"), ("spa", "Spanish")])
    info = probe(p, "ffprobe")
    assert info.video_codec == "h264" and info.width == 1920
    assert [s.language for s in info.streams if s.kind == "audio"] == ["eng", "spa"]


def test_probe_rejects_garbage(tmp_path):
    (tmp_path / "b.mkv").write_bytes(b"nope")
    with pytest.raises(ProbeError):
        probe(tmp_path / "b.mkv", "ffprobe")


def test_probe_reports_basics_and_subtitles(tmp_path):
    p = make_video(tmp_path / "c.mkv", size="1280x720", subs=[("eng", "Full")])
    info = probe(p, "ffprobe")
    assert info.ext == ".mkv" and info.size == p.stat().st_size
    assert info.height == 720 and info.duration == pytest.approx(3, abs=0.5)
    assert info.bit_depth == 8 and info.video_bitrate > 0
    assert [s.language for s in info.streams if s.kind == "subtitle"] == ["eng"]
    assert not info.dolby_vision


def test_probe_marks_audio_default_and_title(tmp_path):
    p = make_video(tmp_path / "d.mkv", audio=[("eng", "Main"), ("fre", "French Dub")])
    audio = [s for s in probe(p, "ffprobe").streams if s.kind == "audio"]
    assert [s.title for s in audio] == ["Main", "French Dub"]
    assert audio[0].is_default


def test_probe_detects_interlaced(tmp_path):
    p = make_video(tmp_path / "e.mkv", interlaced=True)
    assert probe(p, "ffprobe").field_order in ("tt", "bb", "tb", "bt")


def test_probe_detects_hdr_ten_bit(tmp_path):
    p = make_video(tmp_path / "f.mkv", vcodec="libx265", hdr=True)
    info = probe(p, "ffprobe")
    assert info.bit_depth == 10 and info.color_transfer == "smpte2084"
    assert info.color_primaries == "bt2020" and info.video_codec == "hevc"


@pytest.mark.parametrize("name", ["g.mkv", "g.mp4"])
def test_probe_flags_cover_art(tmp_path, name):
    p = make_video(tmp_path / name, cover_art=True)
    info = probe(p, "ffprobe")
    assert any(s.is_cover_art for s in info.streams)
    assert info.width == 1920  # main video, not the cover image


def _raw(**video):
    stream = {"index": 0, "codec_type": "video", "codec_name": "hevc",
              "width": 3840, "height": 2160, "pix_fmt": "yuv420p10le"}
    stream.update(video)
    return {"format": {"duration": "10.0"}, "streams": [stream]}


def test_parse_detects_dolby_vision():
    data = _raw(side_data_list=[{"side_data_type": "DOVI configuration record"}])
    assert parse_probe(data, "x.mkv", 1000).dolby_vision is True
    assert parse_probe(_raw(), "x.mkv", 1000).dolby_vision is False


def test_bitrate_prefers_stream_then_bps_tag_then_estimate():
    assert parse_probe(_raw(bit_rate="5000000", tags={"BPS": "9"}), "x.mkv", 1000).video_bitrate == 5_000_000
    assert parse_probe(_raw(tags={"BPS": "4000000"}), "x.mkv", 1000).video_bitrate == 4_000_000
    data = _raw()
    data["streams"].append({"index": 1, "codec_type": "audio", "codec_name": "aac",
                            "bit_rate": "100000"})
    # 1_000_000 bytes over 10 s is 800_000 bit/s overall; minus 100_000 for audio
    assert parse_probe(data, "x.mkv", 1_000_000).video_bitrate == 700_000


def test_zero_duration_is_an_error():
    data = _raw()
    data["format"]["duration"] = "0"
    with pytest.raises(ProbeError):
        parse_probe(data, "x.mkv", 1000)
