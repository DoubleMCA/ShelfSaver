"""CPU, GPU and RAM use for the tiles on the home page."""
from __future__ import annotations

import pytest

from shelfsaver import system_usage as su


def _smi(monkeypatch, output):
    calls = []

    def run(args):
        calls.append(args)
        return output

    monkeypatch.setattr(su, "_run_tool", run)
    return calls


@pytest.mark.parametrize("output,expected", [
    ("7, 93\n", 93),      # encoding barely shows as general GPU use
    ("80, 12\n", 80),
    ("0, 0\n", 0),
    ("41, [N/A]\n", 41),  # a card that doesn't report its encoder
    ("[N/A], [N/A]\n", None),
    ("", None),
    (None, None),         # nvidia-smi can't run
    ("not a number\n", None),
])
def test_gpu_is_the_busier_of_the_card_and_its_encoder(monkeypatch, output, expected):
    _smi(monkeypatch, output)
    assert su.gpu_percent() == expected


def test_gpu_asks_for_both_numbers_in_plain_form(monkeypatch):
    calls = _smi(monkeypatch, "1, 2\n")
    su.gpu_percent()
    assert calls == [["nvidia-smi", "--query-gpu=utilization.gpu,utilization.encoder",
                      "--format=csv,noheader,nounits"]]


def test_ram_comes_from_the_operating_system():
    assert 0 < su.ram_percent() <= 100


def test_cpu_times_come_from_the_operating_system():
    idle, total = su._cpu_times()
    assert 0 <= idle <= total and total > 0


MEMINFO = "MemTotal:       32000000 kB\nMemFree:         1000000 kB\nMemAvailable:   20000000 kB\n"


def test_linux_ram_counts_reclaimable_cache_as_free():
    # 12M of 32M really in use = 37.5%, even though only 1M is literally "free".
    assert su._meminfo_percent(MEMINFO) == 38


@pytest.mark.parametrize("text", ["", "garbage", "MemTotal: 5 kB\n", "MemTotal: 0 kB\nMemAvailable: 0 kB\n"])
def test_linux_ram_is_unknown_when_meminfo_is_unusable(text):
    assert su._meminfo_percent(text) is None


def test_linux_cpu_idle_includes_iowait_and_guest_is_not_double_counted():
    #            user nice sys idle iowait irq soft steal guest guest_nice
    stat = "cpu  100  10   40  800  50     0   0    0     25    5\ncpu0 1 1 1 1 1 1 1 1 1 1\n"
    # idle = 800 + 50; total = first eight fields only (guest is already inside user).
    assert su._proc_stat_times(stat) == (850, 1000)


@pytest.mark.parametrize("text", ["", "cpu0 1 2 3", "cpu 1 2 3", "cpu a b c d e f g h"])
def test_linux_cpu_is_unknown_when_stat_is_unusable(text):
    assert su._proc_stat_times(text) is None


def _quiet_usage(monkeypatch, times):
    samples = iter(times)
    monkeypatch.setattr(su, "_cpu_times", lambda: next(samples))
    monkeypatch.setattr(su, "gpu_percent", lambda *encoder: 5)
    monkeypatch.setattr(su, "ram_percent", lambda: 6)
    monkeypatch.setattr(su.time, "sleep", lambda seconds: None)
    clock = [1000.0]
    monkeypatch.setattr(su, "_now", lambda: clock[0])
    return su.SystemUsage(), clock


def test_cpu_is_the_busy_share_since_the_last_reading(monkeypatch):
    usage, clock = _quiet_usage(monkeypatch, [(100, 200), (150, 300), (150, 400)])
    assert usage.read() == {"cpu": 50, "gpu": 5, "ram": 6}
    clock[0] += su.CACHE_SECONDS
    assert usage.read()["cpu"] == 100


def test_cpu_with_no_time_passed_or_no_reading_is_unknown(monkeypatch):
    usage, clock = _quiet_usage(monkeypatch, [(100, 200), (100, 200), None])
    assert usage.read()["cpu"] is None
    clock[0] += su.CACHE_SECONDS
    assert usage.read()["cpu"] is None


def test_readings_are_shared_for_a_moment(monkeypatch):
    usage, clock = _quiet_usage(monkeypatch, [(1, 2), (2, 4), (3, 6)])
    gpu = []
    monkeypatch.setattr(su, "gpu_percent", lambda *encoder: gpu.append(1) or 5)
    usage.read()
    usage.read()
    assert len(gpu) == 1
    clock[0] += su.CACHE_SECONDS
    usage.read()
    assert len(gpu) == 2
