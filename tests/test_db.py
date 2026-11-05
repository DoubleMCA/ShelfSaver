import pytest

from shelfsaver.db import Database
from shelfsaver.logs import tail_log


@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / "shelfsaver.db")


@pytest.fixture
def lib(db):
    return db.add_library("Movies", "D:\\Movies", "balanced", True)


def test_next_queued_returns_largest_first(db, lib):
    db.upsert_file(lib.id, "a.mkv", 100, 1.0, "queued")
    db.upsert_file(lib.id, "b.mkv", 900, 1.0, "queued")
    assert db.next_queued().path == "b.mkv"


def test_next_queued_can_skip_libraries(db, lib):
    other = db.add_library("TV", "D:\\TV", "balanced", True)
    db.upsert_file(lib.id, "a.mkv", 100, 1.0, "queued")
    db.upsert_file(other.id, "b.mkv", 900, 1.0, "queued")
    assert db.next_queued([other.id]).path == "a.mkv"
    assert db.next_queued([lib.id, other.id]) is None


def test_files_with_status(db, lib):
    a = db.upsert_file(lib.id, "a.mkv", 100, 1.0, "working")
    db.upsert_file(lib.id, "b.mkv", 100, 1.0, "queued")
    assert [f.id for f in db.files_with_status("working")] == [a.id]


def test_record_converted_moves_the_row_and_replaces_any_row_at_the_new_path(db, lib):
    f = db.upsert_file(lib.id, "a.avi", 100, 1.0, "working", codec="h264")
    stale = db.upsert_file(lib.id, "a.mkv", 5, 1.0, "missing")
    db.record_converted(f.id, "a.mkv", 40, 2.0, "hevc", "mkv", "640x360")
    row = db.get_file(f.id)
    assert (row.path, row.size, row.mtime, row.status) == ("a.mkv", 40, 2.0, "converted")
    assert (row.codec, row.container, row.resolution) == ("hevc", "mkv", "640x360")
    assert db.get_file(stale.id) is None and db.get_file_by_path("a.avi") is None


def test_stats_survive_library_removal(db, lib):
    f = db.upsert_file(lib.id, "a.mkv", 100, 1.0, "converted")
    r = db.start_run(f.id, 100, "H.264 MKV", 8_000_000, "why")
    db.update_run(r.id, state="done", saved_bytes=60)
    db.remove_library(lib.id)
    assert db.stats()["saved_bytes"] == 60
    assert db.get_file_by_path("a.mkv") is None
    assert len(db.history()) == 1


def test_settings_round_trip_and_default(db):
    assert db.get_setting("scratch_dir") is None
    db.set_setting("scratch_dir", "E:\\work")
    assert db.get_setting("scratch_dir") == "E:\\work"


def test_tail_log_filters_by_text(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "shelfsaver.log").write_text(
        "... start ...\n... boom ...\n... done ...\n", encoding="utf-8"
    )
    assert tail_log(tmp_path, 10, contains="boom") == ["... boom ..."]
    assert tail_log(tmp_path, 2) == ["... boom ...", "... done ..."]


def test_upsert_file_updates_existing_path(db, lib):
    a = db.upsert_file(lib.id, "a.mkv", 100, 1.0, "queued")
    b = db.upsert_file(lib.id, "a.mkv", 200, 2.0, "skipped", reason="small")
    assert a.id == b.id
    assert (b.size, b.status, b.reason) == (200, "skipped", "small")


def test_requeue_and_status(db, lib):
    f = db.upsert_file(lib.id, "a.mkv", 100, 1.0, "queued")
    db.set_file_status(f.id, "failed", "boom")
    assert db.get_file(f.id).status == "failed"
    assert db.next_queued() is None
    db.requeue([f.id])
    assert db.get_file(f.id).status == "queued"
    assert db.get_file(f.id).reason is None


def test_active_runs_and_details(db, lib):
    f = db.upsert_file(lib.id, "a.mkv", 100, 1.0, "working")
    r = db.start_run(f.id, 100, "H.264 MKV", 1000, "why")
    assert [x.id for x in db.active_runs(["running"])] == [r.id]
    db.update_run(r.id, state="done", saved_bytes=5)
    assert db.active_runs(["running", "swapping"]) == []
    details = db.file_details(f.id)
    assert details["file"]["path"] == "a.mkv"
    assert details["runs"][0]["saved_bytes"] == 5


def test_library_update_and_list(db, lib):
    db.update_library(lib.id, status="offline", english_only=False)
    got = db.list_libraries()[0]
    assert got.status == "offline" and got.english_only is False


def test_config_env_and_overrides(tmp_path, monkeypatch):
    from shelfsaver.config import load_config

    monkeypatch.setenv("SHELFSAVER_DATA", str(tmp_path))
    assert load_config().ffmpeg == "ffmpeg"
    (tmp_path / "config.json").write_text('{"ffmpeg": "x/ff.exe"}', encoding="utf-8")
    cfg = load_config()
    assert cfg.data_dir == tmp_path and cfg.ffmpeg == "x/ff.exe" and cfg.ffprobe == "ffprobe"


def test_holding_enabled_defaults_on_until_set(db):
    assert db.get_setting("holding_enabled") == "1"
    db.set_setting("holding_enabled", "0")
    assert db.get_setting("holding_enabled") == "0"
    assert db.get_setting("scratch_dir") is None
