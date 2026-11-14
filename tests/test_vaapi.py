"""AMD on Linux through VAAPI: the command it builds, and that it stays out of the way on Windows.

Only the text of the command is checked here. Whether it encodes well on a real Radeon card under
Linux is measured separately (see the comment above `Vaapi.QUALITY`).
"""
from __future__ import annotations

import json

import pytest

from shelfsaver import hostos
from shelfsaver.config import Config, load_config
from shelfsaver.encode import build_command
from shelfsaver.encoders import ENCODERS, Vaapi, encoder_for
from shelfsaver.quality import LEVELS
from tests.helpers import convert, info


def test_vaapi_is_registered_and_linux_only(monkeypatch):
    assert ENCODERS["vaapi"] is Vaapi
    assert Vaapi.codec == "hevc_vaapi" and Vaapi.label == "AMD (VAAPI)"
    monkeypatch.setattr(hostos, "IS_WINDOWS", True)
    assert Vaapi.supported() is False
    monkeypatch.setattr(hostos, "IS_WINDOWS", False)
    assert Vaapi.supported() is True


def test_vaapi_starts_its_device():
    assert Vaapi("/dev/dri/renderD129").input_args() == [
        "-init_hw_device", "vaapi=va:/dev/dri/renderD129", "-filter_hw_device", "va"]
    assert Vaapi().input_args()[1] == "vaapi=va:/dev/dri/renderD128"


def test_vaapi_8bit_uploads_nv12():
    q = str(Vaapi.QUALITY["medium"])
    assert Vaapi().filters(False, False) == ["format=nv12", "hwupload"]
    args = Vaapi().codec_args(0, "medium", False)
    assert args[:2] == ["-c:v:0", "hevc_vaapi"]
    assert ["-rc_mode", "CQP", "-qp", q] == args[args.index("-rc_mode"):args.index("-rc_mode") + 4]
    assert args[-2:] == ["-profile:v:0", "main"]
    assert not any(a.startswith("-pix_fmt") for a in args)  # the frames are already on the GPU


def test_vaapi_10bit_uploads_p010_and_uses_main10():
    assert Vaapi().filters(False, True) == ["format=p010", "hwupload"]
    assert Vaapi().codec_args(0, "low", True)[-2:] == ["-profile:v:0", "main10"]


def test_vaapi_deinterlaces_on_the_cpu_first():
    assert Vaapi().filters(True, False) == ["bwdif", "format=nv12", "hwupload"]


def test_vaapi_self_test_uploads_a_frame():
    args = Vaapi().self_test_args()
    assert args[args.index("-vf") + 1] == "format=nv12,hwupload"
    assert args[args.index("-c:v") + 1] == "hevc_vaapi"


def test_vaapi_levels_go_from_best_to_smallest():
    assert Vaapi.QUALITY["low"] < Vaapi.QUALITY["medium"] < Vaapi.QUALITY["high"]
    assert set(Vaapi.QUALITY) == set(LEVELS)


def test_the_built_command_uploads_the_main_video(monkeypatch):
    monkeypatch.setattr(hostos, "IS_WINDOWS", False)
    cmd = build_command(info(bit_depth=10), convert(), "medium", "in.mkv", "out.mkv", "ffmpeg",
                        Vaapi())
    assert cmd[cmd.index("-init_hw_device") + 1] == "vaapi=va:/dev/dri/renderD128"
    assert cmd.index("-init_hw_device") < cmd.index("-i")
    assert any(a.startswith("-filter:v:") for a in cmd)
    assert "format=p010,hwupload" in cmd


def test_vaapi_device_setting_from_file_and_env(tmp_path, monkeypatch):
    monkeypatch.setenv("SHELFSAVER_DATA", str(tmp_path))
    (tmp_path / "config.json").write_text(json.dumps({"vaapi_device": "/dev/dri/renderD129"}))
    assert load_config().vaapi_device == "/dev/dri/renderD129"
    monkeypatch.setenv("SHELFSAVER_VAAPI_DEVICE", "/dev/dri/renderD130")
    cfg = load_config()
    assert cfg.vaapi_device == "/dev/dri/renderD130"
    cfg.encoder = "vaapi"
    assert encoder_for(cfg).device == "/dev/dri/renderD130"


def test_vaapi_usage_is_unknown():
    assert Vaapi().usage_percent(lambda cmd: "x") is None


@pytest.mark.parametrize("encoder_name", ["nvenc", "qsv", "amf"])
def test_other_encoders_ignore_bit_depth_in_their_filters(encoder_name):
    enc = ENCODERS[encoder_name]()
    assert enc.filters(True, True) == enc.filters(True, False) == ["bwdif"]
    assert enc.filters(False, True) == []
