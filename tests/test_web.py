"""The web API. Fake worker and scanner for route tests, real ones for one integration test."""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from shelfsaver import swap
from shelfsaver.config import Config
from shelfsaver.db import Database, ensure_mcp_token
from shelfsaver.scanner import Scanner
from shelfsaver.web.app import SESSION_COOKIE, create_app
from shelfsaver.worker import Worker
from tests.helpers import make_video

PASSWORD = "correct horse battery"


class FakeWorker:
    def __init__(self):
        self.calls = []
        self.paused = False
        self.resolve_result = "resolved"

    def status(self):
        if self.paused:
            return {"state": "paused", "message": None, "file": None, "step": None,
                    "percent": None, "eta_seconds": None}
        return {"state": "idle", "message": None, "file": None, "step": None,
                "percent": None, "eta_seconds": None}

    def pause(self, reason=None):
        self.paused = True
        self.calls.append("pause")

    def resume(self):
        self.paused = False
        self.calls.append("resume")

    def retry(self, file_id):
        self.calls.append(("retry", file_id))

    def resolve_swap(self, file_id):
        self.calls.append(("resolve", file_id))
        return self.resolve_result


class FakeScanner:
    def __init__(self):
        self.calls = []
        self.happened = threading.Event()

    def _note(self, *call):
        self.calls.append(call)
        self.happened.set()

    def scan_library(self, library_id):
        self._note("scan", library_id)

    def refresh_watches(self):
        self.calls.append(("watches",))

    def reevaluate(self, library_id):
        self._note("reevaluate", library_id)

    def wait(self, timeout=5):
        assert self.happened.wait(timeout), "the scanner was never called"
        self.happened.clear()


@pytest.fixture
def parts(tmp_path):
    db = Database(tmp_path / "data" / "shelfsaver.db")
    return db, FakeWorker(), FakeScanner(), Config(data_dir=tmp_path / "data")


@pytest.fixture
def client(parts):
    db, worker, scanner, config = parts
    with TestClient(create_app(db, worker, scanner, config)) as c:
        yield c


@pytest.fixture
def authed(client):
    assert client.post("/api/auth/setup", json={"password": PASSWORD}).status_code == 200
    return client


def make_folder(tmp_path, name):
    folder = tmp_path / name
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def add_library(authed, folder, name="Movies", **extra):
    return authed.post("/api/libraries", json={
        "name": name, "path": str(folder), "level": "medium", **extra})


# ---- sign-in

def test_everything_requires_sign_in(client):
    assert client.get("/api/status").status_code == 401
    for method, url in [("get", "/api/history"), ("get", "/api/libraries"),
                        ("get", "/api/settings"), ("get", "/api/browse"),
                        ("post", "/api/pause"), ("post", "/api/holding/clear"),
                        ("post", "/api/files/1/retry"), ("post", "/api/files/1/resolve")]:
        assert getattr(client, method)(url).status_code == 401, url


def test_first_visit_sets_password_once(client):
    assert client.get("/api/auth/state").json() == {"has_password": False, "signed_in": False}
    assert client.post("/api/auth/setup", json={"password": PASSWORD}).status_code == 200
    state = client.get("/api/auth/state").json()
    assert state == {"has_password": True, "signed_in": True}
    again = client.post("/api/auth/setup", json={"password": "something else entirely"})
    assert again.status_code == 409
    client.post("/api/auth/logout")
    assert client.post("/api/auth/login", json={"password": "something else entirely"}
                       ).status_code == 401
    assert client.post("/api/auth/login", json={"password": PASSWORD}).status_code == 200


def test_short_password_refused(client):
    r = client.post("/api/auth/setup", json={"password": "short"})
    assert r.status_code == 400
    assert "8" in r.json()["detail"]
    assert client.get("/api/auth/state").json()["has_password"] is False


def test_session_cookie_flags_and_logout(client, parts):
    r = client.post("/api/auth/setup", json={"password": PASSWORD})
    header = r.headers["set-cookie"].lower()
    assert SESSION_COOKIE in header and "httponly" in header and "samesite=strict" in header
    assert client.get("/api/status").status_code == 200
    client.post("/api/auth/logout")
    assert client.get("/api/status").status_code == 401


def test_sessions_survive_restart_and_expire(parts, tmp_path):
    db, worker, scanner, config = parts
    with TestClient(create_app(db, worker, scanner, config)) as first:
        first.post("/api/auth/setup", json={"password": PASSWORD})
        token = first.cookies.get(SESSION_COOKIE)
    db2 = Database(db.path)
    with TestClient(create_app(db2, worker, scanner, config)) as second:
        second.cookies.set(SESSION_COOKIE, token)
        assert second.get("/api/status").status_code == 200
    # Expire every session in the database.
    db2._exec("UPDATE sessions SET expires_at=?", (time.time() - 1,))
    with TestClient(create_app(db2, worker, scanner, config)) as third:
        third.cookies.set(SESSION_COOKIE, token)
        assert third.get("/api/status").status_code == 401


def test_wrong_password_rejected_and_rate_limited(client):
    client.post("/api/auth/setup", json={"password": PASSWORD})
    client.post("/api/auth/logout")
    for _ in range(5):
        assert client.post("/api/auth/login", json={"password": "nope nope nope"}
                           ).status_code == 401
    blocked = client.post("/api/auth/login", json={"password": "nope nope nope"})
    assert blocked.status_code == 429
    # Even the right password waits out the minute.
    assert client.post("/api/auth/login", json={"password": PASSWORD}).status_code == 429


def test_password_and_tokens_never_appear_in_responses(authed, parts):
    db, *_ = parts
    stored = db.get_setting("password_hash")
    token = authed.cookies.get(SESSION_COOKIE)
    bodies = [authed.get(u).text for u in (
        "/api/auth/state", "/api/status", "/api/settings", "/api/libraries", "/api/history")]
    for body in bodies:
        assert stored not in body and PASSWORD not in body and token not in body
    assert "password_hash" not in authed.get("/api/settings").json()


# ---- libraries

def test_add_library_queues_a_scan(authed, parts, tmp_path):
    _, _, scanner, _ = parts
    folder = make_folder(tmp_path, "movies")
    r = add_library(authed, folder, name="  Movies  ")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == "Movies" and body["level"] == "medium" and body["english_only"] is True
    scanner.wait()
    assert ("scan", body["id"]) in scanner.calls
    assert ("watches",) in scanner.calls
    listed = authed.get("/api/libraries").json()
    assert [l["id"] for l in listed] == [body["id"]]


@pytest.mark.parametrize("bad", ["\\\\nas\\movies", "Z:\\does\\not\\exist"])
def test_add_library_rejects_with_plain_message(authed, bad):
    r = authed.post("/api/libraries", json={"name": "X", "path": bad, "level": "low"})
    assert r.status_code == 400
    assert authed.get("/api/libraries").json() == []
    assert len(r.json()["detail"]) > 10


def test_add_library_rejects_a_whole_drive(authed, tmp_path):
    root = os.path.splitdrive(str(tmp_path))[0] + "\\"
    r = authed.post("/api/libraries", json={"name": "All", "path": root, "level": "low"})
    assert r.status_code == 400
    assert r.json()["detail"] == "Choose a folder on the drive, not the whole drive."


def test_add_library_rejects_bad_name_level_and_file(authed, tmp_path):
    folder = make_folder(tmp_path, "movies")
    assert authed.post("/api/libraries", json={"name": "   ", "path": str(folder),
                                              "level": "low"}).status_code == 400
    assert authed.post("/api/libraries", json={"name": "A", "path": str(folder),
                                              "level": "extreme"}).status_code == 400
    afile = folder / "a.txt"
    afile.write_text("x")
    assert authed.post("/api/libraries", json={"name": "A", "path": str(afile),
                                              "level": "low"}).status_code == 400
    assert authed.post("/api/libraries", json={"name": "A"}).status_code == 400


def test_overlapping_libraries_rejected(authed, tmp_path):
    outer = make_folder(tmp_path, "movies")
    inner = make_folder(tmp_path, "movies/kids")
    assert add_library(authed, outer).status_code == 200
    assert add_library(authed, inner, name="Kids").status_code == 400
    assert add_library(authed, outer, name="Again").status_code == 400
    other = make_folder(tmp_path, "movies2")  # a sibling that shares a name prefix is fine
    assert add_library(authed, other, name="Sibling").status_code == 200
    third = make_folder(tmp_path, "shows")
    assert add_library(authed, third, name="Shows").status_code == 200
    assert add_library(authed, tmp_path, name="Parent").status_code == 400


def test_library_cannot_be_the_scratch_folder(authed, tmp_path):
    scratch = make_folder(tmp_path, "scratch")
    assert authed.patch("/api/settings", json={"scratch_dir": str(scratch)}).status_code == 200
    r = add_library(authed, scratch)
    assert r.status_code == 400 and "scratch" in r.json()["detail"].lower()


def test_scratch_inside_library_rejected(authed, tmp_path):
    library = make_folder(tmp_path, "movies")
    inside = make_folder(tmp_path, "movies/tmp")
    assert add_library(authed, library).status_code == 200
    r = authed.patch("/api/settings", json={"scratch_dir": str(inside)})
    assert r.status_code == 400
    assert authed.get("/api/settings").json()["scratch_dir"] in (None, "")
    r = authed.patch("/api/settings", json={"scratch_dir": str(tmp_path / "nope")})
    assert r.status_code == 400


def test_scratch_must_be_a_writable_folder(authed, tmp_path):
    afile = tmp_path / "file.txt"
    afile.write_text("x")
    assert authed.patch("/api/settings", json={"scratch_dir": str(afile)}).status_code == 400
    assert authed.patch("/api/settings", json={"scratch_dir": "\\\\nas\\scratch"}
                        ).status_code == 400
    good = make_folder(tmp_path, "scratch")
    r = authed.patch("/api/settings", json={"scratch_dir": str(good), "holding_enabled": False})
    assert r.status_code == 200
    s = authed.get("/api/settings").json()
    assert s["scratch_dir"] == str(good) and s["holding_enabled"] is False
    assert list(good.iterdir()) == []  # the writable check leaves nothing behind


def test_scratch_change_refused_while_converting(authed, parts, tmp_path):
    db, *_ = parts
    lib = db.add_library("M", str(make_folder(tmp_path, "movies")), "low", True)
    f = db.upsert_file(lib.id, str(tmp_path / "movies" / "a.mkv"), 1, 1.0, "working")
    db.start_run(f.id, 1, "H.264", None, "why")
    r = authed.patch("/api/settings", json={"scratch_dir": str(make_folder(tmp_path, "s"))})
    assert r.status_code == 400


def test_level_change_triggers_reevaluate(authed, parts, tmp_path):
    _, _, scanner, _ = parts
    lib = add_library(authed, make_folder(tmp_path, "movies")).json()
    scanner.wait()
    r = authed.patch(f"/api/libraries/{lib['id']}", json={"level": "high"})
    assert r.status_code == 200 and r.json()["level"] == "high"
    scanner.wait()
    assert ("reevaluate", lib["id"]) in scanner.calls
    scanner.calls.clear()
    authed.patch(f"/api/libraries/{lib['id']}", json={"english_only": False})
    scanner.wait()
    assert ("reevaluate", lib["id"]) in scanner.calls
    scanner.calls.clear()
    authed.patch(f"/api/libraries/{lib['id']}", json={"name": "Renamed"})
    time.sleep(0.1)
    assert scanner.calls == []
    assert authed.patch(f"/api/libraries/{lib['id']}", json={"level": "x"}).status_code == 400
    assert authed.patch("/api/libraries/999", json={"level": "low"}).status_code == 404


def test_removing_library_leaves_files_alone(authed, parts, tmp_path):
    db, _, scanner, _ = parts
    folder = make_folder(tmp_path, "movies")
    video = folder / "a.mkv"
    video.write_bytes(b"data")
    lib = add_library(authed, folder).json()
    scanner.wait()
    db.upsert_file(lib["id"], str(video), 4, 1.0, "queued")
    scanner.calls.clear()
    assert authed.delete(f"/api/libraries/{lib['id']}").status_code == 200
    assert video.read_bytes() == b"data"
    assert authed.get("/api/libraries").json() == []
    assert db.get_file_by_path(str(video)) is None
    assert ("watches",) in scanner.calls
    assert authed.delete(f"/api/libraries/{lib['id']}").status_code == 404


@pytest.mark.parametrize("state", ["running", "swapping", "waiting_in_use"])
def test_cannot_remove_library_while_a_file_is_being_finished(authed, parts, tmp_path, state):
    db, _, scanner, _ = parts
    lib = add_library(authed, make_folder(tmp_path, "movies")).json()
    scanner.wait()
    f = db.upsert_file(lib["id"], str(tmp_path / "movies" / "a.mkv"), 1, 1.0, "working")
    run = db.start_run(f.id, 1, "H.264", None, "why")
    db.update_run(run.id, state=state)
    r = authed.delete(f"/api/libraries/{lib['id']}")
    assert r.status_code == 400 and "still being finished" in r.json()["detail"]
    assert len(authed.get("/api/libraries").json()) == 1


# ---- browse

def test_browse_lists_folders_not_files(authed, tmp_path):
    base = make_folder(tmp_path, "base")
    make_folder(base, "b_folder")
    make_folder(base, "A_folder")
    (base / "file.mkv").write_bytes(b"x")
    r = authed.get("/api/browse", params={"path": str(base)})
    assert r.status_code == 200
    body = r.json()
    assert [f["name"] for f in body["folders"]] == ["A_folder", "b_folder"]
    assert body["folders"][0]["path"] == str(base / "A_folder")
    assert body["parent"] == str(tmp_path)


def test_browse_without_a_path_shows_drives(authed, tmp_path):
    body = authed.get("/api/browse").json()
    drive = os.path.splitdrive(str(tmp_path))[0].upper() + "\\"
    assert drive in [f["path"].upper() for f in body["folders"]]
    assert body["path"] is None and body["parent"] is None


def test_browse_skips_folders_it_cannot_read_and_rejects_bad_paths(authed, tmp_path, monkeypatch):
    base = make_folder(tmp_path, "base")
    make_folder(base, "ok")
    make_folder(base, "locked")
    import shelfsaver.web.app as app_module

    original = app_module._is_visible_folder

    def flaky(entry):
        if entry.name == "locked":
            raise PermissionError("denied")
        return original(entry)

    monkeypatch.setattr(app_module, "_is_visible_folder", flaky)
    names = [f["name"] for f in authed.get("/api/browse", params={"path": str(base)}
                                           ).json()["folders"]]
    assert names == ["ok"]
    assert authed.get("/api/browse", params={"path": str(tmp_path / "missing")}
                      ).status_code == 400
    assert authed.get("/api/browse", params={"path": "\\\\nas\\share"}).status_code == 400
    assert authed.get("/api/browse", params={"path": "relative"}).status_code == 400


# ---- history, files, status

def seed_run(db, tmp_path, state="done", file_status="converted", reason=None):
    lib = db.add_library("Movies", str(tmp_path / "movies"), "medium", True)
    f = db.upsert_file(lib.id, str(tmp_path / "movies" / "Film.mkv"), 500, 1.0, file_status,
                       reason)
    run = db.start_run(f.id, 1000, "H.264", 4000, "Big and old")
    db.update_run(run.id, state=state, now_size=500, now_format="x265", saved_bytes=500,
                  swap_json='{"original": "x"}', tracks_json='[{"kind": "audio"}]',
                  checks_json='{"length": true}', error="boom" if state == "failed" else None)
    return lib, f, run


def test_history_shape_has_was_now_saved(authed, parts, tmp_path):
    db, *_ = parts
    lib, f, run = seed_run(db, tmp_path)
    rows = authed.get("/api/history").json()
    assert len(rows) == 1
    row = rows[0]
    assert row["file_id"] == f.id and row["name"] == "Film.mkv"
    assert row["library"] == "Movies"
    assert row["was"] == {"size": 1000, "format": "H.264", "bitrate": 4000}
    assert row["now"]["size"] == 500 and row["now"]["format"] == "x265"
    assert row["saved_bytes"] == 500 and row["status"] == "converted"
    assert "swap_json" not in row
    assert authed.get("/api/history", params={"library_id": lib.id + 1}).json() == []
    assert authed.get("/api/history", params={"limit": 0}).status_code == 400


def test_history_does_not_call_interrupted_runs_failures(authed, parts, tmp_path):
    db, *_ = parts
    lib, f, run = seed_run(db, tmp_path, state="interrupted", file_status="queued")
    db.update_run(run.id, error="Interrupted by a restart, so it will start again")
    row = authed.get("/api/history").json()[0]
    assert row["state"] == "interrupted" and row["status"] == "queued"
    assert row["reason"] is None


def test_history_uses_the_files_reason(authed, parts, tmp_path):
    db, *_ = parts
    seed_run(db, tmp_path, state="failed", file_status="failed", reason="The conversion failed")
    row = authed.get("/api/history").json()[0]
    assert row["status"] == "failed" and row["reason"] == "The conversion failed"


def test_file_details_hides_internal_fields(authed, parts, tmp_path):
    db, *_ = parts
    lib, f, run = seed_run(db, tmp_path)
    body = authed.get(f"/api/files/{f.id}").json()
    assert body["file"]["id"] == f.id and body["file"]["path"].endswith("Film.mkv")
    assert body["runs"][0]["tracks"] == [{"kind": "audio"}]
    assert body["runs"][0]["checks"] == {"length": True}
    assert "swap_json" not in body["runs"][0]
    assert authed.get("/api/files/9999").status_code == 404


def test_retry_goes_through_the_worker(authed, parts, tmp_path):
    db, worker, *_ = parts
    lib, f, run = seed_run(db, tmp_path, state="failed", file_status="failed", reason="x")
    assert authed.post(f"/api/files/{f.id}/retry").status_code == 200
    assert worker.calls == [("retry", f.id)]
    assert db.get_file(f.id).status == "failed"  # the worker decides, not the route
    assert authed.post("/api/files/9999/retry").status_code == 404


def test_retry_refused_for_a_file_that_is_not_stuck(authed, parts, tmp_path):
    db, worker, *_ = parts
    lib, f, run = seed_run(db, tmp_path)
    r = authed.post(f"/api/files/{f.id}/retry")
    assert r.status_code == 400 and worker.calls == []


def test_resolve_needs_confirm_and_a_kept_swap(authed, parts, tmp_path):
    db, worker, *_ = parts
    lib, f, run = seed_run(db, tmp_path, state="swapping", file_status="failed", reason="Kept")
    assert authed.post(f"/api/files/{f.id}/resolve", json={}).status_code == 400
    assert authed.post(f"/api/files/{f.id}/resolve", json={"confirm": "yes"}).status_code == 400
    assert authed.post(f"/api/files/{f.id}/resolve").status_code == 400
    assert worker.calls == []
    assert authed.post(f"/api/files/{f.id}/resolve", json={"confirm": True}).status_code == 200
    assert worker.calls == [("resolve", f.id)]
    worker.resolve_result = "none"
    r = authed.post(f"/api/files/{f.id}/resolve", json={"confirm": True})
    assert r.status_code == 400 and r.json()["detail"]
    worker.resolve_result = "busy"
    assert authed.post(f"/api/files/{f.id}/resolve", json={"confirm": True}).status_code == 409
    assert authed.post("/api/files/9999/resolve", json={"confirm": True}).status_code == 404


def test_status_reports_worker_stats_held_and_offline(authed, parts, tmp_path):
    db, worker, *_ = parts
    lib, f, run = seed_run(db, tmp_path)
    db.update_library(lib.id, status="offline")
    body = authed.get("/api/status").json()
    assert body["worker"]["state"] == "idle"
    assert body["no_scratch"] is True
    assert body["stats"]["saved_bytes"] == 500
    assert body["held_bytes"] == 0
    assert body["offline_libraries"] == [{"id": lib.id, "name": "Movies"}]
    worker.pause()
    assert authed.get("/api/status").json()["worker"]["state"] == "paused"
    scratch = make_folder(tmp_path, "scratch")
    authed.patch("/api/settings", json={"scratch_dir": str(scratch)})
    assert authed.get("/api/status").json()["no_scratch"] is False


def test_status_passes_on_the_workers_blocked_message(authed, parts):
    _, worker, *_ = parts
    worker.status = lambda: {"state": "blocked", "message": "Not enough room on the scratch disk"}
    body = authed.get("/api/status").json()
    assert body["worker"]["state"] == "blocked"
    assert body["message"] == "Not enough room on the scratch disk"


def test_pause_and_resume(authed, parts):
    _, worker, *_ = parts
    assert authed.post("/api/pause").json()["worker"]["state"] == "paused"
    assert authed.post("/api/resume").json()["worker"]["state"] == "idle"
    assert worker.calls == ["pause", "resume"]


# ---- holding

def test_clear_holding_requires_confirm(authed):
    assert authed.post("/api/holding/clear", json={}).status_code == 400
    assert authed.post("/api/holding/clear", json={"confirm": False}).status_code == 400
    assert authed.post("/api/holding/clear").status_code == 400


def hold_a_file(library_folder: Path) -> Path:
    holding = swap.work_root(library_folder) / "holding"
    holding.mkdir(parents=True, exist_ok=True)
    held = holding / "old.mkv"
    held.write_bytes(b"x" * 2048)
    return held


def test_clear_holding_frees_space_and_names_the_drive(authed, parts, tmp_path):
    folder = make_folder(tmp_path, "movies")
    add_library(authed, folder)
    held = hold_a_file(folder)
    try:
        status = authed.get("/api/status").json()
        assert status["held_bytes"] == 2048
        settings = authed.get("/api/settings").json()
        assert settings["held_bytes"] == 2048
        assert settings["held_libraries"] == ["Movies"]
        r = authed.post("/api/holding/clear", json={"confirm": True})
        assert r.status_code == 200
        body = r.json()
        assert body["freed_bytes"] == 2048 and body["libraries"] == ["Movies"]
        assert body["drives"] == [str(swap.work_root(folder).parent)]
        assert not held.exists()
    finally:
        if held.exists():
            held.unlink()


@pytest.mark.parametrize("state", ["running", "swapping", "waiting_in_use"])
def test_clear_holding_refused_while_a_run_is_active(authed, parts, tmp_path, state):
    db, *_ = parts
    folder = make_folder(tmp_path, "movies")
    lib = add_library(authed, folder).json()
    held = hold_a_file(folder)
    try:
        f = db.upsert_file(lib["id"], str(folder / "a.mkv"), 1, 1.0, "working")
        run = db.start_run(f.id, 1, "H.264", None, "why")
        db.update_run(run.id, state=state)
        r = authed.post("/api/holding/clear", json={"confirm": True})
        assert r.status_code == 400 and "only copy" in r.json()["detail"]
        assert held.exists()
    finally:
        if held.exists():
            held.unlink()


# ---- settings and the MCP token

def test_settings_include_the_mcp_token_and_command(authed, parts):
    db, *_ = parts
    body = authed.get("/api/settings").json()
    token = body["mcp_token"]
    assert token and token == ensure_mcp_token(db)
    assert authed.get("/api/settings").json()["mcp_token"] == token
    command = body["mcp_command"]
    assert command.startswith("claude mcp add")
    assert token in command and "testserver" in command
    other = authed.get("/api/settings", headers={"host": "192.168.1.5:8642"}).json()
    assert "http://192.168.1.5:8642/" in other["mcp_command"]
    assert body["holding_enabled"] is True


def test_ensure_mcp_token_is_stable(tmp_path):
    db = Database(tmp_path / "t.db")
    first = ensure_mcp_token(db)
    assert len(first) >= 40 and ensure_mcp_token(db) == first


def test_validation_errors_are_400_with_a_plain_detail(authed):
    r = authed.post("/api/libraries", content=b"not json",
                    headers={"content-type": "application/json"})
    assert r.status_code == 400 and isinstance(r.json()["detail"], str)


# ---- the real worker and scanner

def test_real_worker_and_scanner_behind_the_api(tmp_path):
    db = Database(tmp_path / "data" / "shelfsaver.db")
    config = Config(data_dir=tmp_path / "data")
    worker = Worker(db, config)
    scanner = Scanner(db, config, stable_seconds=0)
    folder = make_folder(tmp_path, "movies")
    make_video(folder / "Clip.mkv", seconds=2, size="1280x720", crf=1)
    with TestClient(create_app(db, worker, scanner, config)) as c:
        c.post("/api/auth/setup", json={"password": PASSWORD})
        lib = add_library(c, folder).json()
        deadline = time.time() + 30
        while time.time() < deadline and c.get("/api/status").json()["stats"]["remaining"] < 1:
            time.sleep(0.1)
        assert c.get("/api/status").json()["stats"]["remaining"] == 1
        assert c.get("/api/libraries").json()[0]["stats"]["remaining"] == 1

        worker.process_one()  # no scratch disk yet
        status = c.get("/api/status").json()
        assert status["no_scratch"] is True and status["worker"]["state"] == "no_scratch"
        assert status["message"] == "Choose a scratch disk in Settings"

        assert c.patch(f"/api/libraries/{lib['id']}", json={"level": "high"}).status_code == 200
        c.post("/api/pause")
        assert c.get("/api/status").json()["worker"]["state"] == "paused"
        c.post("/api/resume")
        assert c.get("/api/status").json()["worker"]["state"] != "paused"


def test_resolve_swap_ends_a_kept_swap_without_touching_files(tmp_path):
    db = Database(tmp_path / "data" / "shelfsaver.db")
    worker = Worker(db, Config(data_dir=tmp_path / "data"))
    original = tmp_path / "movies" / "a.mkv"
    original.parent.mkdir()
    original.write_bytes(b"orig")
    lib = db.add_library("M", str(original.parent), "low", True)
    f = db.upsert_file(lib.id, str(original), 4, 1.0, "failed", "Couldn't finish")
    run = db.start_run(f.id, 4, "H.264", None, "why")
    db.update_run(run.id, state="swapping", swap_json="{}")
    assert worker.resolve_swap(f.id) == "resolved"
    assert db.active_runs(("swapping", "waiting_in_use", "running")) == []
    after = db.get_file(f.id)
    assert after.status == "failed" and "Nothing was deleted" in after.reason
    assert original.read_bytes() == b"orig"
    assert worker.resolve_swap(f.id) == "none"
    # A file that is not stuck is refused too.
    g = db.upsert_file(lib.id, str(tmp_path / "movies" / "b.mkv"), 1, 1.0, "queued")
    assert worker.resolve_swap(g.id) == "none"
