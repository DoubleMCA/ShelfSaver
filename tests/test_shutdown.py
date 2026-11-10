"""Starting ShelfSaver for real and stopping it the way Docker and systemd do."""
from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time

import pytest

from shelfsaver import hostos
from shelfsaver.logs import tail_log

pytestmark = pytest.mark.skipif(hostos.IS_WINDOWS, reason="SIGTERM is how Linux stops services")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_until_listening(port: int, app: subprocess.Popen, seconds: float = 30) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and app.poll() is None:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def test_sigterm_stops_the_app_in_order_and_exits_cleanly(tmp_path):
    # Regression test: uvicorn re-raises SIGTERM after its own shutdown, which used to kill
    # the process before main() could stop the worker (see __main__.stop_on_sigterm).
    port = free_port()
    env = {**os.environ, "SHELFSAVER_DATA": str(tmp_path / "data"), "SHELFSAVER_PORT": str(port),
           "SHELFSAVER_MEDIA_ROOTS": str(tmp_path)}
    app = subprocess.Popen([sys.executable, "-m", "shelfsaver"], env=env,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        assert wait_until_listening(port, app), "the app never started listening"
        app.send_signal(signal.SIGTERM)
        assert app.wait(timeout=60) == 0, "SIGTERM must end with a normal exit, not be killed"
    finally:
        if app.poll() is None:
            app.kill()
    assert any("Stopped" in line for line in tail_log(tmp_path / "data", 200)), \
        "main() never reached its shutdown code"
