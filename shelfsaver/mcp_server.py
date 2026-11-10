"""Read-only MCP connection so Claude on another PC can read logs and debug information.

Every tool only reads. The endpoint requires `Authorization: Bearer <token>`, where the token
is the one stored in settings (`ensure_mcp_token`). Nothing here returns the password hash,
sign-in sessions or the token.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import re
import shutil
import subprocess
import time
from typing import Any, Optional

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from . import __version__, swap
from .db import ensure_mcp_token
from .encoders import DEFAULT_ENCODER, ENCODERS, Encoder, encoder_for
from .logs import tail_log

logger = logging.getLogger(__name__)

MCP_PATH = "/mcp"
MAX_LOG_LINES = 5000
MAX_FILES = 500
HEALTH_CACHE_SECONDS = 30.0
PROBE_TIMEOUT = 5.0
LOG_TAIL_CHARS = 20000


def _run_tool(args: list[str]) -> Optional[str]:
    """Run a tool with an argument list and a short timeout. None if it can't run."""
    try:
        done = subprocess.run(args, capture_output=True, text=True, timeout=PROBE_TIMEOUT,
                              encoding="utf-8", errors="replace",
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return None
    return (done.stdout or "") if done.returncode == 0 else None


def _gpu(encoder: Encoder = DEFAULT_ENCODER) -> dict:
    return encoder.device_info(_run_tool)


def _ffmpeg(ffmpeg: str) -> dict:
    """What this FFmpeg is and which hardware encoders it was built with.

    `hevc_nvenc` stays as its own key because it was the original answer. `encoders` lists
    every encoder ShelfSaver knows by name, so a new encoder shows up here without changing
    the shape of the reply.
    """
    version_text = _run_tool([ffmpeg, "-hide_banner", "-version"])
    if version_text is None:
        return {"available": False, "version": None, "hevc_nvenc": False,
                "encoders": {name: False for name in ENCODERS}}
    match = re.search(r"version\s+(\S+)", version_text)
    listing = _run_tool([ffmpeg, "-hide_banner", "-encoders"]) or ""
    built = {name: bool(re.search(rf"\b{re.escape(cls.codec)}\b", listing))
             for name, cls in ENCODERS.items()}
    return {"available": True, "version": match.group(1) if match else None,
            "hevc_nvenc": bool(re.search(r"\bhevc_nvenc\b", listing)), "encoders": built}


def _free_space(path: Optional[str]) -> dict:
    if not path:
        return {"path": path, "free_bytes": None}
    try:
        return {"path": path, "free_bytes": shutil.disk_usage(swap.ext_path(path)).free}
    except (OSError, ValueError):
        return {"path": path, "free_bytes": None}


def build_mcp(db, worker, config) -> MCPServer:
    """The read-only tools. Pair it with `McpGate` to serve it."""
    server = MCPServer("ShelfSaver", instructions=(
        "Read-only access to ShelfSaver's status, logs, files and health."))
    slow_cache: dict[str, Any] = {"at": 0.0, "value": None}
    encoder = encoder_for(config)

    def status_now() -> dict:
        libraries = db.list_libraries()
        try:
            held = swap.held_size([l.path for l in libraries] + db.held_volumes())
        except Exception:
            held = None
        return {
            "worker": worker.status(),
            "stats": db.stats(),
            "held_bytes": held,
            "scratch_dir": db.get_setting("scratch_dir") or None,
            "holding_enabled": db.get_setting("holding_enabled") == "1",
            "libraries": [{"id": l.id, "name": l.name, "path": l.path, "level": l.level,
                           "status": l.status, "stats": db.stats(l.id)} for l in libraries],
        }

    def health_now() -> dict:
        age = time.monotonic() - slow_cache["at"]
        if slow_cache["value"] is None or age > HEALTH_CACHE_SECONDS:
            slow_cache["value"] = {"gpu": _gpu(encoder), "encoder": encoder.name,
                                   "ffmpeg": _ffmpeg(config.ffmpeg)}
            slow_cache["at"] = time.monotonic()
        scratch = db.get_setting("scratch_dir") or None
        return {
            **slow_cache["value"],
            "app_version": __version__,
            "free_space": {
                "libraries": [{"id": l.id, "name": l.name, **_free_space(l.path)}
                              for l in db.list_libraries()],
                "scratch": _free_space(scratch),
            },
        }

    def file_json(f) -> dict:
        return {"id": f.id, "library_id": f.library_id, "path": f.path, "size": f.size,
                "status": f.status, "reason": f.reason, "codec": f.codec,
                "container": f.container, "resolution": f.resolution,
                "updated_at": f.updated_at}

    def details_now(file_id: int) -> dict:
        details = db.file_details(file_id)
        if details["file"] is None:
            return {"error": "No file with that id."}
        runs = []
        for run in details["runs"]:
            run = dict(run)
            run.pop("swap_json", None)
            for key in ("tracks", "checks"):
                text = run.pop(f"{key}_json", None)
                try:
                    run[key] = json.loads(text) if text else None
                except ValueError:
                    run[key] = None
            run["log_tail"] = (run.get("log_tail") or "")[-LOG_TAIL_CHARS:]
            runs.append(run)
        return {"file": details["file"], "runs": runs}

    @server.tool()
    async def get_status() -> dict:
        """What ShelfSaver is doing now: worker state, totals and each library."""
        return await asyncio.to_thread(status_now)

    @server.tool()
    async def get_logs(lines: int = 200, contains: str | None = None) -> list[str]:
        """The newest lines of the app log (at most 5000), optionally only lines containing text."""
        count = max(0, min(lines, MAX_LOG_LINES))
        return await asyncio.to_thread(tail_log, config.data_dir, count, contains)

    @server.tool()
    async def list_files(status: str | None = None, library_id: int | None = None,
                         limit: int = 100) -> list[dict]:
        """Files ShelfSaver knows about, optionally by status or library (at most 500)."""
        count = max(0, min(limit, MAX_FILES))
        rows = await asyncio.to_thread(db.list_files, status, library_id, count)
        return [file_json(f) for f in rows]

    @server.tool()
    async def get_file_details(file_id: int) -> dict:
        """One file's runs, including checks, errors and the FFmpeg log tail."""
        return await asyncio.to_thread(details_now, file_id)

    @server.tool()
    async def get_health() -> dict:
        """GPU name and driver, the encoder in use, FFmpeg version and encoders, free disk space
        and app version."""
        return await asyncio.to_thread(health_now)

    return server


def mcp_transport_security() -> TransportSecuritySettings:
    # The bearer token guards the endpoint. Rebinding protection would reject the PC's LAN
    # address, which is how Claude on another PC connects.
    return TransportSecuritySettings(enable_dns_rebinding_protection=False)


class McpGate:
    """ASGI middleware: serves /mcp and /mcp/ with a bearer token before any other route."""

    def __init__(self, app: ASGIApp, db, mcp_app: ASGIApp):
        self.app, self.db, self.mcp_app = app, db, mcp_app

    def _authorised(self, scope: Scope) -> bool:
        header = ""
        for name, value in scope.get("headers", []):
            if name == b"authorization":
                header = value.decode("latin-1")
                break
        scheme, _, given = header.partition(" ")
        if scheme.lower() != "bearer" or not given:
            return False
        expected = ensure_mcp_token(self.db)
        return hmac.compare_digest(given.strip().encode(), expected.encode())

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"].rstrip("/") != MCP_PATH:
            await self.app(scope, receive, send)
            return
        if not self._authorised(scope):
            response = JSONResponse({"error": "Unauthorized"}, status_code=401,
                                    headers={"WWW-Authenticate": "Bearer"})
            await response(scope, receive, send)
            return
        # Both /mcp and /mcp/ reach the same handler, so no redirect drops the header.
        await self.mcp_app({**scope, "path": MCP_PATH, "raw_path": MCP_PATH.encode()},
                           receive, send)
