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
