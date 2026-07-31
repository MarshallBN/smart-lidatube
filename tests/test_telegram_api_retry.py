from smart_lidatube.store import Store
from smart_lidatube.telegram import TelegramBot
from smart_lidatube.api import create_api
from smart_lidatube.retry import filter_candidates, staging_path


def test_track_scoped_candidate_filter_and_staging(tmp_path):
    s=Store(tmp_path/"x.db"); s.reject(1,"youtube","bad")
    candidates=[{"provider":"youtube","source_id":"bad"},{"provider":"youtube","source_id":"ok"}]
    assert filter_candidates(s,1,candidates)==[candidates[1]]
    assert str(staging_path(tmp_path, 1, "song.m4a")).startswith(str(tmp_path/".smart-staging"))


def test_telegram_callbacks_use_attempt_id_and_fail_closed(tmp_path):
    s=Store(tmp_path/"x.db"); j=s.enqueue_job(1,"x"); a=s.add_attempt(j,"youtube","abc")
    sent=[]
    bot=TelegramBot("token", s, allowed_users={5}, allowed_chats={9}, request=lambda method,payload: sent.append((method,payload)) or {})
    bot.send_review(9,a,"candidate")
    callback=sent[0][1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    assert callback == f"attempt:{a}:accept" and "abc" not in callback
    assert bot.handle_callback({"id":"c","from":{"id":6},"message":{"chat":{"id":9}},"data":callback}) is False
    assert bot.handle_callback({"id":"c","from":{"id":5},"message":{"chat":{"id":9}},"data":callback}) is True


def test_token_authenticated_retry_api(tmp_path):
    s=Store(tmp_path/"x.db"); app=create_api(s,"secret"); client=app.test_client()
    assert client.post("/api/smart/retry/22").status_code == 401
    response=client.post("/api/smart/retry/22",headers={"Authorization":"Bearer secret"})
    assert response.status_code == 202
    job=response.get_json()["job_id"]
    assert client.get(f"/api/smart/jobs/{job}",headers={"Authorization":"Bearer secret"}).status_code == 200
