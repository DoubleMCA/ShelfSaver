"""Rotating log file and a helper to read it back."""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_NAME = "shelfsaver.log"
_HANDLER_TAG = "_shelfsaver_handler"


def _log_path(data_dir: Path) -> Path:
    return Path(data_dir) / "logs" / LOG_NAME


def setup_logging(data_dir: Path) -> None:
    path = _log_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    for h in list(root.handlers):
        if getattr(h, _HANDLER_TAG, False):
            root.removeHandler(h)
            h.close()
    handler = RotatingFileHandler(
        path, maxBytes=5 * 1024 * 1024, backupCount=4, encoding="utf-8"
    )  # live file + 4 backups = 5 files
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    setattr(handler, _HANDLER_TAG, True)
    root.addHandler(handler)
    if root.level == logging.NOTSET or root.level > logging.INFO:
        root.setLevel(logging.INFO)


def tail_log(data_dir: Path, lines: int, contains: str | None = None) -> list[str]:
    path = _log_path(data_dir)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    found = text.splitlines()
    if contains:
        found = [ln for ln in found if contains in ln]
    return found[-lines:] if lines > 0 else []
