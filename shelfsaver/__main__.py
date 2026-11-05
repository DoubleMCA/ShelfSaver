"""Start ShelfSaver: the worker, the scanner and the web server."""
from __future__ import annotations

import logging
import threading

import uvicorn

from .config import load_config
from .db import Database
from .logs import setup_logging
from .scanner import Scanner
from .web import create_app
from .worker import Worker

logger = logging.getLogger(__name__)


def main() -> None:
    config = load_config()
    config.data_dir.mkdir(parents=True, exist_ok=True)
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
