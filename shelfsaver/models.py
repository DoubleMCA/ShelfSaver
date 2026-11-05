"""Plain data records shared across the app."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

LIBRARY_STATUSES = ("ok", "offline")
FILE_STATUSES = (
    "queued", "working", "converted", "skipped", "failed", "waiting_in_use", "missing",
)
RUN_STATES = (
    "running", "swapping", "waiting_in_use", "done", "failed", "not_worth_it",
)


@dataclass
class Library:
    id: int
    name: str
    path: str
    level: str
    english_only: bool
    status: str = "ok"


@dataclass
class FileRow:
    id: int
    library_id: int
    path: str
    size: int
    mtime: float
    status: str
    reason: Optional[str] = None
    codec: Optional[str] = None
    container: Optional[str] = None
    resolution: Optional[str] = None
    updated_at: float = 0.0


@dataclass
class Run:
    id: int
    file_id: int
    state: str
    step: Optional[str] = None
    started_at: float = 0.0
    finished_at: Optional[float] = None
    was_size: Optional[int] = None
    now_size: Optional[int] = None
    was_format: Optional[str] = None
    now_format: Optional[str] = None
    now_path: Optional[str] = None
    was_bitrate: Optional[int] = None
    now_bitrate: Optional[int] = None
    why: Optional[str] = None
    tracks_json: Optional[str] = None
    checks_json: Optional[str] = None
    swap_json: Optional[str] = None
    error: Optional[str] = None
    log_tail: Optional[str] = None
    saved_bytes: int = 0
