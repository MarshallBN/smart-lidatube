import sqlite3
import time

from smart_lidatube.api import create_api
from smart_lidatube.audit import AuditConfig, AuditWorker
from smart_lidatube.store import Store


AUTH = {"Authorization": "Bearer secret"}


def test_quality_api_exact_percentages_denominators_unknown_and_zero(tmp_path):
    store = Store(tmp_path / "db")
    store.upsert_audit_track(1); store.upsert_audit_track(2); store.upsert_audit_track(3)
    store.upsert_quality(1, "a", {"codec": "mp3", "bitrate": 128000, "sample_rate": 44100,
        "lossless": False, "size_band": "small", "source": "lidarr", "confidence": "reported"})
    store.upsert_quality(2, "b", {"codec": "flac", "bit_depth": 24, "lossless": True,
        "size_band": "large", "source": "probe", "confidence": "measured"})
    client = create_api(store, "secret").test_client()
    assert client.get("/api/smart/quality").status_code == 401
    quality = client.get("/api/smart/quality", headers=AUTH).get_json()
    assert (quality["total"], quality["measured"], quality["unknown"]) == (3, 2, 1)
    assert quality["formats"] == {"denominator": 2, "buckets": [
        {"name": "flac", "count": 1, "percentage": 50.0},
        {"name": "mp3", "count": 1, "percentage": 50.0}]}
    assert quality["bit_depth"]["denominator"] == 1
    assert quality["bitrate"]["denominator"] == 1
    assert quality["lossless"]["denominator"] == 2
    assert quality["bitrate_buckets"] == {"denominator": 2, "buckets": [
        {"name": "lossless", "count": 1, "percentage": 50.0},
        {"name": "<192 kbps", "count": 1, "percentage": 50.0}]}
    assert client.get("/api/smart/dashboard/quality", headers=AUTH).get_json() == quality
    empty = create_api(Store(tmp_path / "empty"), "secret").test_client().get("/api/smart/quality", headers=AUTH).get_json()
    assert empty["total"] == 0 and empty["formats"] == {"denominator": 0, "buckets": []}


def test_dashboard_summary_is_authenticated_safe_and_exposes_flags(tmp_path):
    store = Store(tmp_path / "db")
    store.set_setting("worker_heartbeat", str(time.time() - 3)); store.set_setting("worker_status", "running")
    store.set_setting("app_version", "1.2.3")
    store.enqueue_job(1, "one", mode="manual")
    payload = create_api(store, "secret").test_client().get("/api/smart/dashboard/summary", headers=AUTH).get_json()
    assert payload["worker"]["state"] == "running" and payload["worker"]["heartbeat_age_seconds"] >= 2
    assert payload["version"] == "1.2.3"
    assert payload["jobs"]["manual"]["queued"] == 1
    assert payload["automation"] == {"audit_mode": "observe", "candidate_discovery_budget_per_hour": 0,
        "automatic_auditor_upgrades": False}
    assert "/" not in str(payload)


def test_events_are_allowlisted_safe_and_cursor_limit_bounded(tmp_path):
    store = Store(tmp_path / "db")
    assert store.record_event("worker", "info", "cycle_ok", "worker_cycle", {"count": 3, "path": "/secret", "url": "https://x"}, job_id=9, track_id=7)
    assert not store.record_event("evil", "trace", "anything", "raw", {"exception": "secret"})
    client = create_api(store, "secret").test_client()
    assert client.get("/api/smart/events?limit=0", headers=AUTH).status_code == 400
    assert client.get("/api/smart/events?limit=201", headers=AUTH).status_code == 400
    page = client.get("/api/smart/events?limit=1", headers=AUTH).get_json()
    assert page["items"] == [{"id": 1, "component": "worker", "severity": "info", "code": "cycle_ok",
        "template": "worker_cycle", "metadata": {"count": 3}, "job_id": "job:9", "track_id": "track:7",
        "created_at": page["items"][0]["created_at"]}]
    assert "/secret" not in str(page) and "https://" not in str(page)
    assert client.get(f"/api/smart/events?cursor={page['next_cursor']}&limit=1", headers=AUTH).get_json()["items"] == []
    assert client.get("/api/smart/dashboard/events?limit=1", headers=AUTH).status_code == 200


def _review(store, audit=False):
    metadata = {"audit_remediation": "recording_mismatch"} if audit else None
    job = store.enqueue_job(4, "review" + str(audit), mode="manual", metadata=metadata)
    attempt = store.add_attempt(job, "youtube", "private-source")
    store.update_attempt(attempt, verdict="awaiting_review", evidence={"raw": "/secret"}, staged_path="/staged/private")
    store.update_job(job, "awaiting_review")
    return job, attempt


def test_safe_paginated_jobs_reviews_and_one_shot_candidate_action(tmp_path):
    store = Store(tmp_path / "db"); job, attempt = _review(store)
    client = create_api(store, "secret").test_client()
    for endpoint in ("/api/smart/jobs", "/api/smart/reviews"):
        assert client.get(endpoint).status_code == 401
        assert client.get(endpoint + "?limit=0", headers=AUTH).status_code == 400
        body = client.get(endpoint + "?limit=1", headers=AUTH).get_json()
        assert len(body["items"]) == 1
        assert not any(key in str(body) for key in ("staged_path", "source_id", "evidence", "/staged"))
    response = client.post(f"/api/smart/reviews/{attempt}/action", json={"action": "accept"}, headers=AUTH)
    assert response.status_code == 202 and store.get_job(job)["status"] == "ready_import"
    assert client.post(f"/api/smart/reviews/{attempt}/action", json={"action": "reject"}, headers=AUTH).status_code == 409


def test_individual_job_endpoint_is_safe_too(tmp_path):
    store = Store(tmp_path / "db")
    job = store.enqueue_job(1, "private-key", metadata={"url": "https://secret"})
    store.prepare_import(job, 2, "/private/submitted/path")
    body = create_api(store, "secret").test_client().get(f"/api/smart/jobs/{job}", headers=AUTH).get_json()
    assert body["id"] == f"job:{job}" and body["track_id"] == "track:1"
    assert "private" not in str(body) and "metadata" not in body and "submitted_path" not in body


def test_audit_control_allowlist_persists_and_pause_prevents_checks(tmp_path):
    store = Store(tmp_path / "db"); store.upsert_audit_track(7)
    client = create_api(store, "secret").test_client()
    assert client.post("/api/smart/audit/control", json={"mode": "auto_safe"}, headers=AUTH).status_code == 400
    assert client.post("/api/smart/audit/control", json={"mode": "paused"}, headers=AUTH).status_code == 202
    assert store.get_setting("audit_mode") == "paused"
    class Lidarr:
        def get_track(self, _): raise AssertionError("paused must not start a check")
    assert AuditWorker(store, Lidarr(), object(), AuditConfig()).process_once() is None
    assert client.post("/api/smart/audit/control", json={"mode": "review"}, headers=AUTH).status_code == 202


def test_control_page_has_sections_and_keeps_token_only_in_memory(tmp_path):
    html = create_api(Store(tmp_path / "db"), "secret").test_client().get("/smart-control").get_data(as_text=True)
    for section in ("Dashboard", "Quality", "Reviews", "Jobs", "Events"):
        assert section in html
    assert "localStorage" not in html and "sessionStorage" not in html
    assert "let token" in html and "SMART_API_TOKEN" not in html
