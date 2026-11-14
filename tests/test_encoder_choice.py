"""Automatic encoder choice: the order, moving up between files, and waiting when nothing works.

`works` is faked (no FFmpeg runs), and so is the clock, so every timing rule is checked exactly.
"""
from __future__ import annotations

import pytest

from shelfsaver import encoders, hostos
from shelfsaver.config import Config
from shelfsaver.encoder_choice import BETTER_RETRY_SECONDS, RETRY_SECONDS, EncoderChoice
from shelfsaver.encoders import Amf, Encoder, Nvenc, Qsv


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class Works:
    """Answers from `ok` (encoder name -> works?) and records every name it was asked about."""

    def __init__(self, **ok):
        self.ok = ok
        self.calls = []

    def __call__(self, encoder):
        self.calls.append(encoder.name)
        return self.ok.get(encoder.name, False)


class LinuxOnly(Encoder):
    """Stands in for the VAAPI encoder until it exists: Linux only, like the real one."""
    name, label, codec = "vaapi", "Stand-in VAAPI", "hevc_vaapi"

    @classmethod
    def supported(cls):
        return not hostos.IS_WINDOWS


@pytest.fixture
def windows(monkeypatch):
    monkeypatch.setattr(hostos, "IS_WINDOWS", True)


@pytest.fixture
def linux(monkeypatch):
    monkeypatch.setattr(hostos, "IS_WINDOWS", False)
    monkeypatch.setitem(encoders.ENCODERS, "vaapi", LinuxOnly)


def choice(works, encoder="auto", clock=None):
    return EncoderChoice(Config(data_dir=".", encoder=encoder), works, clock or Clock())


def test_pinned_never_tests():
    works = Works()
    chosen = choice(works, "qsv")
    assert isinstance(chosen.before_conversion(), Qsv)
    assert chosen.pinned and works.calls == []


def test_auto_takes_the_first_that_works_in_order(windows):
    works = Works(amf=True, qsv=True)
    assert isinstance(choice(works).before_conversion(), Amf)
    assert works.calls == ["nvenc", "amf"]


def test_nothing_works_then_retries_after_a_minute(windows):
    works, clock = Works(), Clock()
    chosen = choice(works, clock=clock)
    assert chosen.before_conversion() is None
    assert works.calls == ["nvenc", "amf", "qsv"]
    clock.now += 30
    assert chosen.before_conversion() is None and len(works.calls) == 3
    clock.now += RETRY_SECONDS - 30 + 1
    works.ok["qsv"] = True
    assert isinstance(chosen.before_conversion(), Qsv)
    assert works.calls[3:] == ["nvenc", "amf", "qsv"]


def test_moves_back_up_when_a_better_encoder_starts_working(windows):
    works, clock = Works(amf=True), Clock()
    chosen = choice(works, clock=clock)
    assert isinstance(chosen.before_conversion(), Amf)   # the NVIDIA driver isn't ready yet
    clock.now += BETTER_RETRY_SECONDS + 1
    works.ok["nvenc"] = True
    assert isinstance(chosen.before_conversion(), Nvenc)
    assert chosen.describe()["name"] == "nvenc"


def test_a_failed_better_encoder_is_retried_at_most_every_ten_minutes(windows):
    works, clock = Works(amf=True), Clock()
    chosen = choice(works, clock=clock)
    chosen.before_conversion()
    asked = len(works.calls)
    for later in (60, 120, 300):
        clock.now = 1000.0 + later
        assert isinstance(chosen.before_conversion(), Amf)
    assert len(works.calls) == asked                    # nvenc not asked again yet
    clock.now = 1000.0 + BETTER_RETRY_SECONDS + 1
    chosen.before_conversion()
    assert works.calls[asked:] == ["nvenc"]             # asked once; amf itself is not retested


def test_the_best_encoder_in_use_is_not_retested(windows):
    works = Works(nvenc=True)
    chosen = choice(works)
    chosen.before_conversion()
    chosen.before_conversion()
    assert works.calls == ["nvenc"]


def test_amf_is_skipped_off_windows(linux):
    works = Works()
    choice(works).before_conversion()
    assert "amf" not in works.calls


def test_linux_prefers_qsv_over_vaapi(linux):
    works = Works(qsv=True, vaapi=True)
    assert isinstance(choice(works).before_conversion(), Qsv)


def test_vaapi_is_skipped_on_windows(windows, monkeypatch):
    monkeypatch.setitem(encoders.ENCODERS, "vaapi", LinuxOnly)
    works = Works()
    choice(works).before_conversion()
    assert "vaapi" not in works.calls


def test_after_failures_redetects_under_auto(windows):
    works = Works(nvenc=True)
    chosen = choice(works)
    assert isinstance(chosen.before_conversion(), Nvenc)
    works.ok = {"qsv": True}
    assert isinstance(chosen.after_failures(), Qsv)
    assert isinstance(chosen.current(), Qsv)


def test_after_failures_finding_nothing_clears_the_choice(windows):
    works = Works(nvenc=True)
    chosen = choice(works)
    chosen.before_conversion()
    works.ok = {}
    assert chosen.after_failures() is None and chosen.current() is None


def test_after_failures_pinned_is_todays_check():
    works = Works()
    chosen = choice(works, "nvenc")
    assert chosen.after_failures() is None
    assert isinstance(chosen.current(), Nvenc)            # pinned stays pinned
    works.ok["nvenc"] = True
    assert isinstance(chosen.after_failures(), Nvenc)


def test_describe_reports_how_it_was_chosen(windows):
    assert choice(Works(), "qsv").describe() == {
        "name": "qsv", "label": Qsv.label, "chosen": "pinned"}
    detected = choice(Works(amf=True))
    detected.before_conversion()
    assert detected.describe() == {"name": "amf", "label": Amf.label, "chosen": "detected"}
    none = choice(Works())
    none.before_conversion()
    assert none.describe() == {"name": None, "label": None, "chosen": "none"}


def test_pinned_encoders_read_their_settings():
    chosen = EncoderChoice(Config(data_dir=".", encoder="qsv", qsv_device="/dev/dri/renderD129"),
                           Works(), Clock())
    assert chosen.current().device == "/dev/dri/renderD129"
