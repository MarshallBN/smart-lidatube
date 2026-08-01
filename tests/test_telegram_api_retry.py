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
