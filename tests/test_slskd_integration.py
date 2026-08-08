from smart_lidatube.api import create_api
from smart_lidatube.slskd import ReviewGatedDiscoverySources, SoularrLidarrConflictGuard
from smart_lidatube.store import Store
from smart_lidatube.worker import JobWorker
from smart_lidatube.runner import build_discovery_source


AUTH = {"Authorization": "Bearer secret"}


class ClearState:
    def conflict_state(self, track_id, album_id=None):
        return {"soularr": "idle", "lidarr": "idle"}


class YouTube:
    def search(self, artist, title):
        return [{"provider": "youtube", "source_id": "yt"}]

    def download(self, candidate, directory):
        raise AssertionError("not used by routing tests")


class Slskd:
    def __init__(self):
        self.calls = []

    def search(self, artist, title, album=None):
        self.calls.append((artist, title, album))
        return [{"provider": "slskd", "source_id": "slskd:opaque", "artist": artist,
                 "title": title, "album": album or "", "codec": "flac", "bitrate": 900000,
                 "sample_rate": 48000, "bit_depth": 24, "size_band": "medium", "duration": 200.0}]

    def health(self):
        return {"state": "available", "error": None}


def test_router_adds_slskd_only_to_explicit_manual_retry_not_auto_or_auditor():
    slskd = Slskd()
    router = ReviewGatedDiscoverySources(YouTube(), slskd, SoularrLidarrConflictGuard(ClearState()))
    identity = {"artist": "Artist", "title": "Song", "album": "Album", "album_id": 3}

    manual = {"lidarr_track_id": 7, "mode": "manual", "metadata": {"playlist_name": "Manual Retry"}}
    assert [item["provider"] for item in router.search_for_job(manual, identity)] == ["slskd", "youtube"]
    auto = {"lidarr_track_id": 7, "mode": "auto", "metadata": {"playlist_name": "Retry"}}
    audit = {"lidarr_track_id": 7, "mode": "manual", "metadata": {
        "playlist_name": "Manual Retry", "audit_remediation": {"reason": "mismatch"}}}
    api_manual = {"lidarr_track_id": 7, "mode": "manual", "metadata": {}}
    assert [item["provider"] for item in router.search_for_job(auto, identity)] == ["youtube"]
    assert [item["provider"] for item in router.search_for_job(audit, identity)] == ["youtube"]
    assert [item["provider"] for item in router.search_for_job(api_manual, identity)] == ["youtube"]
    assert slskd.calls == [("Artist", "Song", "Album")]


def test_router_fails_closed_by_skipping_slskd_when_conflict_state_unavailable():
    class Broken:
        def conflict_state(self, track_id, album_id=None):
            raise RuntimeError("private")

    slskd = Slskd()
    router = ReviewGatedDiscoverySources(YouTube(), slskd, SoularrLidarrConflictGuard(Broken()))
    job = {"lidarr_track_id": 7, "mode": "manual", "metadata": {"playlist_name": "Manual Retry"}}
    assert [item["provider"] for item in router.search_for_job(job, {"artist": "A", "title": "T"})] == ["youtube"]
    assert slskd.calls == []
    assert router.health()["slskd"] == {"state": "blocked", "error": "conflict_state_unavailable"}


def test_router_falls_back_to_already_fetched_youtube_when_slskd_search_fails():
    class BrokenSlskd(Slskd):
        def search(self, *args, **kwargs):
            from smart_lidatube.slskd import SlskdDiscoveryError
            raise SlskdDiscoveryError("slskd search unavailable")
    router = ReviewGatedDiscoverySources(YouTube(), BrokenSlskd(), SoularrLidarrConflictGuard(ClearState()))
    job = {"lidarr_track_id": 7, "mode": "manual", "metadata": {"playlist_name": "Manual Retry"}}
    assert router.search_for_job(job, {"artist": "A", "title": "T"}) == [
        {"provider": "youtube", "source_id": "yt"}]
    assert router.health()["slskd"] == {"state": "degraded", "error": "slskd_search_failed"}


def test_worker_persists_slskd_metadata_for_review_without_download_or_verification(tmp_path):
    store = Store(tmp_path / "db")
    job_id = store.enqueue_job(7, "manual", mode="manual", metadata={"playlist_name": "Manual Retry"})

    class Lidarr:
        def get_track(self, track_id):
            return {"id": track_id, "albumId": 3}

        def track_identity(self, track):
            return {"artist": "Artist", "title": "Song", "album": "Album", "album_id": 3}

    class Sources:
        def search_for_job(self, job, identity):
            return [{"provider": "slskd", "source_id": "slskd:opaque", "artist": "Artist",
                     "title": "Song", "album": "Album", "codec": "flac", "bitrate": 900000,
                     "sample_rate": 48000, "bit_depth": 24, "size_band": "medium", "duration": 200.0}]

        def download(self, *args):
            raise AssertionError("slskd discovery must never download")

    class Verifier:
        def verify_file(self, *args):
            raise AssertionError("metadata-only result has no file to verify")

    assert JobWorker(store, Lidarr(), Sources(), Verifier(), tmp_path).process_once() == job_id
    attempt = store.list_attempts(job_id)[0]
    assert attempt["verdict"] == "awaiting_review"
    assert attempt["staged_path"] is None
    assert attempt["provenance"] == {
        "provider": "slskd", "source_id": "slskd:opaque", "artist": "Artist", "title": "Song",
        "album": "Album", "codec": "flac", "bitrate": 900000, "sample_rate": 48000,
        "bit_depth": 24, "size_band": "medium", "duration": 200.0,
    }
    assert store.get_job(job_id)["status"] == "awaiting_review"
    review = store.list_safe_reviews("", 10)[0][0]
    assert review["candidate"] == {
        "provider": "slskd", "source_id": "slskd:opaque", "artist": "Artist",
        "title": "Song", "album": "Album", "codec": "flac", "bitrate": 900000,
        "sample_rate": 48000, "bit_depth": 24, "size_band": "medium", "duration": 200.0,
    }


def test_slskd_review_cannot_be_accepted_for_acquisition_but_can_be_rejected(tmp_path):
    store = Store(tmp_path / "db")
    job = store.enqueue_job(7, "manual", mode="manual")
    attempt = store.add_attempt(job, "slskd", "slskd:opaque", {"provider": "slskd"})
    store.update_attempt(attempt, verdict="awaiting_review")
    store.update_job(job, "awaiting_review")
    client = create_api(store, "secret").test_client()

    accepted = client.post(f"/api/smart/reviews/{attempt}/action", json={"action": "accept"}, headers=AUTH)
    assert accepted.status_code == 409
    assert accepted.get_json() == {"error": "slskd acquisition is not enabled"}
    assert store.get_job(job)["status"] == "awaiting_review"
    assert client.post(f"/api/smart/reviews/{attempt}/action", json={"action": "reject"}, headers=AUTH).status_code == 202


def test_authenticated_status_aggregates_safe_source_health(tmp_path):
    class Health:
        def health(self):
            return {"youtube": {"state": "available", "error": None},
                    "slskd": {"state": "unavailable", "error": "slskd_unavailable"}}

    client = create_api(Store(tmp_path / "db"), "secret", source_health=Health()).test_client()
    assert client.get("/api/smart/status").status_code == 401
    body = client.get("/api/smart/status", headers=AUTH).get_json()
    assert body == {"status": "degraded", "sources": {
        "youtube": {"state": "available", "error": None},
        "slskd": {"state": "unavailable", "error": "slskd_unavailable"}}}
    assert "http" not in str(body) and "secret" not in str(body)


def test_runtime_source_builder_requires_manual_retry_ack_and_uses_production_guard(tmp_path):
    youtube = object()
    values = {
        "SLSKD_URL": "http://192.168.50.166:5030", "SLSKD_API_KEY": "runtime-key",
        "SMART_SOULARR_COEXISTENCE_MODE": "manual-retry-only",
        "SMART_DB_PATH": str(tmp_path / "db"), "SLSKD_RESULT_CAP": "7",
        "LIDARR_ADDRESS": "http://lidarr:8686", "LIDARR_API_KEY": "lidarr-key",
    }
    source = build_discovery_source(youtube, values.get)
    assert isinstance(source, ReviewGatedDiscoverySources)
    assert source.slskd.base == "http://192.168.50.166:5030"
    assert source.slskd.result_cap == 7

    values.pop("SMART_SOULARR_COEXISTENCE_MODE")
    assert build_discovery_source(youtube, values.get) is youtube


def test_runtime_status_provider_reports_disabled_empty_slskd_configuration():
    from smart_lidatube.runner import build_source_status
    health = build_source_status({}.get).health()
    assert health["slskd"] == {"state": "disabled", "error": "not_configured"}


def test_runtime_status_provider_checks_configured_slskd_safely():
    from smart_lidatube.runner import build_source_status
    values = {"SLSKD_URL": "http://slskd:5030", "SLSKD_API_KEY": "key",
              "SMART_SOULARR_COEXISTENCE_MODE": "manual-retry-only"}
    class Session:
        def get(self, url, **kwargs):
            return type("R", (), {"url": url, "raise_for_status": lambda self: None})()
    status = build_source_status(values.get, session=Session()).health()
    assert status["slskd"] == {"state": "available", "error": None}


def test_production_flask_entrypoint_wires_runtime_source_status():
    from pathlib import Path
    text = (Path(__file__).parents[1] / "src" / "LidaTube.py").read_text()
    assert "from smart_lidatube.runner import build_source_status" in text
    assert "source_health=build_source_status()" in text
