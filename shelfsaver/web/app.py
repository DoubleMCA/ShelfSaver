"""The web API: sign-in, libraries, history, settings and controls. All JSON under /api."""
from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import logging
import os
import stat
import re
import secrets
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, StrictBool

from .. import swap
from . import folders
from ..db import ensure_mcp_token
from ..encoders import encoder_for
from ..mcp_server import McpGate, build_mcp, mcp_transport_security
from ..models import IGNORED_REASON
from ..languages import clean_language, language_choices, language_name, load_language
from ..schedule import clean_blackouts, load_blackouts
from ..system_usage import SystemUsage
from ..worker import _format_name

logger = logging.getLogger(__name__)

SESSION_COOKIE = "shelfsaver_session"
SESSION_SECONDS = 30 * 24 * 3600
MIN_PASSWORD_LENGTH = 8
LOGIN_FAILURES = 5
LOGIN_WINDOW_SECONDS = 60.0
LEVELS = ("low", "medium", "high")
ACTIVE_RUN_STATES = ("running", "swapping", "waiting_in_use")
MAX_PAGE = 500
HISTORY_KINDS = {"conversions": "run", "scans": "file"}
HELD_CACHE_SECONDS = 30.0
STATIC_DIR = Path(__file__).parent / "static"
PAGES = {"/": "index.html", "/library": "library.html", "/queue": "queue.html",
         "/settings": "settings.html", "/login": "login.html"}

STILL_FINISHING = ("A file in this library is still being finished. "
                   "Try again when it's done.")
HOLDING_BUSY = ("A file is being replaced or finished, and a held file can be the only copy "
                "right now. If a conversion is in progress, pause it and wait for it to "
                "stop. If a file is waiting or needs attention, wait for it, or use Try again "
                "or the resolve action on that file.")
SAFE_HOST = re.compile(r"[A-Za-z0-9.\-]{1,253}(:[0-9]{1,5})?|\[[0-9A-Fa-f:.]{2,45}\](:[0-9]{1,5})?")


# ---- passwords and sessions

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return f"scrypt${2 ** 14}$8$1${salt.hex()}${digest.hex()}"


def check_password(password: str, stored: Optional[str]) -> bool:
    try:
        scheme, n, r, p, salt, expected = (stored or "").split("$")
        if scheme != "scrypt":
            return False
        digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n),
                                r=int(r), p=int(p), dklen=len(expected) // 2)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest.hex(), expected)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class LoginLimiter:
    """At most `limit` failed logins per `window` seconds for each client address."""

    def __init__(self, limit: int = LOGIN_FAILURES, window: float = LOGIN_WINDOW_SECONDS,
                 clock: Callable[[], float] = time.monotonic):
        self.limit, self.window, self.clock = limit, window, clock
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def _recent(self, key: str) -> list[float]:
        cutoff = self.clock() - self.window
        recent = [t for t in self._failures.get(key, []) if t > cutoff]
        if recent:
            self._failures[key] = recent
        else:
            self._failures.pop(key, None)
        return recent

    def blocked(self, key: str) -> bool:
        with self._lock:
            return len(self._recent(key)) >= self.limit

    def record_failure(self, key: str) -> None:
        with self._lock:
            for other in list(self._failures):  # keep the table from growing
                self._recent(other)
            self._failures.setdefault(key, []).append(self.clock())

    def clear(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)


# ---- request bodies

class PasswordBody(BaseModel):
    password: str = ""


class ConfirmBody(BaseModel):
    confirm: Optional[StrictBool] = None


class LibraryBody(BaseModel):
    name: str = ""
    path: str = ""
    level: str = ""
    english_only: StrictBool = False
    shrink_h265: StrictBool = False


class LibraryPatch(BaseModel):
    name: Optional[str] = None
    level: Optional[str] = None
    english_only: Optional[StrictBool] = None
    shrink_h265: Optional[StrictBool] = None


class SettingsPatch(BaseModel):
    scratch_dir: Optional[str] = None
    holding_enabled: Optional[StrictBool] = None
    blackouts: Optional[list[Any]] = None
    default_language: Optional[str] = None


# ---- path helpers

def _fail(status: int, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail=message)


def _norm(path: str) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(path)))


def _overlaps(first: str, second: str) -> bool:
    a, b = _norm(first), _norm(second)
    return (a == b or a.startswith(b.rstrip("\\/") + os.sep)
            or b.startswith(a.rstrip("\\/") + os.sep))


def _drive_list(paths) -> list[str]:
    drives = []
    for path in paths:
        try:
            drive = str(swap.work_root(path).parent)
        except Exception:
            continue
        if drive not in drives:
            drives.append(drive)
    return drives


def _in_background(name: str, *steps: Callable[[], None]) -> None:
    def run() -> None:
        for step in steps:
            try:
                step()
            except Exception:
                logger.exception("Background %s failed", name)

    threading.Thread(target=run, name=name, daemon=True).start()


def _parsed(text: Optional[str]) -> Any:
    try:
        return json.loads(text) if text else None
    except ValueError:
        return None


class _FreshStaticFiles(StaticFiles):
    """Serve the page scripts so the browser always checks for a newer copy after an update."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


def create_app(db, worker, scanner, config) -> FastAPI:
    mcp_server = build_mcp(db, worker, config)
    mcp_app = mcp_server.streamable_http_app(
        streamable_http_path="/mcp", stateless_http=True, json_response=True,
        transport_security=mcp_transport_security())

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI):
        async with mcp_server.session_manager.run():
            yield

    app = FastAPI(title="ShelfSaver", docs_url=None, redoc_url=None, openapi_url=None,
                  lifespan=lifespan)
    limiter = LoginLimiter()

    @app.exception_handler(RequestValidationError)
    async def _bad_request(request: Request, exc: RequestValidationError):
        return JSONResponse(status_code=400, content={
            "detail": "That request wasn't understood. Check what you entered and try again."})

    # ---- sign-in

    def signed_in(request: Request) -> bool:
        token = request.cookies.get(SESSION_COOKIE)
        if not token:
            return False
        digest = _token_hash(token)
        expires = db.session_expiry(digest)
        if expires is None:
            return False
        if expires <= time.time():
            db.delete_session(digest)
            return False
        return True

    def require_session(request: Request) -> None:
        if not signed_in(request):
            raise _fail(401, "Please sign in.")

    def start_session(response: Response) -> None:
        token = secrets.token_urlsafe(32)
        db.add_session(_token_hash(token), time.time() + SESSION_SECONDS)
        response.set_cookie(SESSION_COOKIE, token, max_age=SESSION_SECONDS, httponly=True,
                            samesite="strict", path="/")

    def client_key(request: Request) -> str:
        return request.client.host if request.client else "unknown"

    open_api = APIRouter(prefix="/api/auth")
    api = APIRouter(prefix="/api", dependencies=[Depends(require_session)])

    @open_api.get("/state")
    def auth_state(request: Request):
        return {"has_password": bool(db.get_setting("password_hash")),
                "signed_in": signed_in(request)}

    @open_api.post("/setup")
    def auth_setup(body: PasswordBody, response: Response):
        if len(body.password) < MIN_PASSWORD_LENGTH:
            raise _fail(400, f"Choose a password of at least {MIN_PASSWORD_LENGTH} characters.")
        # One statement decides who sets the first password, so two requests can't both win.
        if not db.set_setting_if_absent("password_hash", hash_password(body.password)):
            raise _fail(409, "A password is already set. Sign in instead.")
        start_session(response)
        return {"ok": True}

    @open_api.post("/login")
    def auth_login(body: PasswordBody, request: Request, response: Response):
        key = client_key(request)
        if limiter.blocked(key):
            raise _fail(429, "Too many wrong passwords. Wait a minute and try again.")
        if not check_password(body.password, db.get_setting("password_hash")):
            limiter.record_failure(key)
            raise _fail(401, "That password isn't right.")
        limiter.clear(key)
        start_session(response)
        return {"ok": True}

    @open_api.post("/logout")
    def auth_logout(request: Request, response: Response):
        token = request.cookies.get(SESSION_COOKIE)
        if token:
            db.delete_session(_token_hash(token))
        response.delete_cookie(SESSION_COOKIE, path="/")
        return {"ok": True}

    # ---- helpers that need the database

    def library_or_404(library_id: int):
        library = next((l for l in db.list_libraries() if l.id == library_id), None)
        if library is None:
            raise _fail(404, "That library no longer exists.")
        return library

    def library_json(library) -> dict:
        return {"id": library.id, "name": library.name, "path": library.path,
                "level": library.level, "english_only": library.english_only,
                "shrink_h265": library.shrink_h265, "status": library.status, "stats": db.stats(library.id)}

    def busy_library_ids() -> set:
        busy = set()
        for run in db.active_runs(ACTIVE_RUN_STATES):
            record = db.run_record(run.id) or {}
            file = db.get_file(run.file_id)
            busy.add(record.get("library_id") or (file.library_id if file else None))
        for status in ("working", "waiting_in_use"):
            busy.update(f.library_id for f in db.files_with_status(status))
        return busy

    def held_paths() -> list[str]:
        """The libraries, and every volume that may still hold files from a removed one."""
        return [l.path for l in db.list_libraries()] + db.held_volumes()

    def remember_volume(path: str) -> None:
        db.remember_held_volume(str(swap.work_root(path).parent))

    held_lock = threading.Lock()  # also keeps polls from walking the folders side by side
    held_cache: dict[str, Any] = {"at": None, "paths": None, "bytes": 0}

    def held_bytes(fresh: bool = True) -> int:
        """Bytes held. fresh=False lets a recent measurement stand, because walking a large
        holding folder is too slow for every poll."""
        paths = held_paths()
        with held_lock:
            at = held_cache["at"]
            if (fresh or at is None or held_cache["paths"] != paths
                    or time.monotonic() - at >= HELD_CACHE_SECONDS):
                try:
                    held_cache["bytes"] = swap.held_size(paths)
                except Exception:
                    logger.exception("Could not measure the holding folder")
                    held_cache["bytes"] = 0
                held_cache["at"], held_cache["paths"] = time.monotonic(), paths
            return held_cache["bytes"]

    def held_info() -> dict:
        return {"held_bytes": held_bytes(),
                "held_libraries": [l.name for l in db.list_libraries()],
                "held_drives": _drive_list(held_paths())}

    def check_folder(raw, what: str) -> str:
        """Validate a library or scratch folder, turning a refusal into a plain 400.

        Libraries must sit under the media roots and the scratch folder under the scratch
        roots (see folders.py for why Linux needs both lists spelled out).
        """
        scratch = what == "scratch"
        try:
            return folders.check_local_folder(
                raw, what, config.scratch_roots if scratch else config.media_roots,
                "SHELFSAVER_SCRATCH_ROOTS" if scratch else "SHELFSAVER_MEDIA_ROOTS")
        except folders.FolderProblem as problem:
            raise _fail(problem.status, problem.message)

    def scratch_setting() -> Optional[str]:
        return db.get_setting("scratch_dir") or None

    # ---- status and controls

    @api.get("/status")
    def status():
        state = worker.status()
        return {
            "worker": state,
            "message": state.get("message"),
            "no_scratch": scratch_setting() is None,
            "stats": db.stats(),
            "scanner": scanner.status(),
            "held_bytes": held_bytes(fresh=False),
            "offline_libraries": [{"id": l.id, "name": l.name}
                                  for l in db.list_libraries() if l.status == "offline"],
        }

    system_usage = SystemUsage(encoder_for(config))

    @api.get("/system")
    def system():
        return system_usage.read()

    @api.post("/pause")
    def pause():
        worker.pause()
        return {"worker": worker.status()}

    @api.post("/resume")
    def resume():
        worker.resume()
        return {"worker": worker.status()}

    # ---- history and files

    @api.get("/queue")
    def queue(limit: int = 100, offset: int = 0):
        if not 1 <= limit <= MAX_PAGE or offset < 0:
            raise _fail(400, f"Ask for between 1 and {MAX_PAGE} rows at a time.")
        names = {l.id: l.name for l in db.list_libraries()}
        totals = db.queue_totals()
        return {
            "total_files": totals["files"],
            "total_bytes": totals["bytes"],
            "items": [{"id": f.id, "name": os.path.basename(f.path.replace("\\", "/")),
                       "library": names.get(f.library_id, ""), "size": f.size,
                       "resolution": f.resolution}
                      for f in db.queued_files(limit, offset)],
        }

    @api.get("/history")
    def history(library_id: Optional[int] = None, limit: int = 100, offset: int = 0,
                show: str = "all"):
        if not 1 <= limit <= MAX_PAGE or offset < 0:
            raise _fail(400, f"Ask for between 1 and {MAX_PAGE} rows at a time.")
        if show != "all" and show not in HISTORY_KINDS:
            raise _fail(400, "That isn't one of the choices for what to show.")
        names = {l.id: l.name for l in db.list_libraries()}

        def base_name(path):
            return os.path.basename(path.replace("\\", "/")) if path else ""

        def run_row(item):
            run = db.run_record(item["id"])
            file = db.get_file(run["file_id"])
            path = run.get("file_path") or (file.path if file else "")
            # The file's own status and reason say what is true now; a run that was only
            # interrupted or paused is not a failure.
            if item["status"] == "interrupted":
                reason = run["error"]
            else:
                reason = file.reason if file else None
            return {
                "kind": "run",
                "id": run["id"],
                "file_id": run["file_id"],
                "library_id": run["library_id"],
                "library": names.get(run["library_id"]),
                "name": base_name(path),
                "path": path,
                "now_path": run["now_path"],
                "state": run["state"],
                "status": item["status"],
                "reason": reason,
                "was": {"size": run["was_size"], "format": run["was_format"],
                        "bitrate": run["was_bitrate"]},
                "now": {"size": run["now_size"], "format": run["now_format"],
                        "bitrate": run["now_bitrate"]},
                "saved_bytes": run["saved_bytes"],
                "why": run["why"],
                "started_at": run["started_at"],
                "finished_at": run["finished_at"],
                "updated_at": item["at"],
            }

        def file_row(item):
            file = db.get_file(item["id"])
            return {
                "kind": "file",
                "id": f"file-{file.id}",  # run ids are numbers, and the page tells rows apart by id
                "file_id": file.id,
                "library_id": file.library_id,
                "library": names.get(file.library_id),
                "name": base_name(file.path),
                "path": file.path,
                "now_path": None,
                "state": None,
                "status": file.status,
                "reason": file.reason,
                "was": {"size": file.size,
                        "format": _format_name(file.codec) if file.codec else None,
                        "bitrate": None},
                "now": {"size": None, "format": None, "bitrate": None},
                "saved_bytes": 0,
                "why": None,
                "started_at": None,
                "finished_at": None,
                "updated_at": item["at"],
            }

        items = db.timeline(library_id, limit, offset, HISTORY_KINDS.get(show))
        return [run_row(i) if i["kind"] == "run" else file_row(i) for i in items]

    @api.get("/files/{file_id}")
    def file_details(file_id: int):
        details = db.file_details(file_id)
        if details["file"] is None:
            raise _fail(404, "That file isn't in the list any more.")
        runs = []
        for run in details["runs"]:
            run = dict(run)
            run.pop("swap_json", None)
            run["tracks"] = _parsed(run.pop("tracks_json", None))
            run["checks"] = _parsed(run.pop("checks_json", None))
            runs.append(run)
        return {"file": details["file"], "runs": runs}

    @api.post("/files/{file_id}/retry")
    def retry(file_id: int):
        file = db.get_file(file_id)
        if file is None:
            raise _fail(404, "That file isn't in the list any more.")
        kept = any(r.file_id == file_id for r in db.active_runs(("swapping", "waiting_in_use")))
        if (not kept and file.status not in ("failed", "waiting_in_use")
                and file.reason != IGNORED_REASON):
            raise _fail(400, "This file isn't waiting to be tried again.")
        worker.retry(file_id)
        return {"ok": True}

    @api.post("/files/{file_id}/ignore")
    def ignore(file_id: int):
        file = db.get_file(file_id)
        if file is None:
            raise _fail(404, "That file isn't in the list any more.")
        if file.status != "failed":
            raise _fail(400, "Only a failed file can be ignored. Nothing was changed.")
        if any(r.file_id == file_id for r in db.active_runs(ACTIVE_RUN_STATES)):
            raise _fail(409, "ShelfSaver still has unfinished work on this file. "
                             "Nothing was changed.")
        try:
            found = os.stat(swap.ext_path(file.path))
        except OSError:
            raise _fail(400, "ShelfSaver can't find this file, so there is nothing to ignore. "
                             "Nothing was changed.")
        if (found.st_size, found.st_mtime) != (file.size, file.mtime):
            raise _fail(409, "This file has changed since it failed, so it may be a new copy. "
                             "Nothing was changed.")
        logger.info("Ignoring %s at the owner's request (failed: %s)", file.path, file.reason)
        db.set_file_status(file_id, "skipped", IGNORED_REASON)
        return {"ok": True}

    @api.post("/files/{file_id}/resolve")
    def resolve(file_id: int, body: Optional[ConfirmBody] = None):
        if body is None or body.confirm is not True:
            raise _fail(400, "Please confirm that you have dealt with this file's leftover "
                             "files.")
        if db.get_file(file_id) is None:
            raise _fail(404, "That file isn't in the list any more.")
        outcome, held = worker.resolve_swap(file_id)
        if outcome == "held":
            where = f" It is at {held}." if held else ""
            raise _fail(400, "The original file isn't in its place, and its copy may be in "
                             f"the holding folder.{where} Put the file back or check the "
                             "ShelfSaver folder on this drive first. Nothing was changed.")
        if outcome == "busy":
            raise _fail(409, "ShelfSaver is working on this file right now. "
                             "Try again in a moment.")
        if outcome != "resolved":
            raise _fail(400, "This file has no unfinished replacement to close.")
        return {"ok": True}

    @api.post("/files/{file_id}/delete")
    def delete_failed_file(file_id: int, body: Optional[ConfirmBody] = None):
        if body is None or body.confirm is not True:
            raise _fail(400, "Please confirm that this file should be deleted for good.")
        file = db.get_file(file_id)
        if file is None:
            raise _fail(404, "That file isn't in the list any more.")
        if file.status != "failed":
            raise _fail(400, "Only a failed file can be deleted. Nothing was changed.")
        if any(r.file_id == file_id for r in db.active_runs(ACTIVE_RUN_STATES)):
            raise _fail(409, "ShelfSaver still has unfinished work on this file, and it could be "
                             "the only good copy. Nothing was changed.")
        library = next((l for l in db.list_libraries() if l.id == file.library_id), None)
        path = os.path.abspath(file.path)
        if (library is None or not swap._is_inside(path, library.path)
                or swap._is_inside(path, swap.work_root(path))):
            raise _fail(400, "This file isn't in one of your library folders, so ShelfSaver won't "
                             "delete it. Nothing was changed.")
        if not os.path.isdir(swap.ext_path(library.path)):
            raise _fail(400, "The library drive can't be reached. Nothing was changed.")
        try:
            found = os.lstat(swap.ext_path(path))
        except FileNotFoundError:
            logger.info("Removed %s from the list, as it was already gone", path)
            db.delete_file(file_id)
            return {"ok": True}
        except OSError as exc:
            logger.warning("Could not check %s before deleting it: %s", path, exc)
            raise _fail(409, "ShelfSaver couldn't check this file. Nothing was changed.")
        if not stat.S_ISREG(found.st_mode):
            raise _fail(400, "That isn't an ordinary file, so ShelfSaver won't delete it. "
                             "Nothing was changed.")
        if (found.st_size, found.st_mtime) != (file.size, file.mtime):
            raise _fail(409, "This file has changed since it failed, so it may be a new copy. "
                             "Nothing was changed.")
        try:
            os.unlink(swap.ext_path(path))
        except FileNotFoundError:
            pass
        except PermissionError as exc:
            logger.warning("Could not delete %s: %s", path, exc)
            raise _fail(409, "This file is open in another program or is read-only. "
                             "Nothing was changed.")
        except OSError as exc:
            logger.warning("Could not delete %s: %s", path, exc)
            raise _fail(409, "ShelfSaver couldn't delete this file. Nothing was changed.")
        logger.info("Deleted %s at the owner's request (%s bytes, failed: %s)", path, file.size,
                    file.reason)
        db.delete_file(file_id)
        return {"ok": True}

    # ---- libraries

    def clean_name(raw: Optional[str]) -> str:
        name = (raw or "").strip()
        if not name:
            raise _fail(400, "Give the library a name.")
        return name

    def clean_level(raw: Optional[str]) -> str:
        if raw not in LEVELS:
            raise _fail(400, "Choose how much to save: low, medium or high.")
        return raw

    @api.get("/libraries")
    def libraries():
        return [library_json(l) for l in db.list_libraries()]

    @api.post("/libraries")
    def add_library(body: LibraryBody):
        name = clean_name(body.name)
        level = clean_level(body.level)
        path = check_folder(body.path, "library")
        if swap.is_volume_root(path):
            raise _fail(400, folders.WHOLE_DRIVE)
        for other in db.list_libraries():
            if _overlaps(path, other.path):
                raise _fail(400, f"That folder overlaps the library \"{other.name}\". "
                                 "Libraries can't share or contain each other's folders.")
        scratch = scratch_setting()
        if scratch and _overlaps(path, scratch):
            raise _fail(400, "That folder is the scratch disk folder or overlaps it.")
        library = db.add_library(name, path, level, body.english_only, body.shrink_h265)
        remember_volume(path)
        _in_background("scan", scanner.refresh_watches, lambda: scanner.scan_library(library.id))
        return library_json(library)

    @api.patch("/libraries/{library_id}")
    def change_library(library_id: int, body: LibraryPatch):
        library = library_or_404(library_id)
        fields = {}
        if body.name is not None:
            fields["name"] = clean_name(body.name)
        if body.level is not None:
            fields["level"] = clean_level(body.level)
        if body.english_only is not None:
            fields["english_only"] = body.english_only
        if body.shrink_h265 is not None:
            fields["shrink_h265"] = body.shrink_h265
        db.update_library(library_id, **fields)
        level_changed = fields.get("level", library.level) != library.level
        if level_changed:
            db.forget_saving_results(library_id)  # a new level gives a different saving
        if (level_changed
                or fields.get("english_only", library.english_only) != library.english_only
                or fields.get("shrink_h265", library.shrink_h265) != library.shrink_h265):
            _in_background("reevaluate", lambda: scanner.reevaluate(library_id))
        return library_json(library_or_404(library_id))

    @api.delete("/libraries/{library_id}")
    def remove_library(library_id: int):
        library = library_or_404(library_id)
        if library_id in busy_library_ids():
            raise _fail(400, STILL_FINISHING)
        remember_volume(library.path)  # its held files must stay visible and clearable
        db.remove_library(library_id)
        try:
            scanner.refresh_watches()
        except Exception:
            logger.exception("Could not update the folder watches")
        return {"ok": True}

    # ---- browse

    @api.get("/browse")
    def browse(path: Optional[str] = None):
        # The picker is also used to choose the scratch folder, so it offers both kinds of root.
        try:
            return folders.browse(path, config.media_roots + config.scratch_roots)
        except folders.FolderProblem as problem:
            raise _fail(problem.status, problem.message)

    # ---- settings and holding

    def language_json() -> dict:
        language = load_language(db.get_setting("default_language"))
        return {"default_language": language, "language_name": language_name(language)}

    @api.get("/settings")
    def get_settings(request: Request):
        info = held_info()
        token = ensure_mcp_token(db)
        host = request.headers.get("host") or ""
        if not SAFE_HOST.fullmatch(host):
            host = f"YOUR-PC:{config.port}"
        command = (f'claude mcp add --transport http shelfsaver http://{host}/mcp '
                   f'--header "Authorization: Bearer {token}"')
        return {
            "scratch_dir": scratch_setting(),
            "holding_enabled": db.get_setting("holding_enabled") == "1",
            "blackouts": load_blackouts(db.get_setting("blackouts")),
            **language_json(),
            "languages": language_choices(),
            "language_libraries": [l.name for l in db.list_libraries() if l.english_only],
            "held_bytes": info["held_bytes"],
            "held_libraries": info["held_libraries"],
            "held_drives": info["held_drives"],
            "mcp_token": token,
            "mcp_command": command,
        }

    @api.patch("/settings")
    def change_settings(body: SettingsPatch):
        blackouts = None
        if body.blackouts is not None:
            try:
                blackouts = clean_blackouts(body.blackouts)
            except ValueError as exc:
                raise _fail(400, str(exc))
        language = None
        if body.default_language is not None:
            try:
                language = clean_language(body.default_language)
            except ValueError as exc:
                raise _fail(400, str(exc))
        if body.scratch_dir is not None:
            scratch = check_folder(body.scratch_dir, "scratch")
            for library in db.list_libraries():
                if _overlaps(scratch, library.path):
                    raise _fail(400, f"The scratch folder can't be inside or contain the "
                                     f"library \"{library.name}\".")
            if db.active_runs(("running",)):
                raise _fail(400, "A file is being converted right now. Change the scratch "
                                 "disk when it's finished.")
            try:
                with tempfile.TemporaryFile(dir=scratch):
                    pass
            except OSError:
                raise _fail(400, "ShelfSaver can't write to that scratch folder.")
            db.set_setting("scratch_dir", scratch)
        if body.holding_enabled is not None:
            db.set_setting("holding_enabled", "1" if body.holding_enabled else "0")
        if blackouts is not None:
            db.set_setting("blackouts", json.dumps(blackouts))
        if language is not None and language != language_json()["default_language"]:
            db.set_setting("default_language", language)
            affected = [l.id for l in db.list_libraries() if l.english_only]
            _in_background("reevaluate", *[lambda library_id=library_id: scanner.reevaluate(library_id)
                                           for library_id in affected])
        return {"scratch_dir": scratch_setting(),
                "holding_enabled": db.get_setting("holding_enabled") == "1",
                "blackouts": load_blackouts(db.get_setting("blackouts")),
                **language_json()}

    @api.post("/holding/clear")
    def clear_holding(body: Optional[ConfirmBody] = None):
        if body is None or body.confirm is not True:
            raise _fail(400, "Please confirm that you want to empty the holding folder.")
        paths = held_paths()
        freed = worker.clear_holding_when_idle(paths, ACTIVE_RUN_STATES)
        if freed is None:
            raise _fail(400, HOLDING_BUSY)
        return {"freed_bytes": freed,
                "libraries": [l.name for l in db.list_libraries()],
                "drives": _drive_list(paths),
                "held_bytes": held_info()["held_bytes"]}

    app.include_router(open_api)
    app.include_router(api)

    # The pages hold no private data, so they load before sign-in. Their script asks the API
    # for everything and sends the visitor to /login when the API says 401.
    def page_route(file_name: str):
        def serve() -> FileResponse:
            return FileResponse(STATIC_DIR / file_name, media_type="text/html",
                                headers={"Cache-Control": "no-cache"})
        return serve

    for route, file_name in PAGES.items():
        app.add_api_route(route, page_route(file_name), methods=["GET"],
                          include_in_schema=False)
    app.mount("/static", _FreshStaticFiles(directory=STATIC_DIR), name="static")
    # Added last so it wraps everything: /mcp never reaches the pages or the sign-in routes.
    app.add_middleware(McpGate, db=db, mcp_app=mcp_app)
    return app
