import hashlib
import importlib.util
from pathlib import Path

import pytest

from shelfsaver.probe import probe

from tests.helpers import info, make_video

_SPEC = importlib.util.spec_from_file_location(
    "make_compare_clips",
    Path(__file__).resolve().parent.parent / "scripts" / "make_compare_clips.py",
)
clips = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(clips)


@pytest.fixture(autouse=True)
def _private_data_dir(tmp_path, monkeypatch):
    # An installed PC has an admin-only data folder; tests must never depend on it.
    monkeypatch.setenv("SHELFSAVER_DATA", str(tmp_path / "appdata"))


def _digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@pytest.mark.gpu
def test_clips_created_with_all_levels(tmp_path, capsys):
    src = make_video(tmp_path / "source" / "Some Movie.mkv", seconds=6, size="640x360")
    out = tmp_path / "clips"
    before = _digest(src)

    code = clips.main(["--source", str(src), "--start", "1", "--seconds", "3",
                       "--out", str(out)])

    assert code == 0
    names = {p.name for p in out.iterdir()}
    assert names == {"Some Movie Original.mkv", "Some Movie Low.mkv",
                     "Some Movie Medium.mkv", "Some Movie High.mkv"}
    assert (out / "Some Movie High.mkv").stat().st_size < (out / "Some Movie Low.mkv").stat().st_size
    assert _digest(src) == before
    table = capsys.readouterr().out
    assert "Low" in table and "High" in table and "%" in table


def test_refuses_out_inside_source_folder(tmp_path, capsys):
    src = make_video(tmp_path / "lib" / "Movie.mkv", seconds=2, size="320x180")
    for out in (src.parent, src.parent / "clips"):
        assert clips.main(["--source", str(src), "--start", "0", "--out", str(out)]) == 2
    assert [p.name for p in src.parent.iterdir()] == ["Movie.mkv"]
    assert "inside" in capsys.readouterr().err


def test_refuses_missing_source(tmp_path):
    assert clips.main(["--source", str(tmp_path / "nope.mkv"), "--start", "0",
                       "--out", str(tmp_path / "o")]) == 2


def test_clip_names_come_from_level_names():
    assert clips.clip_name("Film", "original") == "Film Original.mkv"
    assert clips.clip_name("Film", "high") == "Film High.mkv"


def test_decision_is_forced_to_convert_for_already_hevc():
    decision = clips.comparison_decision(info(video_codec="hevc"))
    assert decision.action == "convert" and decision.remove_audio == ()


def test_decision_keeps_natural_convert():
    assert clips.comparison_decision(info()).reason == "Worth converting"


@pytest.mark.gpu
def test_already_hevc_source_still_gets_all_levels(tmp_path):
    src = make_video(tmp_path / "source" / "Modern.mkv", seconds=4, size="640x360",
                     vcodec="libx265")
    out = tmp_path / "clips"
    assert clips.main(["--source", str(src), "--start", "0", "--seconds", "2",
                       "--out", str(out)]) == 0
    assert len(list(out.iterdir())) == 4


def test_mov_text_subtitles_are_cut_into_the_clip_as_srt(tmp_path):
    src = make_video(tmp_path / "source" / "Subbed.mp4", seconds=3, size="320x180",
                     subs=[("eng", None)])
    dest = tmp_path / "clips" / "Subbed Original.mkv"
    dest.parent.mkdir()
    clips._cut(src, 0, 2, dest, "ffmpeg", "ffprobe")
    subs = [s for s in probe(dest, "ffprobe").streams if s.kind == "subtitle"]
    assert [s.codec for s in subs] == ["subrip"]


@pytest.mark.gpu
def test_clips_made_from_mp4_with_text_subtitles(tmp_path):
    src = make_video(tmp_path / "source" / "Subbed.mp4", seconds=3, size="320x180",
                     subs=[("eng", None)])
    out = tmp_path / "clips"
    assert clips.main(["--source", str(src), "--start", "0", "--seconds", "2",
                       "--out", str(out)]) == 0
