"""Drive health: running the check, keeping the latest result, notices and log lines."""
from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time

import pytest

from shelfsaver import hostos
from shelfsaver.db import Database
from shelfsaver.drive_health import (CHECK_SECONDS, FAILING_NOTICE_SECONDS, DriveCheckFailed,
                                     DriveHealth, parse_report, read_report)
from tests.test_drive_health import POOL, disk

HOUR = 3600.0
G_DISK = {"letter": "G", "kind": "disk", "disks": disk("Samsung SSD", media="SSD")}


class Clock:
    def __init__(self):
        self.now = 1_800_000_000.0

    def __call__(self):
        return self.now


class Reader:
    """Stands in for PowerShell: answers with `report`, or raises when `fail` is set."""

    def __init__(self, report=None):
        self.report = report if report is not None else {"drives": [POOL, G_DISK]}
        self.fail = False
        self.calls = []

    def __call__(self, letters):
        self.calls.append(list(letters))
        if self.fail:
            raise DriveCheckFailed("PowerShell did not answer")
        return self.report


@pytest.fixture
def server_db(tmp_path):
    db = Database(tmp_path / "shelfsaver.db")
    db.add_library("TV", "F:\\TV", "medium", False)
    db.add_library("Movies", "F:\\Movies", "medium", False)
    db.set_setting("scratch_dir", "G:\\Work")
    return db


def health(db, reader, clock=None, windows=True):
    return DriveHealth(db, read=reader, clock=clock or Clock(), is_windows=windows)


def test_check_lists_each_letter_once_with_its_users(server_db):
    reader = Reader()
    result = health(server_db, reader).check()
    assert reader.calls == [["F", "G"]]
    users = {d["letter"]: d["used_by"] for d in result["drives"]}
    assert users == {"F": ["TV", "Movies"], "G": ["Scratch"]}
    assert result["available"] is True and result["checked_at"] is not None
    assert result["problems"] == [] and result["failing_since"] is None


def test_a_letter_the_report_left_out_is_listed_as_unknown(server_db):
    result = health(server_db, Reader({"drives": [POOL]})).check()
    g = [d for d in result["drives"] if d["letter"] == "G"][0]
    assert g["kind"] == "unknown" and g["used_by"] == ["Scratch"]


def test_not_windows_is_not_available_and_never_runs(server_db):
    reader = Reader()
    checker = health(server_db, reader, windows=False)
    assert checker.check()["available"] is False
    assert checker.latest()["available"] is False
    assert reader.calls == [] and checker.notices() == []


def test_nothing_checked_yet(server_db):
    latest = health(server_db, Reader()).latest()
    assert latest["checked_at"] is None and latest["drives"] == []


def test_a_failed_check_keeps_the_last_result_and_notices_after_a_day(server_db):
    clock, reader = Clock(), Reader()
    checker = health(server_db, reader, clock)
    checker.check()
    reader.fail = True
    clock.now += HOUR
    failed_at = clock.now
    result = checker.check()
    assert len(result["drives"]) == 2 and result["failing_since"] == failed_at
    clock.now = failed_at + 23 * HOUR
    checker.check()
    assert checker.notices() == []
    clock.now = failed_at + FAILING_NOTICE_SECONDS
    checker.check()
    (notice,) = checker.notices()
    assert notice.startswith("ShelfSaver couldn't check drive health since ")
    reader.fail = False
    checker.check()
    assert checker.latest()["failing_since"] is None and checker.notices() == []


def test_warning_notice_names_the_letter(server_db):
    bad = {**POOL, "disks": [disk("WDC A"), disk("WDC B", health="Warning")]}
    checker = health(server_db, Reader({"drives": [bad, G_DISK]}))
    checker.check()
    assert checker.notices() == [
        "A disk behind F: reports Warning. Open Settings, then Drive health."]


def test_one_notice_per_letter(server_db):
    bad = {**POOL, "pool": {**POOL["pool"], "HealthStatus": "Warning"},
           "disks": [disk("WDC A", health="Unhealthy")]}
    checker = health(server_db, Reader({"drives": [bad, G_DISK]}))
    checker.check()
    assert len(checker.notices()) == 1


def test_problem_logged_once_and_cleared_once(server_db, caplog):
    bad = {**POOL, "disks": [disk("WDC A"), disk("WDC B", health="Warning")]}
    reader = Reader({"drives": [bad, G_DISK]})
    checker = health(server_db, reader)
    with caplog.at_level(logging.INFO, logger="shelfsaver.drive_health"):
        checker.check()
        checker.check()
        reader.report = {"drives": [POOL, G_DISK]}
        checker.check()
        checker.check()
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("Drive health:")]
    assert len(lines) == 2
    assert "found at startup" in lines[0] and "WDC B" in lines[0] and "F:" in lines[0]
    assert "cleared" in lines[1]


def test_a_new_problem_later_is_not_called_found_at_startup(server_db, caplog):
    reader = Reader()
    checker = health(server_db, reader)
    checker.check()
    reader.report = {"drives": [{**POOL, "disks": [disk("WDC A", health="Warning")]}, G_DISK]}
    with caplog.at_level(logging.INFO, logger="shelfsaver.drive_health"):
        checker.check()
    (line,) = [r.getMessage() for r in caplog.records if r.getMessage().startswith("Drive health:")]
    assert "found at startup" not in line and "Warning" in line


def test_unc_library_is_unknown_not_a_problem(tmp_path):
    db = Database(tmp_path / "shelfsaver.db")
    db.add_library("NAS", "\\\\nas\\tv", "medium", False)
    reader = Reader({"drives": []})
    checker = health(db, reader)
    (drive,) = checker.check()["drives"]
    assert drive["letter"] is None and drive["kind"] == "unknown" and drive["used_by"] == ["NAS"]
    assert reader.calls == [] and checker.notices() == []


def test_concurrent_checks_do_not_interleave(server_db):
    started, go_on = threading.Event(), threading.Event()
    reader = Reader()

    def slow(letters):
        started.set()
        assert go_on.wait(10)
        return reader(letters)

    checker = DriveHealth(server_db, read=slow, is_windows=True)
    results = []
    first = threading.Thread(target=lambda: results.append(checker.check()))
    first.start()
    assert started.wait(10)
    second = threading.Thread(target=lambda: results.append(checker.check()))
    second.start()
    time.sleep(0.05)
    go_on.set()
    first.join(10)
    second.join(10)
    assert len(results) == 2 and all(len(r["drives"]) == 2 for r in results)
    assert len(reader.calls) == 1          # the second caller waited and got the same fresh result


def test_run_forever_checks_at_once_then_hourly(server_db):
    reader = Reader()
    checker = health(server_db, reader)
    waits = []

    class Stop:
        def is_set(self):
            return len(waits) >= 2

        def wait(self, seconds):
            waits.append(seconds)
            return self.is_set()

    checker.run_forever(Stop())
    assert len(reader.calls) == 2 and waits == [CHECK_SECONDS, CHECK_SECONDS]


def test_run_forever_does_nothing_off_windows(server_db):
    reader = Reader()
    stop = threading.Event()
    health(server_db, reader, windows=False).run_forever(stop)
    assert reader.calls == []


# ---- the PowerShell call

class Done:
    def __init__(self, stdout="", returncode=0):
        self.stdout, self.returncode, self.stderr = stdout, returncode, ""


@pytest.mark.parametrize("problem", [
    subprocess.TimeoutExpired("powershell", 60), FileNotFoundError("powershell")])
def test_read_report_turns_a_stuck_or_missing_powershell_into_a_failed_check(problem):
    def run(*args, **kwargs):
        raise problem
    with pytest.raises(DriveCheckFailed):
        read_report(["F"], run=run)


@pytest.mark.parametrize("done", [Done("oops"), Done("[1, 2]"), Done("{}", returncode=1)])
def test_read_report_refuses_bad_output(done):
    with pytest.raises(DriveCheckFailed):
        read_report(["F"], run=lambda *a, **k: done)


def test_read_report_returns_the_parsed_json_and_uses_a_timeout():
    seen = {}

    def run(args, **kwargs):
        seen.update(kwargs, args=args)
        return Done(json.dumps({"drives": [POOL]}))

    assert read_report(["F"], run=run) == {"drives": [POOL]}
    assert seen["timeout"] == 60 and seen["args"][0].lower().startswith("powershell")


def test_read_report_only_accepts_drive_letters():
    with pytest.raises(ValueError):
        read_report(["F; Remove-Item x"], run=lambda *a, **k: Done("{}"))


@pytest.mark.skipif(not hostos.IS_WINDOWS, reason="needs Windows' storage tools")
def test_real_script_runs_here():
    letter = os.environ.get("SystemDrive", "C:")[0]
    (drive,) = parse_report(read_report([letter]))
    assert drive["letter"] == letter.upper() and drive["kind"] in ("disk", "pool")
    assert drive["disks"] and drive["disks"][0]["health"]
