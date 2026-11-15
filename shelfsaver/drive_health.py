"""Drive health: is every disk behind ShelfSaver's drives OK, according to Windows?

Why this exists: the owner's server keeps its libraries on a Storage Spaces array, and a dying
disk there should be noticed before the array is lost. Windows already rates every disk and every
storage pool (the rating is built from the disks' SMART data), so ShelfSaver asks Windows and
repeats what it says. It never judges temperatures or error counts itself: those are shown, but
only Windows' own rating raises a warning, so the warning means something when it appears.

This part reads the report the PowerShell script prints and decides what is a problem.
"""
from __future__ import annotations

import re
from typing import Any, Optional

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
