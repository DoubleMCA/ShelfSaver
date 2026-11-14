"""AMD AMF: the command it builds on Windows, and that it stays out of the way elsewhere.

Only the text of the command is checked here. Whether it encodes well on a real Radeon card is
measured separately (see the comment above `Amf.QUALITY`).
"""
from __future__ import annotations

import pytest

from shelfsaver import hostos
from shelfsaver.config import Config
from shelfsaver.encoders import ENCODERS, Amf, Nvenc, encoder_for
from shelfsaver.quality import LEVELS


def test_amf_is_registered_and_named():
    assert ENCODERS["amf"] is Amf
    assert Amf.name == "amf" and Amf.codec == "hevc_amf" and Amf.label == "AMD AMF"
    assert isinstance(encoder_for(Config(data_dir=".", encoder="amf")), Amf)


def test_amf_8bit_args():
    q = str(Amf.QUALITY["medium"])
    assert Amf().codec_args(0, "medium", False) == [
        "-c:v:0", "hevc_amf", "-usage", "transcoding", "-quality", "quality",
        "-rc", "cqp", "-qp_i", q, "-qp_p", q,
        "-pix_fmt:v:0", "nv12", "-profile:v:0", "main"]


def test_amf_10bit_args():
    args = Amf().codec_args(1, "low", True)
    assert args[:2] == ["-c:v:1", "hevc_amf"]
    assert args[-4:] == ["-pix_fmt:v:1", "p010le", "-profile:v:1", "main10"]


def test_amf_levels_go_from_best_to_smallest():
    assert Amf.QUALITY["low"] < Amf.QUALITY["medium"] < Amf.QUALITY["high"]
    assert set(Amf.QUALITY) == set(LEVELS)
    assert all(0 <= v <= 51 for v in Amf.QUALITY.values())  # -qp_i / -qp_p are 0 to 51


def test_amf_needs_no_device_setup_and_uses_bwdif():
    assert Amf().input_args() == []
    assert Amf().filters(True) == ["bwdif"]
    assert Amf().filters(False) == []


def test_amf_self_test_names_a_pixel_format():
    assert Amf().self_test_args() == ["-frames:v", "1", "-c:v", "hevc_amf", "-pix_fmt", "nv12"]


@pytest.mark.parametrize("windows", [True, False])
def test_amf_supported_only_on_windows(monkeypatch, windows):
    monkeypatch.setattr(hostos, "IS_WINDOWS", windows)
    assert Amf.supported() is windows
    assert Nvenc.supported() is True


def test_amf_usage_is_unknown():
    assert Amf().usage_percent(lambda cmd: "x") is None


def test_amf_device_info_on_windows_does_not_start_anything():
    def run_tool(cmd):
        raise AssertionError("device_info must not start a program")
    assert Amf().device_info(run_tool)["available"] is None
