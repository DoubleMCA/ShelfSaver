"""SQLite storage: one connection per thread, WAL mode."""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Optional

from .models import FileRow, Library, Run

SCHEMA = """
CREATE TABLE IF NOT EXISTS libraries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    path TEXT NOT NULL,
    level TEXT NOT NULL,
    english_only INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'ok'
);
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    library_id INTEGER NOT NULL,
    path TEXT NOT NULL UNIQUE,
    size INTEGER NOT NULL,
    mtime REAL NOT NULL,
    status TEXT NOT NULL,
    reason TEXT,
    codec TEXT,
    container TEXT,
    resolution TEXT,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_files_status ON files(status, size);
CREATE INDEX IF NOT EXISTS idx_files_library ON files(library_id);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id INTEGER NOT NULL,
    library_id INTEGER,
    file_path TEXT,
    state TEXT NOT NULL,
    step TEXT,
    started_at REAL NOT NULL,
    finished_at REAL,
    was_size INTEGER,
    now_size INTEGER,
    was_format TEXT,
    now_format TEXT,
    now_path TEXT,
    was_bitrate INTEGER,
    now_bitrate INTEGER,
    why TEXT,
    tracks_json TEXT,
    checks_json TEXT,
    swap_json TEXT,
    error TEXT,
    log_tail TEXT,
    saved_bytes INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_runs_state ON runs(state);
CREATE INDEX IF NOT EXISTS idx_runs_file ON runs(file_id);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""

LIBRARY_FIELDS = {"name", "path", "level", "english_only", "status"}
RUN_FIELDS = {
    "state", "step", "started_at", "finished_at", "was_size", "now_size",
    "was_format", "now_format", "now_path", "was_bitrate", "now_bitrate", "why",
    "tracks_json", "checks_json", "swap_json", "error", "log_tail", "saved_bytes",
}
RUN_COLUMNS = ["id", "file_id", *sorted(RUN_FIELDS)]


def _library(row: sqlite3.Row) -> Library:
    return Library(
        id=row["id"], name=row["name"], path=row["path"], level=row["level"],
        english_only=bool(row["english_only"]), status=row["status"],
    )


def _file(row: sqlite3.Row) -> FileRow:
    return FileRow(**{k: row[k] for k in FileRow.__dataclass_fields__})


def _run(row: sqlite3.Row) -> Run:
    return Run(**{k: row[k] for k in Run.__dataclass_fields__})


class Database:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._conn().executescript(SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = conn
        return conn

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    def _exec(self, sql: str, args: Iterable[Any] = ()) -> sqlite3.Cursor:
        conn = self._conn()
        with conn:
            return conn.execute(sql, tuple(args))

    def _query(self, sql: str, args: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return self._conn().execute(sql, tuple(args)).fetchall()

    # libraries
    def add_library(self, name: str, path: str, level: str, english_only: bool) -> Library:
        cur = self._exec(
            "INSERT INTO libraries(name, path, level, english_only) VALUES (?,?,?,?)",
            (name, path, level, int(english_only)),
        )
        return self._get_library(cur.lastrowid)

    def _get_library(self, library_id: int) -> Library:
        return _library(self._query("SELECT * FROM libraries WHERE id=?", (library_id,))[0])

    def update_library(self, id: int, **fields: Any) -> None:
        bad = set(fields) - LIBRARY_FIELDS
        if bad:
            raise ValueError(f"unknown library fields: {sorted(bad)}")
        if not fields:
            return
        if "english_only" in fields:
            fields["english_only"] = int(fields["english_only"])
        sets = ", ".join(f"{k}=?" for k in fields)
        self._exec(f"UPDATE libraries SET {sets} WHERE id=?", (*fields.values(), id))

    def remove_library(self, id: int) -> None:
        """Delete the library and its queue rows. Run history is kept."""
        conn = self._conn()
        with conn:
            conn.execute("DELETE FROM files WHERE library_id=?", (id,))
            conn.execute("DELETE FROM libraries WHERE id=?", (id,))

    def list_libraries(self) -> list[Library]:
        return [_library(r) for r in self._query("SELECT * FROM libraries ORDER BY id")]

    # settings
    def get_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        rows = self._query("SELECT value FROM settings WHERE key=?", (key,))
        return rows[0]["value"] if rows else default

    def set_setting(self, key: str, value: str) -> None:
        self._exec(
            "INSERT INTO settings(key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    # files
    def upsert_file(
        self, library_id: int, path: str, size: int, mtime: float, status: str,
        reason: Optional[str] = None, codec: Optional[str] = None,
        container: Optional[str] = None, resolution: Optional[str] = None,
    ) -> FileRow:
        self._exec(
            "INSERT INTO files(library_id, path, size, mtime, status, reason, codec,"
            " container, resolution, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(path) DO UPDATE SET library_id=excluded.library_id,"
            " size=excluded.size, mtime=excluded.mtime, status=excluded.status,"
            " reason=excluded.reason, codec=COALESCE(excluded.codec, codec),"
            " container=COALESCE(excluded.container, container),"
            " resolution=COALESCE(excluded.resolution, resolution),"
            " updated_at=excluded.updated_at",
            (library_id, path, size, mtime, status, reason, codec, container,
             resolution, time.time()),
        )
        return self.get_file_by_path(path)

    def get_file(self, id: int) -> Optional[FileRow]:
        rows = self._query("SELECT * FROM files WHERE id=?", (id,))
        return _file(rows[0]) if rows else None

    def get_file_by_path(self, path: str) -> Optional[FileRow]:
        rows = self._query("SELECT * FROM files WHERE path=?", (path,))
        return _file(rows[0]) if rows else None

    def set_file_status(self, id: int, status: str, reason: Optional[str] = None) -> None:
        self._exec(
            "UPDATE files SET status=?, reason=?, updated_at=? WHERE id=?",
            (status, reason, time.time(), id),
        )

    def next_queued(self) -> Optional[FileRow]:
        rows = self._query(
            "SELECT * FROM files WHERE status='queued' ORDER BY size DESC, id LIMIT 1"
        )
        return _file(rows[0]) if rows else None

    def requeue(self, ids: Iterable[int]) -> None:
        conn = self._conn()
        with conn:
            conn.executemany(
                "UPDATE files SET status='queued', reason=NULL, updated_at=? WHERE id=?",
                [(time.time(), i) for i in ids],
            )

    # runs
    def start_run(
        self, file_id: int, was_size: int, was_format: str, was_bitrate: Optional[int],
        why: str,
    ) -> Run:
        f = self.get_file(file_id)
        cur = self._exec(
            "INSERT INTO runs(file_id, library_id, file_path, state, step, started_at,"
            " was_size, was_format, was_bitrate, why) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (file_id, f.library_id if f else None, f.path if f else None, "running",
             "starting", time.time(), was_size, was_format, was_bitrate, why),
        )
        return self._get_run(cur.lastrowid)

    def _get_run(self, run_id: int) -> Run:
        return _run(self._query("SELECT * FROM runs WHERE id=?", (run_id,))[0])

    def update_run(self, id: int, **fields: Any) -> None:
        bad = set(fields) - RUN_FIELDS
        if bad:
            raise ValueError(f"unknown run fields: {sorted(bad)}")
        if not fields:
            return
        sets = ", ".join(f"{k}=?" for k in fields)
        self._exec(f"UPDATE runs SET {sets} WHERE id=?", (*fields.values(), id))

    def active_runs(self, states: Iterable[str]) -> list[Run]:
        states = list(states)
        if not states:
            return []
        marks = ",".join("?" * len(states))
        rows = self._query(
            f"SELECT * FROM runs WHERE state IN ({marks}) ORDER BY id", states
        )
        return [_run(r) for r in rows]

    def history(
        self, library_id: Optional[int] = None, limit: int = 100, offset: int = 0
    ) -> list[dict]:
        where, args = "", []
        if library_id is not None:
            where = "WHERE library_id=?"
            args.append(library_id)
        rows = self._query(
            f"SELECT * FROM runs {where} ORDER BY started_at DESC, id DESC LIMIT ? OFFSET ?",
            (*args, limit, offset),
        )
        return [dict(r) for r in rows]

    def file_details(self, file_id: int) -> dict:
        f = self.get_file(file_id)
        runs = self._query(
            "SELECT * FROM runs WHERE file_id=? ORDER BY started_at DESC, id DESC",
            (file_id,),
        )
        return {"file": dict(f.__dict__) if f else None, "runs": [dict(r) for r in runs]}

    def stats(self, library_id: Optional[int] = None) -> dict:
        run_where, file_where, args = "", "", []
        if library_id is not None:
            run_where = " AND library_id=?"
            file_where = " AND library_id=?"
            args.append(library_id)
        saved = self._query(
            "SELECT COALESCE(SUM(saved_bytes),0) AS n FROM runs WHERE state='done'"
            + run_where, args)[0]["n"]
        converted = self._query(
            "SELECT COUNT(*) AS n FROM runs WHERE state='done'" + run_where, args)[0]["n"]
        remaining = self._query(
            "SELECT COUNT(*) AS n FROM files WHERE status IN ('queued','working',"
            "'waiting_in_use')" + file_where, args)[0]["n"]
        return {"saved_bytes": saved, "converted": converted, "remaining": remaining}
