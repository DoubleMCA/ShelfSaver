import json
from pathlib import Path

from shelfsaver.config import load_config


def test_reads_tool_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("SHELFSAVER_DATA", str(tmp_path))
    (tmp_path / "config.json").write_text(json.dumps({"ffmpeg": "X:/f.exe", "other": 1}))
    assert load_config().ffmpeg == "X:/f.exe"


def test_unreadable_data_folder_gives_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("SHELFSAVER_DATA", str(tmp_path))

    def denied(self):
        raise PermissionError("Access is denied")

    monkeypatch.setattr(Path, "is_file", denied)
    cfg = load_config()
    assert cfg.ffmpeg == "ffmpeg" and cfg.ffprobe == "ffprobe"


def test_broken_json_gives_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("SHELFSAVER_DATA", str(tmp_path))
    (tmp_path / "config.json").write_text("{not json")
    assert load_config().ffmpeg == "ffmpeg"


def test_port_comes_from_the_environment_then_the_file_and_bad_values_are_ignored(tmp_path, monkeypatch):
    monkeypatch.setenv("SHELFSAVER_DATA", str(tmp_path))
    monkeypatch.delenv("SHELFSAVER_PORT", raising=False)
    assert load_config().port == 8642
    (tmp_path / "config.json").write_text(json.dumps({"port": 9000}))
    assert load_config().port == 9000
    monkeypatch.setenv("SHELFSAVER_PORT", "9100")
    assert load_config().port == 9100
    for bad in ("0", "70000", "abc", "-1", ""):
        monkeypatch.setenv("SHELFSAVER_PORT", bad)
        assert load_config().port == 9000  # falls back to the file's value
    (tmp_path / "config.json").write_text(json.dumps({"port": True}))
    monkeypatch.delenv("SHELFSAVER_PORT")
    assert load_config().port == 8642  # True is not a port, even though True == 1
