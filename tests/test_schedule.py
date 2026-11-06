"""Blackout times: when ShelfSaver doesn't start new files."""
from __future__ import annotations

import json
from datetime import datetime

import pytest

from shelfsaver.schedule import blackout_end, clean_blackouts, load_blackouts

EVERY_DAY = [0, 1, 2, 3, 4, 5, 6]
MON, TUE, WED, SUN = 0, 1, 2, 6


def at(day, clock):  # 2026-11-02 is a Monday
    hour, minute = clock.split(":")
    return datetime(2026, 11, 2 + day, int(hour), int(minute))


def window(start, end, *days):
    return {"days": list(days) or EVERY_DAY, "start": start, "end": end}


def test_no_blackouts_means_it_always_runs():
    assert blackout_end([], at(MON, "12:00")) is None


@pytest.mark.parametrize("clock,inside", [
    ("17:59", False), ("18:00", True), ("22:59", True), ("23:00", False),
])
def test_a_blackout_covers_its_start_but_not_its_end(clock, inside):
    end = blackout_end([window("18:00", "23:00")], at(MON, clock))
    assert end == (at(MON, "23:00") if inside else None)


def test_a_blackout_only_applies_on_its_days():
    blackouts = [window("18:00", "23:00", MON)]
    assert blackout_end(blackouts, at(MON, "19:00")) == at(MON, "23:00")
    assert blackout_end(blackouts, at(TUE, "19:00")) is None


def test_an_overnight_blackout_runs_into_the_next_morning():
    blackouts = [window("22:00", "06:00", MON)]
    assert blackout_end(blackouts, at(MON, "21:59")) is None
    assert blackout_end(blackouts, at(MON, "23:00")) == at(TUE, "06:00")
    assert blackout_end(blackouts, at(TUE, "05:59")) == at(TUE, "06:00")
    assert blackout_end(blackouts, at(TUE, "06:00")) is None
    # It started on Monday, so Sunday night and Monday morning are free.
    assert blackout_end(blackouts, at(MON, "05:00")) is None


def test_an_overnight_blackout_wraps_from_sunday_to_monday():
    blackouts = [window("22:00", "06:00", SUN)]
    assert blackout_end(blackouts, at(SUN, "23:00")) == at(SUN + 1, "06:00")
    assert blackout_end(blackouts, at(MON, "05:00")) == at(MON, "06:00")


def test_overlapping_and_touching_blackouts_end_at_the_last_one():
    blackouts = [window("18:00", "20:00"), window("19:00", "21:00"), window("21:00", "22:30")]
    assert blackout_end(blackouts, at(MON, "18:30")) == at(MON, "22:30")


def test_blackouts_covering_all_week_still_give_an_answer():
    blackouts = [window("00:00", "12:00"), window("12:00", "00:00")]
    end = blackout_end(blackouts, at(MON, "09:00"))
    assert end is not None and end > at(MON, "09:00")


def test_clean_keeps_what_was_chosen_in_a_tidy_form():
    assert clean_blackouts([{"days": [2, 0, 2], "start": "18:00", "end": "23:30"}]) == [
        {"days": [0, 2], "start": "18:00", "end": "23:30"}]
    assert clean_blackouts([]) == []


@pytest.mark.parametrize("bad", [
    {"days": [], "start": "18:00", "end": "23:00"},
    {"days": [7], "start": "18:00", "end": "23:00"},
    {"days": [-1], "start": "18:00", "end": "23:00"},
    {"days": [0], "start": "", "end": "23:00"},
    {"days": [0], "start": "24:00", "end": "23:00"},
    {"days": [0], "start": "18:60", "end": "23:00"},
    {"days": [0], "start": "6pm", "end": "23:00"},
    {"days": [0], "start": "18:00", "end": "18:00"},
])
def test_clean_refuses_what_it_cannot_follow(bad):
    with pytest.raises(ValueError) as problem:
        clean_blackouts([bad])
    assert str(problem.value)


def test_clean_refuses_too_many():
    with pytest.raises(ValueError):
        clean_blackouts([window("18:00", "23:00")] * 51)


def test_load_treats_a_missing_or_damaged_setting_as_no_blackouts():
    assert load_blackouts(None) == []
    assert load_blackouts("") == []
    assert load_blackouts("not json") == []
    assert load_blackouts('{"days": []}') == []
    assert load_blackouts(json.dumps([{"days": [], "start": "1", "end": "2"}])) == []


def test_load_reads_what_was_saved():
    saved = [window("18:00", "23:00", MON, WED)]
    assert load_blackouts(json.dumps(saved)) == saved
