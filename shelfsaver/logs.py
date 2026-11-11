"""Rotating log file and a helper to read it back."""
from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_NAME = "shelfsaver.log"
BACKUP_COUNT = 4
_HANDLER_TAG = "_shelfsaver_handler"


class _QuietPageRefreshes(logging.Filter):
    """Open pages ask the API for updates every few seconds, which would push out everything
    else in the log. Actions, errors and page visits are still logged."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if not isinstance(args, tuple) or len(args) != 5:
            return True
        _, method, path, _, status = args
        return not (method == "GET" and str(path).startswith("/api/") and int(status) < 400)


_QUIET_PAGE_REFRESHES = _QuietPageRefreshes()


def _log_path(data_dir: Path) -> Path:
    return Path(data_dir) / "logs" / LOG_NAME


def _wants_stdout() -> bool:
    """True when SHELFSAVER_LOG_STDOUT is set to anything but empty/0/false/no.

    Why: a container's log is whatever the program writes to stdout/stderr (`docker logs`, the
    Unraid log button). The rotating file in the data folder stays too, because the web page's
    log viewer reads it back. The Docker image sets this variable; a plain install does not.
    """
    return os.environ.get("SHELFSAVER_LOG_STDOUT", "").strip().lower() not in ("", "0", "false", "no")


def setup_logging(data_dir: Path) -> None:
    path = _log_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    for h in list(root.handlers):
        if getattr(h, _HANDLER_TAG, False):
            root.removeHandler(h)
            h.close()
    handler = RotatingFileHandler(
        path, maxBytes=5 * 1024 * 1024, backupCount=BACKUP_COUNT, encoding="utf-8"
    )  # live file + 4 backups = 5 files
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    setattr(handler, _HANDLER_TAG, True)
    root.addHandler(handler)
    if _wants_stdout():
        # Same format as the file, so the two never disagree. Tagged like the file handler so a
        # second setup_logging() call replaces it instead of printing every line twice.
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(handler.formatter)
        setattr(console, _HANDLER_TAG, True)
        root.addHandler(console)
    logging.getLogger("uvicorn.access").addFilter(_QUIET_PAGE_REFRESHES)
    if root.level == logging.NOTSET or root.level > logging.INFO:
        root.setLevel(logging.INFO)


def tail_log(data_dir: Path, lines: int, contains: str | None = None) -> list[str]:
    """The newest matching lines, oldest first, from the live file and then its backups."""
    if lines <= 0:
        return []
    live = _log_path(data_dir)
    found: list[str] = []
    for path in [live] + [live.with_name(f"{LOG_NAME}.{n}") for n in range(1, BACKUP_COUNT + 1)]:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        kept = text.splitlines()
        if contains:
            kept = [ln for ln in kept if contains in ln]
        found = kept + found
        if len(found) >= lines:
            break
    return found[-lines:]
