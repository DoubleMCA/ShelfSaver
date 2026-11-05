import hashlib
import importlib.util
from pathlib import Path

import pytest

from tests.helpers import make_video

_SPEC = importlib.util.spec_from_file_location(
    "make_compare_clips",
    Path(__file__).resolve().parent.parent / "scripts" / "make_compare_clips.py",
)
clips = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(clips)


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
