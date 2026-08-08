from datetime import datetime, timezone

import pytest

from smart_lidatube.audit import AuditConfig, AuditWorker, audit_rate_tier
from smart_lidatube.store import Store


@pytest.mark.parametrize("backlog,rate", [
    (10001, 300), (10000, 200), (5001, 200), (5000, 120),
    (1001, 120), (1000, 60), (101, 60), (100, 24), (1, 24), (0, 12),
])
def test_adaptive_audit_tier_boundaries(backlog, rate):
    assert audit_rate_tier(backlog) == rate


def test_audit_summary_persists_tier_effective_rate_backlog_and_eta(tmp_path):
    store = Store(tmp_path / "db")
    for track_id in range(1, 102):
        store.upsert_audit_track(track_id)
    config = AuditConfig(budget_per_hour=40)
    worker = AuditWorker(store, object(), object(), config)

    assert worker.refresh_throughput() == 40
    status = store.audit_status()
    assert status["backlog"] == 101
    assert status["tier_rate_per_hour"] == 60
    assert status["effective_rate_per_hour"] == 40
    assert status["eta_hours"] == pytest.approx(2.53)
    assert store.get_setting("audit_effective_rate_per_hour") == "40"

    reopened = Store(tmp_path / "db")
    assert reopened.audit_status()["effective_rate_per_hour"] == 40


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


def test_user_job_preempts_audit_even_when_audit_has_tokens(tmp_path):
    store = Store(tmp_path / "db"); store.upsert_audit_track(1)
    store.enqueue_job(2, "user-retry", mode="auto")
    class Lidarr:
        def get_track(self, _): raise AssertionError("audit must yield")
    worker = AuditWorker(store, Lidarr(), object(), AuditConfig(), clock=lambda: datetime.now(timezone.utc))
    assert worker.process_once() is None
    assert store.get_audit_track(1)["check_count"] == 0
