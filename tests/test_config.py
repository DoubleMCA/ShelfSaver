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


def test_host_and_trusted_proxies_default_to_open_and_trust_nobody(tmp_path, monkeypatch):
    monkeypatch.setenv("SHELFSAVER_DATA", str(tmp_path))
    cfg = load_config()
    assert cfg.host == "0.0.0.0" and cfg.trusted_proxies == ""


def test_host_comes_from_the_environment_then_the_file_and_bad_values_are_ignored(tmp_path, monkeypatch):
    monkeypatch.setenv("SHELFSAVER_DATA", str(tmp_path))
    (tmp_path / "config.json").write_text(json.dumps({"host": "127.0.0.1"}))
    assert load_config().host == "127.0.0.1"
    monkeypatch.setenv("SHELFSAVER_HOST", "::1")
    assert load_config().host == "::1"
    # A hostname, a typo or a non-string must not stop the web page from starting.
    for bad in ("localhost", "999.1.1.1", ""):
        monkeypatch.setenv("SHELFSAVER_HOST", bad)
        assert load_config().host == "127.0.0.1"
    monkeypatch.delenv("SHELFSAVER_HOST")
    (tmp_path / "config.json").write_text(json.dumps({"host": 5}))
    assert load_config().host == "0.0.0.0"


def test_trusted_proxies_accept_addresses_networks_and_star_but_reject_the_whole_list_if_one_is_bad(tmp_path, monkeypatch):
    monkeypatch.setenv("SHELFSAVER_DATA", str(tmp_path))
    monkeypatch.setenv("SHELFSAVER_TRUSTED_PROXIES", "172.17.0.1, 10.0.0.0/8")
    assert load_config().trusted_proxies == "172.17.0.1,10.0.0.0/8"
    monkeypatch.setenv("SHELFSAVER_TRUSTED_PROXIES", "*")
    assert load_config().trusted_proxies == "*"
    # One bad entry: ignore the lot, because guessing which proxies were meant is a security call.
    monkeypatch.setenv("SHELFSAVER_TRUSTED_PROXIES", "172.17.0.1, not-an-address")
    assert load_config().trusted_proxies == ""
    monkeypatch.delenv("SHELFSAVER_TRUSTED_PROXIES")
    (tmp_path / "config.json").write_text(json.dumps({"trusted_proxies": "192.168.1.5"}))
    assert load_config().trusted_proxies == "192.168.1.5"
