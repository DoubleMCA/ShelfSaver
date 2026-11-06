"""The app log: what gets written to it and how it is read back."""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from shelfsaver.logs import setup_logging, tail_log


@pytest.fixture
def logged(tmp_path):
    setup_logging(tmp_path)
    yield tmp_path
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "_shelfsaver_handler", False):
            root.removeHandler(handler)
            handler.close()


def _request(method, path, status):
    logging.getLogger("uvicorn.access").info(
        '%s - "%s %s HTTP/%s" %d', "192.0.2.1:50000", method, path, "1.1", status)


@pytest.mark.parametrize("path", ["/api/status", "/api/libraries",
                                  "/api/history?limit=100&show=conversions"])
def test_page_refreshes_are_not_logged(logged, path):
    _request("GET", path, 200)
    assert tail_log(logged, 10) == []


@pytest.mark.parametrize("method,path,status", [
    ("POST", "/api/pause", 200),
    ("POST", "/api/files/1/delete", 200),
    ("POST", "/mcp", 200),
    ("GET", "/api/status", 401),
    ("GET", "/api/status", 500),
    ("GET", "/settings", 200),
])
def test_actions_errors_and_page_visits_are_still_logged(logged, method, path, status):
    _request(method, path, status)
    found = tail_log(logged, 10)
    assert len(found) == 1 and f"{method} {path}" in found[0] and str(status) in found[0]


def test_setting_up_twice_still_keeps_refreshes_out(logged):
    setup_logging(logged)
    _request("GET", "/api/status", 200)
    assert tail_log(logged, 10) == []


def _write_logs(folder, **files):
    (folder / "logs").mkdir(exist_ok=True)
    for name, lines in files.items():
        path = "shelfsaver.log" if name == "live" else f"shelfsaver.log.{name[1:]}"
        (folder / "logs" / path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_older_log_files_are_read_oldest_first(tmp_path):
    _write_logs(tmp_path, b2=["oldest"], b1=["middle"], live=["newest"])
    assert tail_log(tmp_path, 10) == ["oldest", "middle", "newest"]


def test_search_reaches_older_log_files(tmp_path):
    _write_logs(tmp_path, b4=["ERROR old"], b1=["fine"], live=["ERROR new", "fine"])
    assert tail_log(tmp_path, 10, contains="ERROR") == ["ERROR old", "ERROR new"]


def test_line_limit_keeps_the_newest_across_files(tmp_path):
    _write_logs(tmp_path, b1=["a", "b"], live=["c"])
    assert tail_log(tmp_path, 2) == ["b", "c"]


def test_older_files_are_left_unread_once_there_are_enough_lines(tmp_path, monkeypatch):
    _write_logs(tmp_path, b1=["old"], live=["a", "b"])
    read = []
    real = Path.read_text
    monkeypatch.setattr(Path, "read_text", lambda self, *a, **k: (
        read.append(self.name), real(self, *a, **k))[1])
    assert tail_log(tmp_path, 2) == ["a", "b"]
    assert read == ["shelfsaver.log"]


def test_a_gap_in_the_files_does_not_hide_older_ones(tmp_path):
    _write_logs(tmp_path, b2=["old"])
    assert tail_log(tmp_path, 10) == ["old"]


def test_no_logs_at_all_is_an_empty_list(tmp_path):
    assert tail_log(tmp_path, 10) == []
    assert tail_log(tmp_path, 0) == []
