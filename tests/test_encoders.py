"""The encoder layer. The NVENC command must be exactly what it was before encoders were pluggable."""
from __future__ import annotations

import json

import pytest

from shelfsaver import encoders
from shelfsaver.config import Config, load_config
from shelfsaver.encode import build_command, encoder_works
from shelfsaver.encoders import ENCODERS, Encoder, Nvenc, encoder_for
from tests import golden_commands
from tests.helpers import convert, info

# ---- the NVENC command did not change

GOLDEN = json.loads(golden_commands.GOLDEN_FILE.read_text(encoding="utf-8"))


def test_the_recorded_snapshot_covers_the_whole_matrix():
    # If the matrix grows, the snapshot must be regenerated on purpose (see golden_commands.py).
    assert set(GOLDEN) == {name for name, _ in golden_commands.cases()}


@pytest.mark.parametrize("name", sorted(GOLDEN))
def test_nvenc_command_is_unchanged(name):
    kwargs = dict(golden_commands.cases())[name]
    assert [str(a) for a in build_command(**kwargs)] == GOLDEN[name]


def test_passing_nvenc_explicitly_changes_nothing():
    name, kwargs = next(iter(golden_commands.cases()))
    assert build_command(**kwargs, encoder=Nvenc()) == build_command(**kwargs)


# ---- the interface an encoder must meet

class FakeEncoder(Encoder):
    """Stands in for a second encoder, to prove build_command takes *everything* from it."""

    name, label, codec = "fake", "Fake", "hevc_fake"

    def input_args(self):
        return ["-init_hw_device", "fake=hw"]

    def codec_args(self, stream, level, deep):
        return [f"-c:v:{stream}", self.codec, "-q", level, "-deep", str(deep)]

    def filters(self, deinterlace):
        return ["hwupload", "deint"] if deinterlace else ["hwupload"]

    def self_test_args(self):
        return ["-frames:v", "1", "-c:v", self.codec]


def _kwargs(**media):
    return dict(info=info(**media), decision=convert(), level="high", source="in.mkv",
                output="out.mkv", ffmpeg="ffmpeg")


def test_hardware_setup_comes_before_the_input():
    cmd = build_command(**_kwargs(), encoder=FakeEncoder())
    assert cmd[cmd.index("-init_hw_device") + 1] == "fake=hw"
    assert cmd.index("-init_hw_device") < cmd.index("-i")


def test_the_encoder_supplies_codec_options_and_filters_and_nothing_nvenc_leaks_in():
    cmd = build_command(**_kwargs(bit_depth=10, field_order="tt"), encoder=FakeEncoder())
    assert "hevc_fake" in cmd and "-deep" in cmd and cmd[cmd.index("-deep") + 1] == "True"
    assert cmd[cmd.index("-filter:v:0") + 1] == "hwupload,deint"
    assert not any("nvenc" in str(a) for a in cmd)


def test_no_filter_argument_when_the_encoder_wants_none():
    class Plain(FakeEncoder):
        def filters(self, deinterlace):
            return []

    assert "-filter:v:0" not in build_command(**_kwargs(field_order="tt"), encoder=Plain())


def test_the_self_test_uses_the_encoders_own_arguments(monkeypatch):
    seen = []

    class Recorder:
        def __init__(self, cmd, **kwargs):
            seen.append(cmd)
            self.returncode = 0

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr("shelfsaver.encode.subprocess.Popen", Recorder)
    monkeypatch.setattr("shelfsaver.encode.kill_with_app", lambda proc: None)
    assert encoder_works("ffmpeg", FakeEncoder()) is True
    cmd = seen[0]
    assert cmd.index("-init_hw_device") < cmd.index("-i")          # device set up first
    # ...then the encoder's own one-frame test, then discard the output.
    assert cmd[cmd.index("-frames:v"):] == ["-frames:v", "1", "-c:v", "hevc_fake", "-f", "null", "-"]


# ---- NVENC's diagnostics

def test_nvenc_load_is_the_busier_of_the_card_and_its_encoder():
    assert Nvenc().usage_percent(lambda args: "7, 93\n") == 93
    assert Nvenc().usage_percent(lambda args: "41, [N/A]\n") == 41
    assert Nvenc().usage_percent(lambda args: None) is None


def test_nvenc_device_info_names_the_card_and_driver():
    info_ = Nvenc().device_info(lambda args: "NVIDIA GeForce RTX 4070, 551.23\n")
    assert info_ == {"available": True, "name": "NVIDIA GeForce RTX 4070", "driver": "551.23"}
    assert Nvenc().device_info(lambda args: None)["available"] is False


# ---- choosing an encoder

def test_nvenc_is_the_default_and_registered():
    assert encoders.DEFAULT_NAME == "nvenc" and ENCODERS["nvenc"] is Nvenc
    assert isinstance(encoder_for(Config(data_dir=".")), Nvenc)


def test_an_unknown_encoder_name_falls_back_to_the_default():
    assert isinstance(encoder_for(Config(data_dir=".", encoder="nonsense")), Nvenc)


def test_every_registered_encoder_names_itself_consistently():
    for key, cls in ENCODERS.items():
        enc = cls()
        assert enc.name == key and enc.label and enc.codec


def test_the_encoder_setting_comes_from_the_environment_then_the_file(tmp_path, monkeypatch):
    monkeypatch.setenv("SHELFSAVER_DATA", str(tmp_path))
    monkeypatch.delenv("SHELFSAVER_ENCODER", raising=False)
    assert load_config().encoder == "nvenc"
    (tmp_path / "config.json").write_text(json.dumps({"encoder": "nvenc"}))
    assert load_config().encoder == "nvenc"
    monkeypatch.setenv("SHELFSAVER_ENCODER", "  NVENC ")  # case and spaces do not matter
    assert load_config().encoder == "nvenc"
    monkeypatch.setenv("SHELFSAVER_ENCODER", "banana")      # unknown: ignored, not fatal
    assert load_config().encoder == "nvenc"
    (tmp_path / "config.json").write_text(json.dumps({"encoder": 5}))
    assert load_config().encoder == "nvenc"
