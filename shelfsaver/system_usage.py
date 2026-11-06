"""How busy this PC is right now, for the tiles on the home page."""
from __future__ import annotations

import ctypes
import threading
import time
from ctypes import wintypes
from typing import Optional

from .mcp_server import _run_tool

CACHE_SECONDS = 1.5  # just under the pages' refresh, so several open pages share one reading
FIRST_CPU_SAMPLE_SECONDS = 0.2

_now = time.monotonic


class _MemoryStatus(ctypes.Structure):
    _fields_ = [("dwLength", wintypes.DWORD), ("dwMemoryLoad", wintypes.DWORD),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]


def ram_percent() -> Optional[int]:
    status = _MemoryStatus()
    status.dwLength = ctypes.sizeof(status)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return None
    return int(status.dwMemoryLoad)


def gpu_percent() -> Optional[int]:
    """The busier of the card and its video encoder: encoding barely shows as general GPU use."""
    out = _run_tool(["nvidia-smi", "--query-gpu=utilization.gpu,utilization.encoder",
                     "--format=csv,noheader,nounits"])
    lines = (out or "").strip().splitlines()
    if not lines:
        return None
    numbers = [int(part) for part in lines[0].split(",") if part.strip().isdigit()]
    return max(numbers) if numbers else None


def _cpu_times() -> Optional[tuple[int, int]]:
    """(idle, total) processor time since the PC started. Kernel time already includes idle."""
    idle, kernel, user = wintypes.FILETIME(), wintypes.FILETIME(), wintypes.FILETIME()
    if not ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel),
                                                 ctypes.byref(user)):
        return None

    def ticks(value):
        return (value.dwHighDateTime << 32) | value.dwLowDateTime

    return ticks(idle), ticks(kernel) + ticks(user)


class SystemUsage:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_cpu = None
        self._cached = None
        self._cached_at = 0.0

    def _cpu_percent(self) -> Optional[int]:
        now = _cpu_times()
        if self._last_cpu is None and now is not None:
            self._last_cpu = now
            time.sleep(FIRST_CPU_SAMPLE_SECONDS)
            now = _cpu_times()
        if now is None or self._last_cpu is None:
            return None
        idle = now[0] - self._last_cpu[0]
        total = now[1] - self._last_cpu[1]
        self._last_cpu = now
        return round(100 * (total - idle) / total) if total > 0 else None

    def read(self) -> dict:
        with self._lock:
            if self._cached is None or _now() - self._cached_at >= CACHE_SECONDS:
                self._cached = {"cpu": self._cpu_percent(), "gpu": gpu_percent(),
                                "ram": ram_percent()}
                self._cached_at = _now()
            return self._cached
