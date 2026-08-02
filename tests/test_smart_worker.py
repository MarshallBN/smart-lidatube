import json
import subprocess

from smart_lidatube.clients import LidarrClient, YouTubeClient
from smart_lidatube.fingerprint import Fpcalc
from smart_lidatube.retry import PlaylistPoller
from smart_lidatube.store import Store
from smart_lidatube.telegram import TelegramBot
from smart_lidatube.worker import JobWorker
from smart_lidatube.api import create_api


class Response:
    def __init__(self, data=None, status=200, headers=None):
        self._data = data if data is not None else {}
        self.status_code = status
        self.headers = headers or {}

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


def test_atomic_claim_and_state_transitions(tmp_path):
    store = Store(tmp_path / "smart.db")
    first = store.enqueue_job(1, "one")
    second = store.enqueue_job(2, "two")
    assert store.claim_job()["id"] == first
    assert store.get_job(first)["status"] == "processing"
    assert store.claim_job()["id"] == second
    assert store.claim_job() is None
    store.update_job(first, "queued", error="retry")
    assert store.claim_job()["id"] == first
    assert store.get_job(first)["last_error"] == "retry"


def test_lidarr_identity_path_resolution_and_release_fallback():
    posts = []

    class Session:
        def get(self, url, **kwargs):
            if url.endswith("/api/v1/trackfile"):
                return Response({"records": [{"id": 9, "path": "/music/A/S.mp3"}]})
            if url.endswith("/api/v1/track"):
                return Response([{"id": 7, "trackFileId": 9}])
            if url.endswith("/api/v1/track/7"):
                return Response({
                    "id": 7,
                    "title": "Song",
                    "artistId": 2,
                    "albumId": 3,
                    "foreignTrackId": "mb-track",
                    "duration": 201000,
                    "album": {"releases": [{"id": 44, "monitored": True}]},
                })
            raise AssertionError(url)

        def post(self, url, **kwargs):
            posts.append((url, kwargs))
            return Response({"id": 101, "status": "queued"}, status=202)

    client = LidarrClient("http://lidarr", "key", session=Session())
    assert client.resolve_track_by_path("A/S.mp3") == 7
    identity = client.track_identity(client.get_track(7))
    assert identity["recording_id"] == "mb-track"
    assert identity["duration"] == 201
    client.manual_import("/downloads/.smart-staging/1/a.m4a", client.get_track(7))
    assert posts[0][0].endswith("/api/v1/command")
    assert posts[0][1]["json"]["files"][0]["albumReleaseId"] == 44
    assert posts[0][1]["json"]["replaceExistingFiles"] is True


def test_youtube_search_and_download_are_injectable(tmp_path):
    class Music:
        def search(self, query, filter, limit):
            assert query == "Artist - Song"
            return [{"videoId": "abc", "title": "Song", "artists": [{"name": "Artist"}], "duration_seconds": 200}]

    seen = {}

    class YDL:
        def __init__(self, options):
            seen["options"] = options

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def extract_info(self, url, download):
            seen["url"] = url
            target = seen["options"]["outtmpl"].replace("%(ext)s", "m4a")
            open(target, "wb").write(b"audio")
            return {"id": "abc"}

    client = YouTubeClient(ytmusic=Music(), ydl_factory=YDL)
    candidate = client.search("Artist", "Song", limit=3)[0]
    path = client.download(candidate, tmp_path)
    assert candidate["source_id"] == "abc"
    assert path.read_bytes() == b"audio"
    assert seen["url"].endswith("abc")


def test_worker_rejects_mismatch_then_imports_match(tmp_path):
    store = Store(tmp_path / "smart.db")
    job_id = store.enqueue_job(7, "worker")

    class Lidarr:
        def get_track(self, track_id):
            return {"id": track_id, "title": "Song", "artist": {"artistName": "Artist"}}

        def track_identity(self, track):
            return {"recording_id": "expected", "duration": 200, "artist": "Artist", "title": "Song"}

        def manual_import(self, path, track):
            self.imported = path
            return {"id": 10}

    class Sources:
        def search(self, artist, title):
            return [
                {"provider": "youtube", "source_id": "bad", "url": "bad"},
                {"provider": "youtube", "source_id": "good", "url": "good"},
            ]

        def download(self, candidate, directory):
            path = directory / (candidate["source_id"] + ".m4a")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"audio")
            return path

    class Verifier:
        def verify_file(self, path, identity):
            verdict = "rejected" if path.name.startswith("bad") else "accepted"
            return type("V", (), {"verdict": verdict, "reason": verdict, "evidence": {"file": path.name}})()

    lidarr = Lidarr()
    worker = JobWorker(store, lidarr, Sources(), Verifier(), tmp_path)
    assert worker.process_once() == job_id
    assert store.is_rejected(7, "youtube", "bad")
    assert store.get_job(job_id)["status"] == "importing"
    assert ".smart-staging" in str(lidarr.imported)
    assert len(store.list_attempts(job_id)) == 2


def test_manual_review_callback_resumes_or_imports(tmp_path):
    store = Store(tmp_path / "smart.db")
    job = store.enqueue_job(1, "review", mode="manual")
    attempt = store.add_attempt(job, "youtube", "abc")
    store.update_attempt(attempt, verdict="awaiting_review", staged_path="/staged/a.m4a")
    store.update_job(job, "awaiting_review")
    bot = TelegramBot("x", store, {5}, {9}, request=lambda *args: {})

    reject = {"id": "r", "from": {"id": 5}, "message": {"chat": {"id": 9}}, "data": f"attempt:{attempt}:reject"}
    assert bot.handle_callback(reject)
    assert store.get_job(job)["status"] == "queued"

    other = store.add_attempt(job, "youtube", "def")
    store.update_attempt(other, verdict="awaiting_review", staged_path="/staged/b.m4a")
    store.update_job(job, "awaiting_review")
    accept = {**reject, "id": "a", "data": f"attempt:{other}:accept"}
    assert bot.handle_callback(accept)
    assert store.get_job(job)["status"] == "ready_import"


def test_audit_origin_rejects_verified_lossless_current_file_before_review(tmp_path):
    store = Store(tmp_path / "smart.db")
    job = store.enqueue_job(1, "audit-lossless", mode="manual", metadata={"audit_remediation": "recording_mismatch"})
    sent = []

    class Lidarr:
        def get_track(self, track_id): return {"id": track_id, "trackFileId": 4, "title": "Song", "artist": {"artistName": "Artist"}}
        def get_track_file(self, file_id): return {"id": file_id, "mediaInfo": {"audioFormat": "FLAC"}}
        def track_identity(self, track): return {"recording_id": "expected", "artist": "Artist", "title": "Song", "track_file_id": 4}
    class Sources:
        def search(self, *args): return [{"provider": "youtube", "source_id": "x"}]
        def download(self, candidate, directory):
            directory.mkdir(parents=True, exist_ok=True); path = directory / "x"; path.write_bytes(b"audio"); return path
    class Verifier:
        def verify_file(self, path, identity): return type("V", (), {"verdict": "accepted", "reason": "recording_match", "evidence": {"edition_match": True, "codec": "m4a"}})()

    store.upsert_audit_track(1); store.record_audit_result(1, "verified")
    worker = JobWorker(store, Lidarr(), Sources(), Verifier(), tmp_path, telegram=type("T", (), {"send_review": lambda *args: sent.append(args) or True})(), review_chat_id=9)
    assert worker.process_once() == job
    assert sent == []
    assert store.list_attempts(job)[0]["verdict"] == "rejected"


def test_audit_origin_rejects_known_edition_mismatch_and_never_auto_import(tmp_path):
    store = Store(tmp_path / "smart.db")
    job = store.enqueue_job(1, "audit-edition", metadata={"audit_remediation": "recording_mismatch"})

    class Lidarr:
        def get_track(self, track_id): return {"id": track_id, "trackFileId": 4, "title": "Song", "artist": {"artistName": "Artist"}}
        def get_track_file(self, file_id): return {"id": file_id, "mediaInfo": {"audioFormat": "MP3"}}
        def track_identity(self, track): return {"recording_id": "expected", "artist": "Artist", "title": "Song", "track_file_id": 4}
        def manual_import(self, *args): raise AssertionError("audit work must never auto import")
    class Sources:
        def search(self, *args): return [{"provider": "youtube", "source_id": "x"}]
        def download(self, candidate, directory):
            directory.mkdir(parents=True, exist_ok=True); path = directory / "x"; path.write_bytes(b"audio"); return path
    class Verifier:
        def verify_file(self, path, identity): return type("V", (), {"verdict": "accepted", "reason": "recording_match", "evidence": {"edition_match": False, "codec": "m4a"}})()

    worker = JobWorker(store, Lidarr(), Sources(), Verifier(), tmp_path)
    assert worker.process_once() == job
    assert store.list_attempts(job)[0]["verdict"] == "rejected"


def test_audit_origin_sends_verified_recording_match_without_edition_evidence_to_review(tmp_path):
    store = Store(tmp_path / "smart.db")
    job = store.enqueue_job(1, "audit-recording-match", metadata={"audit_remediation": "recording_mismatch"})
    sent = []

    class Lidarr:
        def get_track(self, track_id): return {"id": track_id, "trackFileId": 4, "title": "Song", "artist": {"artistName": "Artist"}}
        def get_track_file(self, file_id): return {"id": file_id, "mediaInfo": {"audioFormat": "MP3"}}
        def track_identity(self, track): return {"recording_id": "expected", "artist": "Artist", "title": "Song", "track_file_id": 4}
        def manual_import(self, *args): raise AssertionError("audit work must never auto import")
    class Sources:
        def search(self, *args): return [{"provider": "youtube", "source_id": "x"}]
        def download(self, candidate, directory):
            directory.mkdir(parents=True, exist_ok=True); path = directory / "x"; path.write_bytes(b"audio"); return path
    class Verifier:
        def verify_file(self, path, identity): return type("V", (), {"verdict": "accepted", "reason": "recording_match", "evidence": {"actual_duration": 200}})()

    worker = JobWorker(store, Lidarr(), Sources(), Verifier(), tmp_path, telegram=type("T", (), {"send_review": lambda *args: sent.append(args) or True})(), review_chat_id=9)
    assert worker.process_once() == job
    assert len(sent) == 1
    assert store.list_attempts(job)[0]["verdict"] == "awaiting_review"


def test_audit_origin_without_telegram_is_reviewable_through_api_after_verified_staging(tmp_path):
    store = Store(tmp_path / "smart.db")
    job = store.enqueue_job(1, "audit-api-review", metadata={"audit_remediation": "recording_mismatch"})

    class Lidarr:
        def get_track(self, track_id): return {"id": track_id, "trackFileId": 4, "title": "Song", "artist": {"artistName": "Artist"}}
        def get_track_file(self, file_id): return {"id": file_id, "mediaInfo": {"audioFormat": "MP3"}}
        def track_identity(self, track): return {"recording_id": "expected", "artist": "Artist", "title": "Song", "track_file_id": 4}
        def manual_import(self, *args): raise AssertionError("review is handled by the API")
    class Sources:
        def search(self, *args): return [{"provider": "youtube", "source_id": "x"}]
        def download(self, candidate, directory):
            directory.mkdir(parents=True, exist_ok=True); path = directory / "x"; path.write_bytes(b"audio"); return path
    class Verifier:
        def verify_file(self, path, identity): return type("V", (), {"verdict": "accepted", "reason": "recording_match", "evidence": {"actual_duration": 200}})()

    assert JobWorker(store, Lidarr(), Sources(), Verifier(), tmp_path).process_once() == job
    attempt = store.list_attempts(job)[0]
    assert attempt["verdict"] == "awaiting_review"
    assert attempt["artifact_manifest"]
    assert store.get_job(job)["status"] == "awaiting_review"

    response = create_api(store, "secret").test_client().post(
        f"/api/smart/audit/attempts/{attempt['id']}/review",
        json={"action": "accept"}, headers={"Authorization": "Bearer secret"},
    )

    assert response.status_code == 202
    assert store.get_attempt(attempt["id"])["verdict"] == "manual_accepted"
    assert store.get_job(job)["status"] == "ready_import"


def test_non_audit_job_without_telegram_remains_review_unavailable(tmp_path):
    store = Store(tmp_path / "smart.db")
    job = store.enqueue_job(1, "ordinary-review", mode="manual")

    class Lidarr:
        def get_track(self, track_id): return {"id": track_id, "title": "Song", "artist": {"artistName": "Artist"}}
        def track_identity(self, track): return {"artist": "Artist", "title": "Song"}
    class Sources:
        def search(self, *args): return [{"provider": "youtube", "source_id": "x"}]
        def download(self, candidate, directory):
            directory.mkdir(parents=True, exist_ok=True); path = directory / "x"; path.write_bytes(b"audio"); return path
    class Verifier:
        def verify_file(self, path, identity): return type("V", (), {"verdict": "accepted", "reason": "match", "evidence": {}})()

    assert JobWorker(store, Lidarr(), Sources(), Verifier(), tmp_path).process_once() == job
    assert store.list_attempts(job)[0]["verdict"] == "review_unavailable"
    assert store.get_job(job)["status"] == "review_unavailable"


def test_audit_review_actions_are_distinct_one_shot_and_safe(tmp_path):
    store = Store(tmp_path / "smart.db")
    job = store.enqueue_job(1, "audit-review", mode="manual", metadata={"audit_remediation": "recording_mismatch"})
    attempt = store.add_attempt(job, "youtube", "abc")
    store.update_attempt(attempt, verdict="awaiting_review", staged_path="/staged/a")
    store.update_job(job, "awaiting_review")
    sent = []
    bot = TelegramBot("x", store, {5}, {9}, request=lambda method, payload: sent.append((method, payload)) or {})
    bot.send_review(9, attempt, "candidate")
    labels = [button["text"] for row in sent[0][1]["reply_markup"]["inline_keyboard"] for button in row]
    assert labels == ["Accept replacement", "Reject candidate", "Ignore track", "Audit later"]
    assert bot.handle_callback({"id": "x", "from": {"id": 5}, "message": {"chat": {"id": 9}}, "data": f"attempt:{attempt}:ignore_track"})
    assert store.get_audit_track(1)["do_not_upgrade"] == 1
    assert store.get_job(job)["status"] == "cancelled"
    assert not bot.handle_callback({"id": "y", "from": {"id": 5}, "message": {"chat": {"id": 9}}, "data": f"attempt:{attempt}:ignore_track"})


def test_audit_import_rejects_staged_artifact_drift(tmp_path):
    store = Store(tmp_path / "smart.db")
    staged = tmp_path / ".smart-staging" / "1" / "candidate"
    staged.parent.mkdir(parents=True); staged.write_bytes(b"verified")
    job = store.enqueue_job(1, "audit-drift", mode="manual", metadata={"audit_remediation": "recording_mismatch"})
    attempt = store.add_attempt(job, "youtube", "abc")
    store.update_attempt(attempt, verdict="manual_accepted", staged_path=staged)
    store.capture_artifact_manifest(attempt, staged)
    store.update_job(job, "ready_import")
    staged.write_bytes(b"drifted")

    class Lidarr:
        def get_track(self, track_id): return {"id": track_id, "title": "Song", "artist": {"artistName": "Artist"}}
        def manual_import(self, *args): raise AssertionError("drifted artifact must not import")

    assert JobWorker(store, Lidarr(), None, None, tmp_path).process_once() == job
    assert store.get_job(job)["status"] == "import_attention"


def test_playlist_removal_descends_and_verifies_occurrence(tmp_path):
    entries = [
        {"id": "same", "playlist_id": "p", "playlist_name": "Retry", "playlist_index": 0, "path": "A/one.mp3"},
        {"id": "same", "playlist_id": "p", "playlist_name": "Retry", "playlist_index": 2, "path": "A/two.mp3"},
    ]

    class Nav:
        def retry_entries(self):
            return entries

        def remove_entry(self, playlist_id, index, expected_id=None):
            self.removed = getattr(self, "removed", []) + [(index, expected_id)]
            return True

    nav = Nav()
    poller = PlaylistPoller(nav, Store(tmp_path / "x.db"), lambda entry: entry["playlist_index"] + 10)
    poller.poll_once()
    assert nav.removed == [(2, "same"), (0, "same")]


def test_fpcalc_exit_three_is_partial_and_empty_is_rejected():
    partial = lambda args, **kwargs: subprocess.CompletedProcess(
        args, 3, json.dumps({"duration": 100, "fingerprint": "partial"}), "short"
    )
    assert Fpcalc(run=partial).calculate("x").partial is True

    empty = lambda args, **kwargs: subprocess.CompletedProcess(
        args, 0, json.dumps({"duration": 100, "fingerprint": ""}), ""
    )
    try:
        Fpcalc(run=empty).calculate("x")
    except Exception as exc:
        assert "fingerprint" in str(exc)
    else:
        raise AssertionError("empty fingerprint accepted")
