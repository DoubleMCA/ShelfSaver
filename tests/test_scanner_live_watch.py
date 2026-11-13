"""Real-observer check: a file that appears in a watched library is noticed by watchdog.

Every other scanner test drives the Scanner directly and never starts a watchdog Observer, so
nothing in the suite would notice if a watchdog release stopped delivering events. pyproject.toml
allows more than one watchdog major, and CI-less upgrades are where that kind of change slips in.
This test starts the real Scanner (real Observer, real handler) and waits for each path to reach
Scanner._on_event, the one place every watchdog event passes through.

Only an event can satisfy it: the periodic full scan is pushed an hour away, and the first scan
runs before the files exist, so a scan cannot be what finds them.
"""
from __future__ import annotations

import os
import shutil
import threading
import time

import pytest

from shelfsaver.config import Config
from shelfsaver.db import Database
from shelfsaver.scanner import Scanner

# Generous because CI machines and network-backed disks can be slow to deliver events; the test
# returns as soon as the event arrives, so a high ceiling costs nothing when things work.
WAIT_SECONDS = 15


@pytest.fixture
def live(tmp_path):
    root = tmp_path / "Movies"
    root.mkdir()
    db = Database(tmp_path / "data" / "test.db")
    db.add_library("Movies", str(root), "high", False)
    scanner = Scanner(db, Config(data_dir=tmp_path / "data"), stable_seconds=0.0,
                      pending_interval=0.2, full_interval=3600)

    seen: list[str] = []
    real_on_event = scanner._on_event

    def spy(path) -> None:
        seen.append(os.path.normcase(os.fspath(path)))
        real_on_event(path)

    scanner._on_event = spy

    stop = threading.Event()
    thread = threading.Thread(target=scanner.start, args=(stop,), daemon=True)
    thread.start()
    deadline = time.monotonic() + WAIT_SECONDS
    while not scanner._watches and time.monotonic() < deadline:
        time.sleep(0.05)
    assert scanner._watches, "the scanner never started watching the library"
    time.sleep(0.5)  # let the first scan end, so it is not what reports the files below
    seen.clear()
    try:
        yield root, seen, tmp_path
    finally:
        stop.set()
        thread.join(timeout=10)


def _noticed(seen: list[str], path) -> bool:
    wanted = os.path.normcase(os.fspath(path))
    deadline = time.monotonic() + WAIT_SECONDS
    while time.monotonic() < deadline:
        if wanted in seen:
            return True
        time.sleep(0.05)
    return False


def test_new_file_in_the_library_is_noticed(live):
    root, seen, _ = live
    path = root / "new.mkv"
    path.write_bytes(b"x" * 2048)
    assert _noticed(seen, path)


def test_new_file_in_a_new_subfolder_is_noticed(live):
    """The watch is recursive, so a folder created after the watch started must be followed."""
    root, seen, _ = live
    folder = root / "Show" / "Season 1"
    folder.mkdir(parents=True)
    time.sleep(0.5)  # a new folder is registered a moment after it appears
    path = folder / "ep1.mkv"
    path.write_bytes(b"x" * 2048)
    assert _noticed(seen, path)


def test_file_moved_in_from_outside_is_noticed(live):
    """What a download client does when it finishes: rename the file into the library."""
    root, seen, tmp_path = live
    source = tmp_path / "done.mkv"
    source.write_bytes(b"x" * 2048)
    path = root / "done.mkv"
    shutil.move(str(source), str(path))
    assert _noticed(seen, path)
