"""Blackout times: when ShelfSaver doesn't start new files. Days count from Monday as 0."""
from __future__ import annotations

import json
import re
from datetime import datetime, time, timedelta
from typing import Any, Optional

MAX_BLACKOUTS = 50
LONGEST_LOOK_AHEAD = timedelta(days=8)  # back-to-back blackouts could cover the whole week
_CLOCK = re.compile(r"([01]\d|2[0-3]):[0-5]\d")


def clean_blackouts(raw: Any) -> list[dict]:
    """A tidy copy of the blackouts to save. Raises ValueError with a reason to show."""
    if not isinstance(raw, list):
        raise ValueError("That request wasn't understood.")
    if len(raw) > MAX_BLACKOUTS:
        raise ValueError(f"You can have up to {MAX_BLACKOUTS} blackout times.")
    cleaned = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("That request wasn't understood.")
        days, start, end = item.get("days"), item.get("start"), item.get("end")
        if not isinstance(days, list) or not days:
            raise ValueError("Choose at least one day for the blackout.")
        if any(type(d) is not int or not 0 <= d <= 6 for d in days):
            raise ValueError("That request wasn't understood.")
        if not (isinstance(start, str) and _CLOCK.fullmatch(start)
                and isinstance(end, str) and _CLOCK.fullmatch(end)):
            raise ValueError("Enter a start time and an end time for the blackout.")
        if start == end:
            raise ValueError("A blackout's start and end times can't be the same.")
        cleaned.append({"days": sorted(set(days)), "start": start, "end": end})
    return cleaned


def load_blackouts(saved: Optional[str]) -> list[dict]:
    """The saved blackouts. A damaged setting must never stop the app, so it counts as none."""
    try:
        return clean_blackouts(json.loads(saved)) if saved else []
    except ValueError:
        return []


def _end_of_blackout_covering(blackouts: list[dict], moment: datetime) -> Optional[datetime]:
    latest = None
    for blackout in blackouts:
        start, end = time.fromisoformat(blackout["start"]), time.fromisoformat(blackout["end"])
        # One that began yesterday can still be running, if it goes past midnight.
        for date in (moment.date() - timedelta(days=1), moment.date()):
            if date.weekday() not in blackout["days"]:
                continue
            begins, ends = datetime.combine(date, start), datetime.combine(date, end)
            if ends <= begins:
                ends += timedelta(days=1)
            if begins <= moment < ends and (latest is None or ends > latest):
                latest = ends
    return latest


def blackout_end(blackouts: list[dict], now: datetime) -> Optional[datetime]:
    """When the blackout covering `now` is over, following any that overlap or touch it.
    None when there is no blackout right now."""
    end = _end_of_blackout_covering(blackouts, now)
    while end is not None and end < now + LONGEST_LOOK_AHEAD:
        later = _end_of_blackout_covering(blackouts, end)
        if later is None:
            break
        end = later
    return end
