"""Predicting an H.265 file's saving from short sections. The GPU encode is faked."""
from __future__ import annotations

from pathlib import Path

import pytest

from shelfsaver import sample
from shelfsaver.encode import EncodeResult
from shelfsaver.probe import probe
from shelfsaver.sample import SampleError, predict_saving, section_starts
from tests.helpers import make_video


def test_a_long_file_is_sampled_near_the_start_middle_and_end():
    assert section_starts(3600) == [(530.0, 20.0), (1790.0, 20.0), (3050.0, 20.0)]


def test_a_short_file_is_sampled_once_in_the_middle():
    assert section_starts(60) == [(20.0, 20.0)]


def test_a_very_short_file_is_sampled_whole():
    assert section_starts(10) == [(0.0, 10.0)]


class FakeEncode:
    """Writes the converted section at `ratio` of the cut section's size."""

    def __init__(self, ratio=0.5, returncode=0, stopped=False):
        self.ratio, self.returncode, self.stopped = ratio, returncode, stopped
        self.commands = []

    def __call__(self, cmd, duration, on_progress, should_stop):
        self.commands.append(cmd)
        source, out = Path(cmd[cmd.index("-i") + 1]), Path(cmd[-1])
        if self.returncode == 0 and not self.stopped:
            out.write_bytes(b"\0" * int(source.stat().st_size * self.ratio))
        return EncodeResult(self.returncode, self.stopped, "sample encoder")


@pytest.fixture
def fat(tmp_path):
    path = make_video(tmp_path / "fat.mkv", seconds=2, size="640x360", vcodec="libx265", crf=1)
    return probe(path, "ffprobe")


def _predict(fat, tmp_path, monkeypatch, encode, should_stop=lambda: False):
    monkeypatch.setattr(sample, "run_ffmpeg", encode)
    return predict_saving(fat, "high", Path(fat.path), tmp_path / "run", "ffmpeg", "ffprobe",
                          should_stop)


def test_saving_is_the_video_saving_scaled_to_the_whole_file(fat, tmp_path, monkeypatch):
    saving = _predict(fat, tmp_path, monkeypatch, FakeEncode(ratio=0.5))
    share = min(1.0, fat.video_bitrate * fat.duration / 8 / fat.size)
    assert saving == pytest.approx(0.5 * share, abs=0.02)


def test_sections_are_encoded_like_the_real_conversion(fat, tmp_path, monkeypatch):
    encode = FakeEncode()
    _predict(fat, tmp_path, monkeypatch, encode)
    cmd = encode.commands[0]
    assert "hevc_nvenc" in cmd and cmd[cmd.index("-cq") + 1] == "30"


def test_sample_files_are_deleted_afterwards(fat, tmp_path, monkeypatch):
    _predict(fat, tmp_path, monkeypatch, FakeEncode())
    assert list((tmp_path / "run").iterdir()) == []


def test_stopping_gives_no_answer(fat, tmp_path, monkeypatch):
    assert _predict(fat, tmp_path, monkeypatch, FakeEncode(), should_stop=lambda: True) is None
    assert _predict(fat, tmp_path, monkeypatch, FakeEncode(stopped=True)) is None
    assert list((tmp_path / "run").iterdir()) == []


def test_a_failed_section_encode_is_a_sample_error(fat, tmp_path, monkeypatch):
    with pytest.raises(SampleError):
        _predict(fat, tmp_path, monkeypatch, FakeEncode(returncode=1))
    assert list((tmp_path / "run").iterdir()) == []


def test_a_section_that_cannot_be_cut_is_a_sample_error(tmp_path, monkeypatch, fat):
    broken = tmp_path / "broken.mkv"
    broken.write_bytes(b"not a video")
    monkeypatch.setattr(sample, "run_ffmpeg", FakeEncode())
    with pytest.raises(SampleError):
        predict_saving(fat, "high", broken, tmp_path / "run", "ffmpeg", "ffprobe", lambda: False)
