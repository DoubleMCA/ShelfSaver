"""Drive health: reading Windows' storage report, judging it, and keeping the latest result.

No real disks are needed: reports are written by hand in the shape the PowerShell script prints,
including its habit of turning a one-item list into a single object.
"""
from __future__ import annotations

import pytest

from shelfsaver.drive_health import drive_letter, parse_report, problems


def disk(name, health="Healthy", operational="OK", media="HDD", **counters):
    raw = {"FriendlyName": name, "MediaType": media, "BusType": "SATA", "Size": 14 * 10**12,
           "HealthStatus": health, "OperationalStatus": operational}
    names = {"temperature": "Temperature", "power_on_hours": "PowerOnHours",
             "read_errors": "ReadErrorsUncorrected", "write_errors": "WriteErrorsUncorrected",
             "wear": "Wear"}
    for key, value in counters.items():
        raw[names[key]] = value
    return raw


POOL = {"letter": "F", "kind": "pool",
        "pool": {"FriendlyName": "Pool1", "HealthStatus": "Healthy", "OperationalStatus": "OK"},
        "disks": [disk("WDC A"), disk("WDC B")]}


# ---- reading the report

def test_single_objects_are_read_as_lists():
    raw = {"drives": {**POOL, "disks": disk("Only")}}
    (drive,) = parse_report(raw)
    assert drive["letter"] == "F" and [d["name"] for d in drive["disks"]] == ["Only"]


def test_counters_are_read_when_present():
    (drive,) = parse_report({"drives": [{**POOL, "disks": [disk(
        "A", temperature=34, power_on_hours=21400, read_errors=0, write_errors=0, wear=3)]}]})
    d = drive["disks"][0]
    assert (d["temperature"], d["power_on_hours"], d["read_errors"], d["write_errors"],
            d["wear"]) == (34, 21400, 0, 0, 3)
    assert (d["media"], d["bus"], d["size"]) == ("HDD", "SATA", 14 * 10**12)


def test_missing_counters_are_blank():
    (drive,) = parse_report({"drives": [{**POOL, "disks": [disk("A")]}]})
    assert drive["disks"][0]["temperature"] is None
    assert drive["disks"][0]["wear"] is None


def test_operational_status_lists_are_joined():
    (drive,) = parse_report({"drives": [{**POOL, "disks": [disk("A", operational=["OK", "In Maintenance"])]}]})
    assert drive["disks"][0]["operational"] == "OK, In Maintenance"


def test_pool_details_are_kept():
    (drive,) = parse_report({"drives": [POOL]})
    assert drive["kind"] == "pool"
    assert drive["pool"] == {"name": "Pool1", "health": "Healthy", "operational": "OK"}


def test_unknown_drive_is_listed_without_a_problem():
    (drive,) = parse_report({"drives": [{"letter": "Z", "kind": "unknown"}]})
    assert drive["kind"] == "unknown" and drive["disks"] == [] and drive["pool"] is None
    assert problems([drive]) == []


def test_an_empty_report_has_no_drives():
    assert parse_report({}) == [] and parse_report({"drives": None}) == []


# ---- judging it

def test_healthy_pool_has_no_problems():
    assert problems(parse_report({"drives": [POOL]})) == []


def test_a_degraded_pool_warns_even_with_healthy_disks():
    raw = {"drives": [{**POOL, "pool": {**POOL["pool"], "HealthStatus": "Warning",
                                        "OperationalStatus": "Degraded"}}]}
    (p,) = problems(parse_report(raw))
    assert p["letter"] == "F" and p["rating"] == "Warning" and "Pool1" in p["what"]


def test_a_degraded_storage_space_warns_while_the_pool_and_disks_read_healthy():
    """A failed disk was removed: the pool is fine again, but F:'s space has lost redundancy."""
    raw = {"drives": [{**POOL, "virtual": {"FriendlyName": "Media", "HealthStatus": "Warning",
                                           "OperationalStatus": "Degraded"}}]}
    (drive,) = parse_report(raw)
    assert drive["virtual"] == {"name": "Media", "health": "Warning", "operational": "Degraded"}
    (p,) = problems([drive])
    assert p["letter"] == "F" and p["rating"] == "Warning" and "Media" in p["what"]


def test_a_letter_that_failed_keeps_its_error():
    (drive,) = parse_report({"drives": [{"letter": "F", "kind": "unknown", "error": "WMI broke"}]})
    assert drive["error"] == "WMI broke" and problems([drive]) == []


def test_a_disk_rated_warning_warns():
    raw = {"drives": [{**POOL, "disks": [disk("WDC A"), disk("WDC B", health="Warning")]}]}
    assert [(p["what"], p["rating"]) for p in problems(parse_report(raw))] == [("WDC B", "Warning")]


@pytest.mark.parametrize("state", ["Predictive Failure", "Lost Communication", "Failed"])
def test_failing_operational_states_warn(state):
    raw = {"drives": [{**POOL, "disks": [disk("WDC A", operational=["OK", state])]}]}
    (p,) = problems(parse_report(raw))
    assert p["rating"] == state


def test_in_maintenance_and_blanks_do_not_warn():
    raw = {"drives": [{**POOL, "disks": [disk("A", operational="In Maintenance"),
                                         disk("B", health=None, operational=None)]}]}
    assert problems(parse_report(raw)) == []


def test_a_plain_disk_warns_too():
    raw = {"drives": [{"letter": "G", "kind": "disk", "disks": disk("SSD", health="Unhealthy",
                                                                     media="SSD")}]}
    (p,) = problems(parse_report(raw))
    assert (p["letter"], p["rating"], p["what"]) == ("G", "Unhealthy", "SSD")


def test_drive_letter():
    assert drive_letter("F:\\TV") == "F" and drive_letter("g:/work") == "G"
    assert drive_letter("F:") == "F"
    assert drive_letter("\\\\nas\\tv") is None and drive_letter("/mnt/tv") is None
    assert drive_letter("") is None and drive_letter(None) is None
