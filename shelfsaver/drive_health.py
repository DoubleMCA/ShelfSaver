"""Drive health: is every disk behind ShelfSaver's drives OK, according to Windows?

Why this exists: the owner's server keeps its libraries on a Storage Spaces array, and a dying
disk there should be noticed before the array is lost. Windows already rates every disk and every
storage pool (the rating is built from the disks' SMART data), so ShelfSaver asks Windows and
repeats what it says. It never judges temperatures or error counts itself: those are shown, but
only Windows' own rating raises a warning, so the warning means something when it appears.

The script only runs Get- commands: it reads, and never changes a disk or a setting.
"""
from __future__ import annotations

import base64
import json
import logging
import re
import subprocess
import threading
import time
from typing import Any, Callable, Optional

from . import hostos

logger = logging.getLogger(__name__)

CHECK_SECONDS = 3600.0            # the background check runs this often
FAILING_NOTICE_SECONDS = 86400.0  # checks failing this long in a row get a notice on Home
TIMEOUT_SECONDS = 60

HEALTHY = "Healthy"
# Operational states that mean a disk is failing even while its health still reads Healthy.
FAILING_STATES = ("Predictive Failure", "Lost Communication", "Failed")

_COUNTERS = {"temperature": "Temperature", "power_on_hours": "PowerOnHours",
             "read_errors": "ReadErrorsUncorrected", "write_errors": "WriteErrorsUncorrected",
             "wear": "Wear"}


def _as_list(value: Any) -> list:
    """PowerShell's ConvertTo-Json writes a one-item list as the item itself."""
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _text(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, list):
        parts = [str(v) for v in value if v is not None and str(v) != ""]
        return ", ".join(parts) or None
    text = str(value).strip()
    return text or None


def _number(value: Any) -> Optional[int]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _disk(raw: dict) -> dict:
    disk = {"name": _text(raw.get("FriendlyName")) or "Unnamed disk",
            "media": _text(raw.get("MediaType")), "bus": _text(raw.get("BusType")),
            "size": _number(raw.get("Size")), "health": _text(raw.get("HealthStatus")),
            "operational": _text(raw.get("OperationalStatus"))}
    for key, name in _COUNTERS.items():
        disk[key] = _number(raw.get(name))
    return disk


def parse_report(raw: dict) -> list[dict]:
    """The drives in a report from the script, each with its pool (if any) and disks."""
    drives = []
    for item in _as_list((raw or {}).get("drives")):
        if not isinstance(item, dict):
            continue
        pool = item.get("pool")
        drives.append({
            "letter": _text(item.get("letter")),
            "kind": item.get("kind") if item.get("kind") in ("pool", "disk") else "unknown",
            "pool": {"name": _text(pool.get("FriendlyName")) or "Storage pool",
                     "health": _text(pool.get("HealthStatus")),
                     "operational": _text(pool.get("OperationalStatus"))}
            if isinstance(pool, dict) else None,
            "disks": [_disk(d) for d in _as_list(item.get("disks")) if isinstance(d, dict)],
        })
    return drives


def _bad_health(health: Optional[str]) -> bool:
    # Unknown or unreadable is not a warning: only a rating Windows actually gave counts.
    return health is not None and health != HEALTHY


def _failing_state(operational: Optional[str]) -> Optional[str]:
    for state in FAILING_STATES:
        if operational and state in operational.split(", "):
            return state
    return None


def problems(drives: list[dict]) -> list[dict]:
    """Every pool or disk Windows rates as not healthy: letter, rating and what it is."""
    found = []
    for drive in drives:
        pool = drive.get("pool")
        if pool and _bad_health(pool["health"]):
            found.append({"letter": drive["letter"], "rating": pool["health"],
                          "what": f'the storage pool "{pool["name"]}"'})
        for disk in drive.get("disks", []):
            if _bad_health(disk["health"]):
                rating = disk["health"]
            else:
                rating = _failing_state(disk["operational"])
            if rating:
                found.append({"letter": drive["letter"], "rating": rating, "what": disk["name"]})
    return found


def drive_letter(path: Optional[str]) -> Optional[str]:
    """The drive letter of a Windows path ("F" for F:\\TV), or None (network or Linux path)."""
    match = re.match(r"^([A-Za-z]):", path or "")
    return match.group(1).upper() if match else None


# ---- asking Windows

# One read-only script per check, for all letters at once. For each letter: the partition's disk;
# if that disk is a Storage Spaces virtual disk, its pool and every physical disk in the pool;
# otherwise the one physical disk. The counters need admin rights (ShelfSaver runs as SYSTEM) and
# some controllers hide them, so a disk without counters is still listed. "$(...)" turns each
# status into its name, as Windows PowerShell may otherwise print numbers.
SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
function Status($value) { @($value | ForEach-Object { "$_" }) }
function DiskInfo($pd) {
  $info = [ordered]@{ FriendlyName = $pd.FriendlyName; MediaType = "$($pd.MediaType)";
    BusType = "$($pd.BusType)"; Size = $pd.Size; HealthStatus = "$($pd.HealthStatus)";
    OperationalStatus = Status $pd.OperationalStatus }
  try {
    $c = $pd | Get-StorageReliabilityCounter
    foreach ($n in 'Temperature','PowerOnHours','ReadErrorsUncorrected','WriteErrorsUncorrected','Wear') {
      $info[$n] = $c.$n }
  } catch {}
  [pscustomobject]$info
}
$found = foreach ($letter in $letters) {
  try {
    $disk = Get-Partition -DriveLetter $letter | Get-Disk
    $virtual = $null
    try { $virtual = $disk | Get-VirtualDisk } catch {}
    if ($virtual) {
      $pool = $virtual | Get-StoragePool | Select-Object -First 1
      [pscustomobject]@{ letter = $letter; kind = 'pool';
        pool = [pscustomobject]@{ FriendlyName = $pool.FriendlyName;
          HealthStatus = "$($pool.HealthStatus)"; OperationalStatus = Status $pool.OperationalStatus };
        disks = @(Get-PhysicalDisk -StoragePool $pool | ForEach-Object { DiskInfo $_ }) }
    } else {
      $physical = @(Get-PhysicalDisk | Where-Object { "$($_.DeviceId)" -eq "$($disk.Number)" })
      [pscustomobject]@{ letter = $letter; kind = 'disk';
        disks = @($physical | ForEach-Object { DiskInfo $_ }) }
    }
  } catch { [pscustomobject]@{ letter = $letter; kind = 'unknown' } }
}
@{ drives = @($found) } | ConvertTo-Json -Depth 6 -Compress
"""


class DriveCheckFailed(Exception):
    """Windows couldn't be asked, or gave no usable answer."""


def read_report(letters: list[str], run: Callable = subprocess.run) -> dict:
    """Run the script for these drive letters and return its JSON report."""
    for letter in letters:
        if not re.fullmatch(r"[A-Za-z]", letter or ""):
            raise ValueError(f"not a drive letter: {letter!r}")
    listed = ",".join(f"'{letter.upper()}'" for letter in letters)
    script = f"$letters = @({listed})\n" + SCRIPT
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    args = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-EncodedCommand", encoded]
    try:
        done = run(args, capture_output=True, text=True, timeout=TIMEOUT_SECONDS,
                   stdin=subprocess.DEVNULL,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired as exc:
        raise DriveCheckFailed("Windows took too long to answer") from exc
    except OSError as exc:
        raise DriveCheckFailed(f"PowerShell couldn't be started: {exc}") from exc
    if done.returncode != 0:
        raise DriveCheckFailed(f"PowerShell exited with code {done.returncode}")
    try:
        report = json.loads(done.stdout or "")
    except ValueError as exc:
        raise DriveCheckFailed("PowerShell gave an answer that isn't a report") from exc
    if not isinstance(report, dict):
        raise DriveCheckFailed("PowerShell gave an answer that isn't a report")
    return report


# ---- keeping the latest result

class DriveHealth:
    """The latest drive health result, shared by the page, the Home notice and the health tool.

    Only check() asks Windows; everything else reads what the last check found, so a slow or
    stuck disk query never holds up a page.
    """

    def __init__(self, db, read: Callable[[list[str]], dict] = read_report,
                 clock: Callable[[], float] = time.time, is_windows: bool = hostos.IS_WINDOWS):
        self._db = db
        self._read = read
        self._clock = clock
        self._windows = is_windows
        self._check_lock = threading.Lock()   # one check at a time
        self._lock = threading.Lock()         # guards the fields below
        self._completed = 0
        self._checked_at: Optional[float] = None
        self._drives: list[dict] = []
        self._problems: list[dict] = []
        self._failing_since: Optional[float] = None
        self._logged: Optional[set] = None    # problems already logged; None before any check

    def latest(self) -> dict:
        with self._lock:
            return {"available": self._windows, "checked_at": self._checked_at,
                    "drives": [dict(d) for d in self._drives],
                    "problems": [dict(p) for p in self._problems],
                    "failing_since": self._failing_since}

    def check(self) -> dict:
        """Ask Windows now. A caller arriving during a check waits for it and shares its result."""
        if not self._windows:
            return self.latest()
        with self._lock:
            seen = self._completed
        with self._check_lock:
            with self._lock:
                shared = self._completed > seen
            if shared:
                return self.latest()  # a check finished while this one waited
            try:
                self._check_now()
            finally:
                with self._lock:
                    self._completed += 1
        return self.latest()

    def notices(self) -> list[str]:
        """What Home should say: one line per drive with a problem, and a check failing a day."""
        if not self._windows:
            return []
        with self._lock:
            found, failing = list(self._problems), self._failing_since
        lines, letters = [], []
        for problem in found:
            if problem["letter"] not in letters:
                letters.append(problem["letter"])
                lines.append(f"A disk behind {problem['letter']}: reports {problem['rating']}. "
                             "Open Settings, then Drive health.")
        if failing is not None and self._clock() - failing >= FAILING_NOTICE_SECONDS:
            since = time.strftime("%Y-%m-%d %H:%M", time.localtime(failing))
            lines.append(f"ShelfSaver couldn't check drive health since {since}.")
        return lines

    def run_forever(self, stop) -> None:
        """Check at once, then every CHECK_SECONDS until stop is set."""
        if not self._windows:
            return
        while not stop.is_set():
            try:
                self.check()
            except Exception:
                logger.exception("The drive health check hit an unexpected error")
            if stop.wait(CHECK_SECONDS):
                break

    # ---- internals

    def _uses(self) -> list[tuple[Optional[str], list[str]]]:
        """Each drive letter ShelfSaver uses (None for a path with none), with what uses it."""
        uses: dict = {}
        no_letter = []
        named = [(lib.name, lib.path) for lib in self._db.list_libraries()]
        scratch = self._db.get_setting("scratch_dir")
        if scratch:
            named.append(("Scratch", scratch))
        for name, path in named:
            letter = drive_letter(path)
            if letter is None:
                no_letter.append((None, [name]))
            else:
                uses.setdefault(letter, []).append(name)
        return list(uses.items()) + no_letter

    def _check_now(self) -> None:
        uses = self._uses()
        letters = [letter for letter, _ in uses if letter]
        try:
            report = self._read(letters) if letters else {"drives": []}
            found = {d["letter"]: d for d in parse_report(report)}
        except DriveCheckFailed as exc:
            with self._lock:
                first = self._failing_since is None
                if first:
                    self._failing_since = self._clock()
            if first:
                logger.warning("Couldn't check drive health: %s", exc)
            return
        drives = []
        for letter, users in uses:
            drive = found.get(letter) if letter else None
            if drive is None:
                drive = {"letter": letter, "kind": "unknown", "pool": None, "disks": []}
            drives.append({**drive, "used_by": users})
        found_problems = problems(drives)
        self._log_changes(found_problems)
        with self._lock:
            recovered = self._failing_since is not None
            self._drives, self._problems = drives, found_problems
            self._checked_at = self._clock()
            self._failing_since = None
        if recovered:
            logger.info("Drive health can be checked again")

    def _log_changes(self, found: list[dict]) -> None:
        keys = {(p["letter"], p["what"], p["rating"]) for p in found}
        startup = self._logged is None
        before = self._logged or set()
        for letter, what, rating in sorted(keys - before, key=str):
            logger.warning("Drive health: %s%s behind %s: reports %s",
                           "found at startup: " if startup else "", what, letter, rating)
        for letter, what, rating in sorted(before - keys, key=str):
            logger.info("Drive health: cleared: %s behind %s: no longer reports %s",
                        what, letter, rating)
        self._logged = keys
