"""Quality levels: the settings behind Low, Medium and High."""
from __future__ import annotations

from dataclasses import dataclass

MIN_SAVINGS = 0.10


@dataclass(frozen=True)
class LevelSettings:
    cq: int
    min_mbps: dict


LEVELS: dict = {
    "low": LevelSettings(22, {"sd": 2.0, "720": 4.0, "1080": 8.0, "4k": 25.0}),
    "medium": LevelSettings(26, {"sd": 1.5, "720": 3.0, "1080": 6.0, "4k": 18.0}),
    "high": LevelSettings(30, {"sd": 1.0, "720": 2.0, "1080": 4.0, "4k": 12.0}),
}


def resolution_bucket(width: int) -> str:
    """Bucket by width so cropped (wide-aspect) films land correctly."""
    if width <= 1024:
        return "sd"
    if width <= 1400:
        return "720"
    if width <= 2200:
        return "1080"
    return "4k"
