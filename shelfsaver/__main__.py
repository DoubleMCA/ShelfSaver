"""Start ShelfSaver: the worker, the scanner and the web server."""
from __future__ import annotations

import logging
import msvcrt
import sys
import threading

import uvicorn

from .config import load_config
from .db import Database
from .logs import setup_logging
from .scanner import Scanner
from .web import create_app
from .worker import Worker

logger = logging.getLogger(__name__)

LOCK_NAME = "shelfsaver.lock"
ALREADY_RUNNING = "ShelfSaver is already running on this PC. Close it first, then start it again."


def acquire_instance_lock(data_dir):
    """Lock a file in the data folder for as long as the returned handle stays open.

    Returns None if another process holds it. Windows drops the lock when its process dies,
    so a crash never blocks the next start.
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    handle = open(data_dir / LOCK_NAME, "a+b")
    try:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        handle.close()
        return None
    return handle


def main() -> None:
    config = load_config()
    lock = acquire_instance_lock(config.data_dir)
    if lock is None:
        print(ALREADY_RUNNING, file=sys.stderr)
        sys.exit(1)
    setup_logging(config.data_dir)
    db = Database(config.data_dir / "shelfsaver.db")
    worker = Worker(db, config)
    scanner = Scanner(db, config)
    worker.recover()

    stop = threading.Event()
    threads = [
        threading.Thread(target=worker.run_forever, args=(stop,), name="worker"),
        threading.Thread(target=scanner.start, args=(stop,), name="scanner"),
    ]
    for thread in threads:
        thread.start()
    try:
        server = uvicorn.Server(uvicorn.Config(
            create_app(db, worker, scanner, config), host="0.0.0.0", port=config.port,
            log_config=None))
        server.run()
    finally:
        stop.set()
        for thread in threads:
            thread.join(timeout=30)
        logger.info("Stopped")


if __name__ == "__main__":
    main()
