from smart_lidatube.store import Store
from smart_lidatube.telegram import TelegramBot
from smart_lidatube.api import create_api
from smart_lidatube.retry import filter_candidates, staging_path, PlaylistPoller


def test_track_scoped_candidate_filter_and_staging(tmp_path):
    s=Store(tmp_path/"x.db"); s.reject(1,"youtube","bad")
    candidates=[{"provider":"youtube","source_id":"bad"},{"provider":"youtube","source_id":"ok"}]
    assert filter_candidates(s,1,candidates)==[candidates[1]]
    assert str(staging_path(tmp_path, 1, "song.m4a")).startswith(str(tmp_path/".smart-staging"))


def test_telegram_callbacks_use_attempt_id_and_fail_closed(tmp_path):
    s=Store(tmp_path/"x.db"); j=s.enqueue_job(1,"x"); a=s.add_attempt(j,"youtube","abc")
    s.update_attempt(a, verdict="awaiting_review"); s.update_job(j, "awaiting_review")
    sent=[]
    bot=TelegramBot("token", s, allowed_users={5}, allowed_chats={9}, request=lambda method,payload: sent.append((method,payload)) or {})
    bot.send_review(9,a,"candidate")
    callback=sent[0][1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    assert callback == f"attempt:{a}:accept" and "abc" not in callback
    assert bot.handle_callback({"id":"c","from":{"id":6},"message":{"chat":{"id":9}},"data":callback}) is False
    assert bot.handle_callback({"id":"c","from":{"id":5},"message":{"chat":{"id":9}},"data":callback}) is True


def test_playlist_removes_only_after_durable_enqueue(tmp_path):
    class Nav:
        def retry_entries(self): return [{"id":"song","playlist_id":"p","playlist_name":"Retry","playlist_index":0,"path":"A/S.mp3"}]
        def remove_entry(self,p,i): self.removed=(p,i)
    nav=Nav(); s=Store(tmp_path/"x.db")
    jobs=PlaylistPoller(nav,s,lambda entry: 77).poll_once()
    assert s.get_job(jobs[0])["lidarr_track_id"] == 77
    assert nav.removed == ("p",0)


def test_audit_review_api_actions_are_authenticated_and_reversible(tmp_path):
    store = Store(tmp_path / "x.db")
    client = create_api(store, "secret").test_client()
    headers = {"Authorization": "Bearer secret"}
    assert client.post("/api/smart/audit/7/ignore", headers=headers).status_code == 202
    assert store.get_audit_track(7)["do_not_upgrade"] == 1
    assert client.post("/api/smart/audit/7/later", headers=headers).status_code == 202
    assert store.get_audit_track(7)["do_not_upgrade"] == 0


def _awaiting_audit_attempt(store, track_id=1, provider="youtube", source_id="candidate"):
    job = store.enqueue_job(
        track_id, f"audit-{track_id}-{source_id}", mode="manual",
        metadata={"audit_remediation": "recording_mismatch"},
    )
    attempt = store.add_attempt(job, provider, source_id)
    store.update_attempt(attempt, verdict="awaiting_review")
    store.update_job(job, "awaiting_review")
    return job, attempt


def test_audit_attempt_review_api_requires_token_and_accepts_audit_attempt(tmp_path):
    store = Store(tmp_path / "x.db")
    job, attempt = _awaiting_audit_attempt(store)
    client = create_api(store, "secret").test_client()

    assert client.post(
        f"/api/smart/audit/attempts/{attempt}/review", json={"action": "accept"}
    ).status_code == 401
    response = client.post(
        f"/api/smart/audit/attempts/{attempt}/review",
        json={"action": "accept"},
        headers={"Authorization": "Bearer secret"},
    )

    assert response.status_code == 202
    assert response.get_json() == {"attempt_id": attempt, "status": "ready_import"}
    assert store.get_job(job)["status"] == "ready_import"
    assert store.get_attempt(attempt)["verdict"] == "manual_accepted"


def test_audit_attempt_review_api_rejects_a_candidate_only_for_that_track(tmp_path):
    store = Store(tmp_path / "x.db")
    job, attempt = _awaiting_audit_attempt(store, track_id=7, source_id="bad")
    client = create_api(store, "secret").test_client()

    response = client.post(
        f"/api/smart/audit/attempts/{attempt}/review",
        json={"action": "reject"},
        headers={"Authorization": "Bearer secret"},
    )

    assert response.status_code == 202
    assert response.get_json() == {"attempt_id": attempt, "status": "queued"}
    assert store.get_job(job)["status"] == "queued"
    assert store.is_rejected(7, "youtube", "bad")
    assert not store.is_rejected(8, "youtube", "bad")


def test_audit_attempt_review_api_ignores_track(tmp_path):
    store = Store(tmp_path / "x.db")
    job, attempt = _awaiting_audit_attempt(store, track_id=9)
    client = create_api(store, "secret").test_client()

    response = client.post(
        f"/api/smart/audit/attempts/{attempt}/review",
        json={"action": "ignore_track"},
        headers={"Authorization": "Bearer secret"},
    )

    assert response.status_code == 202
    assert response.get_json() == {"attempt_id": attempt, "status": "cancelled"}
    assert store.get_job(job)["status"] == "cancelled"
    assert store.get_audit_track(9)["do_not_upgrade"] == 1


def test_audit_attempt_review_api_defers_and_requeues(tmp_path):
    store = Store(tmp_path / "x.db")
    job, attempt = _awaiting_audit_attempt(store, track_id=10)
    client = create_api(store, "secret").test_client()

    response = client.post(
        f"/api/smart/audit/attempts/{attempt}/review",
        json={"action": "audit_later"},
        headers={"Authorization": "Bearer secret"},
    )

    assert response.status_code == 202
    assert response.get_json() == {"attempt_id": attempt, "status": "queued"}
    assert store.get_job(job)["status"] == "queued"
    assert store.get_attempt(attempt)["verdict"] == "deferred"


def test_audit_attempt_review_api_rejects_unknown_non_audit_and_stale_actions(tmp_path):
    store = Store(tmp_path / "x.db")
    client = create_api(store, "secret").test_client()
    headers = {"Authorization": "Bearer secret"}

    unknown = client.post(
        "/api/smart/audit/attempts/999/review", json={"action": "accept"}, headers=headers
    )
    assert unknown.status_code == 409
    assert unknown.get_json() == {"error": "audit review is unavailable"}

    job = store.enqueue_job(11, "ordinary")
    ordinary = store.add_attempt(job, "youtube", "candidate")
    store.update_attempt(ordinary, verdict="awaiting_review")
    store.update_job(job, "awaiting_review")
    non_audit = client.post(
        f"/api/smart/audit/attempts/{ordinary}/review",
        json={"action": "accept"}, headers=headers,
    )
    assert non_audit.status_code == 409
    assert non_audit.get_json() == {"error": "audit review is unavailable"}

    _, audit_attempt = _awaiting_audit_attempt(store, track_id=12)
    accepted = client.post(
        f"/api/smart/audit/attempts/{audit_attempt}/review",
        json={"action": "accept"}, headers=headers,
    )
    stale = client.post(
        f"/api/smart/audit/attempts/{audit_attempt}/review",
        json={"action": "reject"}, headers=headers,
    )
    assert accepted.status_code == 202
    assert stale.status_code == 409
    assert stale.get_json() == {"error": "audit review is unavailable"}


def test_token_authenticated_retry_api(tmp_path):
    s=Store(tmp_path/"x.db"); app=create_api(s,"secret"); client=app.test_client()
    assert client.post("/api/smart/retry/22").status_code == 401
    response=client.post("/api/smart/retry/22",headers={"Authorization":"Bearer secret"})
    assert response.status_code == 202
    job=response.get_json()["job_id"]
    assert client.get(f"/api/smart/jobs/{job}",headers={"Authorization":"Bearer secret"}).status_code == 200


def test_retry_api_validates_mode_and_supports_optional_idempotency(tmp_path):
    s = Store(tmp_path / "x.db")
    client = create_api(s, "secret").test_client()
    headers = {"Authorization": "Bearer secret"}
    assert client.post(
        "/api/smart/retry/22", json={"mode": "bad"}, headers=headers
    ).status_code == 400
    first = client.post(
        "/api/smart/retry/22", headers={**headers, "Idempotency-Key": "request-1"}
    ).get_json()["job_id"]
    second = client.post(
        "/api/smart/retry/22", headers={**headers, "Idempotency-Key": "request-1"}
    ).get_json()["job_id"]
    fresh = client.post("/api/smart/retry/22", headers=headers).get_json()["job_id"]
    assert first == second
    assert fresh != first


def test_audit_requeue_api_requires_auth_and_only_makes_safe_statuses_due(tmp_path):
    store = Store(tmp_path / "audit.db")
    for track_id, status, checked_at in (
        (1, "unavailable", "2025-01-03 00:00:00"),
        (2, "unverifiable", "2025-01-01 00:00:00"),
        (3, "verified", "2025-01-02 00:00:00"),
        (4, "unavailable", "2025-01-04 00:00:00"),
    ):
        store.upsert_audit_track(track_id)
        store.record_audit_result(track_id, status, {"reason": "target_file_missing"}, "2099-01-01 00:00:00")
        store.set_audit_last_checked(track_id, checked_at)
    store.set_audit_exemption(4, do_not_audit=True)
    job = store.enqueue_job(99, "existing-retry")
    before = store.get_audit_track(1).copy()
    client = create_api(store, "secret").test_client()

    assert client.post("/api/smart/audit/requeue").status_code == 401
    response = client.post(
        "/api/smart/audit/requeue", json={"limit": 1},
        headers={"Authorization": "Bearer secret"},
    )

    assert response.status_code == 202
    assert response.get_json() == {"requeued": 1}
    assert store.get_audit_track(2)["next_check_at"] is None  # oldest matching track wins
    assert store.get_audit_track(1)["next_check_at"] == "2099-01-01 00:00:00"
    assert store.get_audit_track(3)["next_check_at"] == "2099-01-01 00:00:00"
    assert store.get_audit_track(4)["next_check_at"] == "2099-01-01 00:00:00"
    assert store.get_audit_track(1) == before
    assert store.get_job(job)["status"] == "queued"


def test_audit_requeue_api_accepts_only_safe_statuses(tmp_path):
    store = Store(tmp_path / "audit.db")
    store.upsert_audit_track(1)
    store.record_audit_result(1, "unavailable", {}, "2099-01-01 00:00:00")
    client = create_api(store, "secret").test_client()
    headers = {"Authorization": "Bearer secret"}

    invalid = client.post("/api/smart/audit/requeue", json={"statuses": ["verified"]}, headers=headers)
    too_many = client.post("/api/smart/audit/requeue", json={"limit": 201}, headers=headers)

    assert invalid.status_code == 400
    assert too_many.status_code == 400
    assert store.get_audit_track(1)["next_check_at"] == "2099-01-01 00:00:00"


def test_audit_requeue_api_can_target_one_safe_status_without_mutating_audit_data(tmp_path):
    store = Store(tmp_path / "audit.db")
    for track_id, status in ((1, "unavailable"), (2, "unverifiable")):
        store.upsert_audit_track(track_id)
        store.record_audit_result(track_id, status, {"reason": "target_file_missing"}, "2099-01-01 00:00:00")
    untouched = store.get_audit_track(1).copy()
    client = create_api(store, "secret").test_client()

    response = client.post(
        "/api/smart/audit/requeue", json={"statuses": ["unverifiable"], "limit": 2},
        headers={"Authorization": "Bearer secret"},
    )

    assert response.get_json() == {"requeued": 1}
    assert store.get_audit_track(2)["next_check_at"] is None
    assert store.get_audit_track(1) == untouched


def test_retry_import_api_only_recovers_prepared_import_without_submission(tmp_path):
    store = Store(tmp_path / "x.db")
    client = create_api(store, "secret").test_client()
    headers = {"Authorization": "Bearer secret"}
    job = store.enqueue_job(7, "prepared")
    attempt = store.add_attempt(job, "youtube", "candidate")
    store.update_attempt(attempt, verdict="manual_accepted", staged_path="/stage/a.m4a")
    store.prepare_import(job, 10, "/visible/a.m4a")
    store.update_job(job, "import_attention", "manual import failed before submission")

    response = client.post(f"/api/smart/jobs/{job}/retry-import", headers=headers)
    assert response.status_code == 202
    saved = store.get_job(job)
    assert saved["status"] == "ready_import"
    assert saved["import_phase"] is None and saved["import_result"] is None
    assert saved["submitted_path"] is None and saved["prior_track_file_id"] is None
    assert store.get_attempt(attempt)["staged_path"] == "/stage/a.m4a"

    submitted = store.enqueue_job(8, "submitted")
    store.prepare_import(submitted, 10, "/visible/b.m4a")
    store.mark_import_submitted(submitted, {"status": "queued"})
    store.update_job(submitted, "import_attention", "outcome unknown")
    assert client.post(f"/api/smart/jobs/{submitted}/retry-import", headers=headers).status_code == 409

    unknown = store.enqueue_job(9, "unknown")
    store.prepare_import(unknown, 10, "/visible/c.m4a")
    store.record_import_result(unknown, {"outcome": "unknown"})
    store.update_job(unknown, "import_attention", "outcome unknown")
    assert client.post(f"/api/smart/jobs/{unknown}/retry-import", headers=headers).status_code == 409
    assert client.post(f"/api/smart/jobs/{job}/retry-import").status_code == 401
