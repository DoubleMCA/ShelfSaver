"""The web API: sign-in, libraries, history, settings and controls. All JSON under /api."""
from __future__ import annotations

import contextlib
import ctypes
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import string
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
from ..db import ensure_mcp_token
from ..mcp_server import McpGate, build_mcp, mcp_transport_security

logger = logging.getLogger(__name__)

SESSION_COOKIE = "shelfsaver_session"
SESSION_SECONDS = 30 * 24 * 3600
MIN_PASSWORD_LENGTH = 8
LOGIN_FAILURES = 5
LOGIN_WINDOW_SECONDS = 60.0
LEVELS = ("low", "medium", "high")
ACTIVE_RUN_STATES = ("running", "swapping", "waiting_in_use")
MAX_PAGE = 500
STATIC_DIR = Path(__file__).parent / "static"
PAGES = {"/": "index.html", "/library": "library.html", "/settings": "settings.html",
         "/login": "login.html"}
DRIVE_REMOTE = 4
HIDDEN_OR_SYSTEM = 0x2 | 0x4  # FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM

WHOLE_DRIVE = "Choose a folder on the drive, not the whole drive."
NOT_LOCAL = "Choose a folder on a drive in this PC. Network folders aren't supported."
STILL_FINISHING = ("A file in this library is still being finished. "
                   "Try again when it's done.")
HOLDING_BUSY = ("A file is being replaced or finished, and a held file can be the only copy "
                "right now. If a conversion is in progress, pause it and wait for it to "
                "stop. If a file is waiting or needs attention, wait for it, or use Try again "
                "or the resolve action on that file.")
IN_WORK_FOLDER = ("That folder is inside the ShelfSaver folder that holds files being replaced. "
                  "Choose a different folder.")
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
    english_only: StrictBool = True


class LibraryPatch(BaseModel):
    name: Optional[str] = None
    level: Optional[str] = None
    english_only: Optional[StrictBool] = None


class SettingsPatch(BaseModel):
    scratch_dir: Optional[str] = None
    holding_enabled: Optional[StrictBool] = None


# ---- path helpers

def _fail(status: int, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail=message)


def _norm(path: str) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(path)))


def _overlaps(first: str, second: str) -> bool:
    a, b = _norm(first), _norm(second)
    return (a == b or a.startswith(b.rstrip("\\/") + os.sep)
            or b.startswith(a.rstrip("\\/") + os.sep))


def _drive_type(path: str) -> int:
    drive = os.path.splitdrive(os.path.abspath(path))[0]
    try:
        return ctypes.windll.kernel32.GetDriveTypeW(drive + "\\")
    except (AttributeError, OSError):
        return 0


def _check_local_folder(raw: str, what: str) -> str:
    """Return the cleaned path of an existing local folder, or raise a plain 400."""
    text = (raw or "").strip()
    if not text:
        raise _fail(400, f"Choose a {what} folder.")
    if text.startswith(("\\\\", "//")):
        raise _fail(400, NOT_LOCAL)
    if not os.path.isabs(text) or not os.path.splitdrive(text)[0]:
        raise _fail(400, f"Enter the full path of the {what} folder, such as D:\\Movies.")
    path = os.path.normpath(text)
    if _drive_type(path) == DRIVE_REMOTE:
        raise _fail(400, NOT_LOCAL)
    try:
        is_folder = os.path.isdir(swap.ext_path(path))
    except OSError:
        is_folder = False
    if not is_folder:
        raise _fail(400, f"That {what} folder can't be found. Check the path and that the "
                         "drive is connected.")
    if swap._is_inside(os.path.abspath(path), swap.work_root(path)):
        raise _fail(400, IN_WORK_FOLDER)
    return path


def _is_visible_folder(entry: os.DirEntry) -> bool:
    if not entry.is_dir(follow_symlinks=False):
        return False
    if entry.name.startswith("$") or entry.name.lower() == "system volume information":
        return False
    return not (entry.stat(follow_symlinks=False).st_file_attributes & HIDDEN_OR_SYSTEM)


def _drives() -> list[dict]:
    found = []
    for letter in string.ascii_uppercase:
        root = f"{letter}:\\"
        if os.path.isdir(root) and _drive_type(root) != DRIVE_REMOTE:
            found.append({"name": root, "path": root})
    return found


def _drive_list(libraries) -> list[str]:
    drives = []
    for library in libraries:
        try:
            drive = str(swap.work_root(library.path).parent)
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
                "status": library.status, "stats": db.stats(library.id)}

    def busy_library_ids() -> set:
        busy = set()
        for run in db.active_runs(ACTIVE_RUN_STATES):
            record = db.run_record(run.id) or {}
            file = db.get_file(run.file_id)
            busy.add(record.get("library_id") or (file.library_id if file else None))
        for status in ("working", "waiting_in_use"):
            busy.update(f.library_id for f in db.files_with_status(status))
        return busy

    def held_info() -> dict:
        libraries = db.list_libraries()
        try:
            held = swap.held_size([l.path for l in libraries])
        except Exception:
            logger.exception("Could not measure the holding folder")
            held = 0
        return {"held_bytes": held,
                "held_libraries": [l.name for l in libraries],
                "held_drives": _drive_list(libraries)}

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
            "held_bytes": held_info()["held_bytes"],
            "offline_libraries": [{"id": l.id, "name": l.name}
                                  for l in db.list_libraries() if l.status == "offline"],
        }

    @api.post("/pause")
    def pause():
        worker.pause()
        return {"worker": worker.status()}

    @api.post("/resume")
    def resume():
        worker.resume()
        return {"worker": worker.status()}

    # ---- history and files

    @api.get("/history")
    def history(library_id: Optional[int] = None, limit: int = 100, offset: int = 0):
        if not 1 <= limit <= MAX_PAGE or offset < 0:
            raise _fail(400, f"Ask for between 1 and {MAX_PAGE} rows at a time.")
        names = {l.id: l.name for l in db.list_libraries()}

        def was_interrupted(run, file):
            # A restart ended this run; if the file has since failed, that was a later run.
            return run["state"] == "interrupted" and (file is None or file.status == "failed")

        def status_of(run, file):
            if was_interrupted(run, file):
                return "interrupted"
            return file.status if file else run["state"]

        def reason_of(run, file):
            if was_interrupted(run, file):
                return run["error"]
            return file.reason if file else None

        rows = []
        for run in db.history(library_id, limit, offset):
            file = db.get_file(run["file_id"])
            path = run.get("file_path") or (file.path if file else "")
            rows.append({
                "id": run["id"],
                "file_id": run["file_id"],
                "library_id": run["library_id"],
                "library": names.get(run["library_id"]),
                "name": os.path.basename(path.replace("\\", "/")) if path else "",
                "path": path,
                "now_path": run["now_path"],
                "state": run["state"],
                # The file's own status and reason say what is true now; a run that was only
                # interrupted or paused is not a failure.
                "status": status_of(run, file),
                "reason": reason_of(run, file),
                "was": {"size": run["was_size"], "format": run["was_format"],
                        "bitrate": run["was_bitrate"]},
                "now": {"size": run["now_size"], "format": run["now_format"],
                        "bitrate": run["now_bitrate"]},
                "saved_bytes": run["saved_bytes"],
                "why": run["why"],
                "started_at": run["started_at"],
                "finished_at": run["finished_at"],
            })
        return rows

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
        if not kept and file.status not in ("failed", "waiting_in_use"):
            raise _fail(400, "This file isn't waiting to be tried again.")
        worker.retry(file_id)
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
        path = _check_local_folder(body.path, "library")
        if swap.is_volume_root(path):
            raise _fail(400, WHOLE_DRIVE)
        for other in db.list_libraries():
            if _overlaps(path, other.path):
                raise _fail(400, f"That folder overlaps the library \"{other.name}\". "
                                 "Libraries can't share or contain each other's folders.")
        scratch = scratch_setting()
        if scratch and _overlaps(path, scratch):
            raise _fail(400, "That folder is the scratch disk folder or overlaps it.")
        library = db.add_library(name, path, level, body.english_only)
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
        db.update_library(library_id, **fields)
        if (fields.get("level", library.level) != library.level
                or fields.get("english_only", library.english_only) != library.english_only):
            _in_background("reevaluate", lambda: scanner.reevaluate(library_id))
        return library_json(library_or_404(library_id))

    @api.delete("/libraries/{library_id}")
    def remove_library(library_id: int):
        library_or_404(library_id)
        if library_id in busy_library_ids():
            raise _fail(400, STILL_FINISHING)
        db.remove_library(library_id)
        try:
            scanner.refresh_watches()
        except Exception:
            logger.exception("Could not update the folder watches")
        return {"ok": True}

    # ---- browse

    @api.get("/browse")
    def browse(path: Optional[str] = None):
        if not path:
            return {"path": None, "parent": None, "folders": _drives()}
        if path.startswith(("\\\\", "//")):
            raise _fail(400, NOT_LOCAL)
        if not os.path.isabs(path) or not os.path.splitdrive(path)[0]:
            raise _fail(400, "Enter the full path of a folder.")
        folder = os.path.normpath(path)
        if not os.path.isdir(folder):
            raise _fail(400, "That folder can't be found.")
        try:
            with os.scandir(folder) as entries:
                listing = list(entries)
        except OSError:
            raise _fail(403, "ShelfSaver isn't allowed to open that folder.")
        folders = []
        for entry in listing:
            try:
                if _is_visible_folder(entry):
                    folders.append({"name": entry.name, "path": os.path.join(folder, entry.name)})
            except OSError:
                continue  # no permission for this one: leave it out
        folders.sort(key=lambda f: f["name"].lower())
        parent = os.path.dirname(folder.rstrip("\\/"))
        at_root = os.path.splitdrive(folder)[1] in ("\\", "/") or parent == folder
        return {"path": folder, "parent": None if at_root else parent, "folders": folders}

    # ---- settings and holding

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
            "held_bytes": info["held_bytes"],
            "held_libraries": info["held_libraries"],
            "held_drives": info["held_drives"],
            "mcp_token": token,
            "mcp_command": command,
        }

    @api.patch("/settings")
    def change_settings(body: SettingsPatch):
        if body.scratch_dir is not None:
            scratch = _check_local_folder(body.scratch_dir, "scratch")
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
        return {"scratch_dir": scratch_setting(),
                "holding_enabled": db.get_setting("holding_enabled") == "1"}

    @api.post("/holding/clear")
    def clear_holding(body: Optional[ConfirmBody] = None):
        if body is None or body.confirm is not True:
            raise _fail(400, "Please confirm that you want to empty the holding folder.")
        libraries = db.list_libraries()
        freed = worker.clear_holding_when_idle([l.path for l in libraries], ACTIVE_RUN_STATES)
        if freed is None:
            raise _fail(400, HOLDING_BUSY)
        return {"freed_bytes": freed,
                "libraries": [l.name for l in libraries],
                "drives": _drive_list(libraries),
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
