"""Which hardware encoder to use when the setting is "auto": whichever works, best first.

Why this exists: ShelfSaver used to assume NVIDIA unless a setting said otherwise, so a PC with
only an AMD or Intel card did nothing until someone edited config.json. Now the encoder setting
defaults to "auto" and this class picks for the whole app. The worker, the usage tile and the
health tool all read the same instance, so they never disagree about the encoder in use.

The rules, all applied *between* files, never during one:
- Pinned (the setting names an encoder): that encoder, never tested here. Failures are handled
  exactly as before auto existed.
- Auto, first time: try each encoder's one-frame self-test in ORDER and take the first that works.
- Auto, nothing works: wait, and look again RETRY_SECONDS later. A graphics driver can come up
  late after a reboot, so this must never be a permanent failure.
- Auto, an encoder in use: before each file, re-test the ones ranked *above* it (each at most every
  BETTER_RETRY_SECONDS) and move up if one now works. Without this, an NVIDIA PC whose driver was
  slow after a reboot would stay on the CPU's built-in graphics until the next restart.
- Auto, after repeated failures: detect again from the top and move to whatever works.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

from .encoders import AUTO, ENCODERS, Encoder, encoder_for

logger = logging.getLogger(__name__)

# Best first. AMF is Windows-only and VAAPI Linux-only (Encoder.supported), so Windows tries
# NVIDIA, AMD, Intel, and Linux tries NVIDIA, Intel, AMD. VAAPI is last on purpose: Intel's Linux
# driver answers VAAPI too, and an Intel machine must stay on Quick Sync and its measured levels.
# A name not registered in ENCODERS is skipped.
ORDER = ("nvenc", "amf", "qsv", "vaapi")
RETRY_SECONDS = 60.0            # nothing works: look again after this
BETTER_RETRY_SECONDS = 600.0    # a higher-ranked encoder that failed: test it again after this


class EncoderChoice:
    def __init__(self, config, works: Callable[[Encoder], bool],
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._config = config
        self._works = works
        self._clock = clock
        self._lock = threading.Lock()  # web threads read current()/describe()
        self._pinned = getattr(config, "encoder", AUTO) != AUTO
        self._current: Optional[Encoder] = encoder_for(config) if self._pinned else None
        self._last_full_try: Optional[float] = None
        self._failed_at: dict[str, float] = {}  # name -> when its self-test last failed
        self._warned_none = False

    @property
    def pinned(self) -> bool:
        return self._pinned

    def current(self) -> Optional[Encoder]:
        with self._lock:
            return self._current

    def before_conversion(self) -> Optional[Encoder]:
        """The encoder for the next file, or None when none works yet."""
        if self._pinned:
            return self._current
        current = self.current()
        if current is None:
            last = self._last_full_try
            if last is not None and self._clock() - last < RETRY_SECONDS:
                return None
            return self._detect_from_top()
        for name in self._candidates():
            if name == current.name:
                break
            failed = self._failed_at.get(name)
            if failed is not None and self._clock() - failed < BETTER_RETRY_SECONDS:
                continue
            better = self._build(name)
            if self._works(better):
                logger.info("Switching to %s", better.label)
                self._set(better)
                return better
            self._failed_at[name] = self._clock()
        return current

    def after_failures(self) -> Optional[Encoder]:
        """After several failed files in a row: an encoder that works now, or None.

        Pinned: today's check of that one encoder. Auto: detect again from the top.
        """
        if self._pinned:
            return self._current if self._works(self._current) else None
        return self._detect_from_top()

    def describe(self) -> dict:
        with self._lock:
            enc = self._current
        if enc is None:
            return {"name": None, "label": None, "chosen": "none"}
        return {"name": enc.name, "label": enc.label,
                "chosen": "pinned" if self._pinned else "detected"}

    # ---- internals

    def _candidates(self) -> list:
        # supported(): this operating system at all; auto_ok(): this machine's card (VAAPI is for
        # AMD cards only). Both are cheap checks that start no program.
        return [n for n in ORDER if n in ENCODERS and ENCODERS[n].supported()
                and self._build(n).auto_ok()]

    def _build(self, name: str) -> Encoder:
        return ENCODERS[name].from_config(self._config)

    def _set(self, encoder: Optional[Encoder]) -> None:
        with self._lock:
            self._current = encoder

    def _detect_from_top(self) -> Optional[Encoder]:
        for name in self._candidates():
            candidate = self._build(name)
            if self._works(candidate):
                previous = self.current()
                if previous is None or previous.name != candidate.name:
                    logger.info("Converting with %s (detected)", candidate.label)
                self._failed_at.pop(name, None)
                self._warned_none = False
                self._set(candidate)
                return candidate
            self._failed_at[name] = self._clock()
        self._set(None)
        self._last_full_try = self._clock()
        if not self._warned_none:
            logger.warning("No graphics card encoder works (tried %s). Checking again every "
                           "minute.", ", ".join(self._candidates()) or "none")
            self._warned_none = True
        return None
