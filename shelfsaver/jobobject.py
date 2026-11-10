"""Tie child processes to this app so they die with it, even after a crash (Windows)."""
from __future__ import annotations

import ctypes
import logging
import threading
from ctypes import wintypes

from . import hostos

logger = logging.getLogger(__name__)

_JobObjectExtendedLimitInformation = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000

_lock = threading.Lock()
_job = None  # kept open for the life of the process; Windows closes it when we die
_failed = False


class _BasicLimits(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimits),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def _create_job():
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    k32.SetInformationJobObject.restype = wintypes.BOOL
    k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                            ctypes.c_void_p, wintypes.DWORD]
    k32.AssignProcessToJobObject.restype = wintypes.BOOL
    k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    job = k32.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    limits = _ExtendedLimits()
    limits.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not k32.SetInformationJobObject(job, _JobObjectExtendedLimitInformation,
                                       ctypes.byref(limits), ctypes.sizeof(limits)):
        raise ctypes.WinError(ctypes.get_last_error())
    return k32, job


def kill_with_app(proc) -> bool:
    """Make `proc` die when this process does. Returns False (and logs) if it can't.

    Linux: deliberately does nothing and returns False. The Linux equivalent is
    prctl(PR_SET_PDEATHSIG), but it can only be set inside the child before it starts, which
    in Python means `preexec_fn`, and Python documents `preexec_fn` as unsafe in a program
    with threads (it can deadlock); ShelfSaver is heavily threaded. This is acceptable because:
    - a normal stop or SIGTERM already makes the worker terminate ffmpeg (encode.run_ffmpeg);
    - in Docker, removing or stopping the container kills every process inside it.
    The one gap is a hard `kill -9` of ShelfSaver outside a container, which leaves ffmpeg
    to finish on its own; it writes only to the scratch folder, never to the library.
    """
    global _job, _failed
    if not hostos.IS_WINDOWS:
        return False
    try:
        with _lock:
            if _failed:
                return False
            if _job is None:
                _job = _create_job()
            k32, job = _job
        if not k32.AssignProcessToJobObject(job, int(proc._handle)):
            raise ctypes.WinError(ctypes.get_last_error())
        return True
    except Exception as exc:  # never let this stop a conversion
        if _job is None:
            _failed = True
        logger.warning("Could not tie ffmpeg to the app, so it may outlive a crash: %s", exc)
        return False
