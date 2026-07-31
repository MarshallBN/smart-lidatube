import json
import subprocess

from smart_lidatube.clients import LidarrClient, YouTubeClient
from smart_lidatube.fingerprint import Fpcalc
from smart_lidatube.retry import PlaylistPoller
from smart_lidatube.store import Store
from smart_lidatube.telegram import TelegramBot
from smart_lidatube.worker import JobWorker


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
    assert posts[0][1]["json"][0]["albumReleaseId"] == 44
    assert posts[0][1]["json"][0]["replaceExistingFiles"] is True


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
