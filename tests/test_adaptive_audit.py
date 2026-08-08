from datetime import datetime, timezone

import pytest

from smart_lidatube.audit import AuditConfig, AuditWorker, audit_rate_tier
from smart_lidatube.store import Store
from smart_lidatube.guards import HostResourceGuard


@pytest.mark.parametrize("backlog,rate", [
    (10001, 300), (10000, 200), (5001, 200), (5000, 120),
    (1001, 120), (1000, 60), (101, 60), (100, 24), (1, 24), (0, 12),
])
def test_adaptive_audit_tier_boundaries(backlog, rate):
    assert audit_rate_tier(backlog) == rate


def test_audit_summary_uses_adaptive_tier_bounded_by_shipped_max(tmp_path):
    store = Store(tmp_path / "db")
    for track_id in range(1, 102):
        store.upsert_audit_track(track_id)
    config = AuditConfig(max_per_hour=300)
    worker = AuditWorker(store, object(), object(), config)

    assert worker.refresh_throughput() == 60
    status = store.audit_status()
    assert status["backlog"] == 101
    assert status["tier_rate_per_hour"] == 60
    assert status["effective_rate_per_hour"] == 60
    assert status["eta_hours"] == pytest.approx(1.69)
    assert store.get_setting("audit_effective_rate_per_hour") == "60"

    reopened = Store(tmp_path / "db")
    assert reopened.audit_status()["effective_rate_per_hour"] == 60


def test_audit_max_caps_adaptive_tier(tmp_path):
    store = Store(tmp_path / "db")
    for track_id in range(1, 102):
        store.upsert_audit_track(track_id)
    assert AuditWorker(store, object(), object(), AuditConfig(max_per_hour=40)).refresh_throughput() == 40


def test_health_and_resource_hooks_fail_closed_before_consuming_work(tmp_path):
    store = Store(tmp_path / "db"); store.upsert_audit_track(1)
    called = []
    worker = AuditWorker(
        store, object(), object(), AuditConfig(),
        health_check=lambda: (_ for _ in ()).throw(RuntimeError("secret")),
        resource_check=lambda: called.append("resource") or True,
    )
    before = store.get_setting("audit_tokens")
    assert worker.process_once() is None
    assert called == []
    assert store.get_setting("audit_tokens") == before
    assert store.get_setting("audit_backoff_reason") == "health_unavailable"


def test_healthy_gate_clears_stale_backoff_before_candidate_selection(tmp_path):
    store = Store(tmp_path / "db")
    store.set_setting("audit_backoff_reason", "resource_unavailable")
    worker = AuditWorker(store, object(), object(), AuditConfig(),
                         health_check=lambda: True, resource_check=lambda: True)
    assert worker.process_once() is None
    assert store.get_setting("audit_backoff_reason") == ""


def _diskstats(*devices):
    return "".join(
        f"8 {minor} {name} 1 0 2 3 4 0 5 6 0 {io_ticks} 8 0 0 0 0 0\n"
        for minor, name, io_ticks in devices
    )


def test_host_resource_guard_first_sample_defers_and_excludes_partitions():
    files = {
        "/proc/loadavg": "0.25 0.10 0.05 1/100 1\n",
        "/proc/diskstats": _diskstats((0, "sda", 100), (1, "sda1", 100)),
    }
    times = iter((10.0, 11.0))
    guard = HostResourceGuard(
        max_load_per_cpu=1.0, max_disk_io_ms=50, cpu_count=lambda: 2,
        read_text=files.__getitem__, monotonic=lambda: next(times),
        whole_device=lambda name: name == "sda",
    )

    assert not guard()
    files["/proc/diskstats"] = _diskstats((0, "sda", 120), (1, "sda1", 200))
    assert guard()


def test_host_resource_guard_normalizes_io_ticks_by_elapsed_time():
    files = {
        "/proc/loadavg": "0.25 0.10 0.05 1/100 1\n",
        "/proc/diskstats": _diskstats((0, "nvme0n1", 100)),
    }
    times = iter((1.0, 1.5, 3.5))
    guard = HostResourceGuard(
        max_load_per_cpu=1.0, max_disk_io_ms=50, read_text=files.__getitem__,
        monotonic=lambda: next(times), whole_device=lambda _: True,
    )

    assert not guard()
    files["/proc/diskstats"] = _diskstats((0, "nvme0n1", 140))
    assert not guard()  # 40 ms / 0.5 s = 80 ms/s
    files["/proc/diskstats"] = _diskstats((0, "nvme0n1", 180))
    assert guard()  # 40 ms / 2 s = 20 ms/s


def test_host_resource_guard_fails_closed_on_proc_read_error_without_losing_sample():
    files = {
        "/proc/loadavg": "0.25 0.10 0.05 1/100 1\n",
        "/proc/diskstats": _diskstats((0, "sda", 100)),
    }
    times = iter((1.0, 2.0))
    guard = HostResourceGuard(
        max_disk_io_ms=50,
        read_text=lambda path: files[path] if path in files else (_ for _ in ()).throw(OSError()),
        monotonic=lambda: next(times), whole_device=lambda _: True,
    )
    assert not guard()
    files.pop("/proc/loadavg")
    assert not guard()
    files["/proc/loadavg"] = "0.25 0.10 0.05 1/100 1\n"
    files["/proc/diskstats"] = _diskstats((0, "sda", 120))
    assert guard()


def test_host_resource_guard_defers_after_device_set_changes_then_uses_new_baseline():
    files = {
        "/proc/loadavg": "0.25 0.10 0.05 1/100 1\n",
        "/proc/diskstats": _diskstats((0, "sda", 100)),
    }
    times = iter((1.0, 2.0, 3.0, 4.0))
    guard = HostResourceGuard(
        max_load_per_cpu=1.0, max_disk_io_ms=50,
        read_text=files.__getitem__, monotonic=lambda: next(times),
        whole_device=lambda _: True,
    )

    assert not guard()
    files["/proc/diskstats"] = _diskstats((0, "sda", 110), (1, "sdb", 10))
    assert not guard()
    files["/proc/diskstats"] = _diskstats((0, "sda", 120))
    assert not guard()
    files["/proc/diskstats"] = _diskstats((0, "sda", 130))
    assert guard()


def test_host_resource_guard_defers_after_any_io_counter_reset_then_uses_new_baseline():
    files = {
        "/proc/loadavg": "0.25 0.10 0.05 1/100 1\n",
        "/proc/diskstats": _diskstats((0, "sda", 100), (1, "sdb", 200)),
    }
    times = iter((1.0, 2.0, 3.0))
    guard = HostResourceGuard(
        max_load_per_cpu=1.0, max_disk_io_ms=50,
        read_text=files.__getitem__, monotonic=lambda: next(times),
        whole_device=lambda _: True,
    )

    assert not guard()
    files["/proc/diskstats"] = _diskstats((0, "sda", 110), (1, "sdb", 190))
    assert not guard()
    files["/proc/diskstats"] = _diskstats((0, "sda", 120), (1, "sdb", 200))
    assert guard()


def test_user_job_preempts_audit_even_when_audit_has_tokens(tmp_path):
    store = Store(tmp_path / "db"); store.upsert_audit_track(1)
    store.enqueue_job(2, "user-retry", mode="auto")
    class Lidarr:
        def get_track(self, _): raise AssertionError("audit must yield")
    worker = AuditWorker(store, Lidarr(), object(), AuditConfig(), clock=lambda: datetime.now(timezone.utc))
    assert worker.process_once() is None
    assert store.get_audit_track(1)["check_count"] == 0
