import pytest

from smart_lidatube.slskd import (
    ConflictStateUnavailable,
    HttpConflictStateProvider,
    SlskdDiscoveryClient,
    SlskdDiscoveryError,
    SourceConflict,
    SoularrLidarrConflictGuard,
)


class Response:
    def __init__(self, data=None, status=200):
        self.data = data
        self.status_code = status

    def json(self):
        return self.data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"request failed: {self.status_code}")


def test_slskd_search_returns_only_bounded_safe_metadata_and_uses_timeouts():
    calls = []

    class Session:
        def post(self, url, **kwargs):
            calls.append(("post", url, kwargs))
            return Response({"id": "search-internal"}, 201)

        def get(self, url, **kwargs):
            calls.append(("get", url, kwargs))
            return Response({
                "state": "Completed",
                "responses": [{
                    "username": "private-peer",
                    "ipAddress": "10.2.3.4",
                    "files": [
                        {"filename": r"private-peer\\Artist\\Album\\01 Song.flac", "size": 40_000_000,
                         "bitRate": 1000, "sampleRate": 96000, "bitDepth": 24, "length": 201},
                        {"filename": r"private-peer\\Artist\\Album\\02 Other.mp3", "size": 4_000_000,
                         "bitRate": 192, "sampleRate": 44100, "length": 180},
                    ],
                }],
            })

    client = SlskdDiscoveryClient(
        "http://192.168.50.166:5030", "runtime-secret", session=Session(),
        timeout=(1.5, 4), result_cap=1, poll_attempts=1, opaque_key="runtime-opaque-key",
    )
    results = client.search("Artist", "Song", album="Album")

    assert results == [{
        "provider": "slskd", "source_id": results[0]["source_id"], "artist": "Artist",
        "title": "Song", "album": "Album", "codec": "flac", "bitrate": 1000000,
        "sample_rate": 96000, "bit_depth": 24, "size_band": "medium", "duration": 201.0,
    }]
    assert results[0]["source_id"].startswith("slskd:")
    assert len(results[0]["source_id"]) == len("slskd:") + 32
    serialized = str(results)
    assert not any(secret in serialized for secret in ("private-peer", "10.2.3.4", "Artist\\\\Album", "runtime-secret"))
    assert calls[0] == ("post", "http://192.168.50.166:5030/api/v0/searches", {
        "json": {"searchText": "Artist - Song Album"},
        "headers": {"X-API-Key": "runtime-secret"}, "timeout": (1.5, 4),
    })
    assert calls[1][2]["timeout"] == (1.5, 4)


def test_slskd_errors_are_sanitized_and_health_is_safe():
    leaked = "http://admin:secret@192.168.50.166:5030/private/path?token=secret"

    class Session:
        def post(self, *args, **kwargs):
            raise RuntimeError(leaked)

        def get(self, *args, **kwargs):
            raise RuntimeError(leaked)

    client = SlskdDiscoveryClient("http://192.168.50.166:5030", "secret", session=Session())
    with pytest.raises(SlskdDiscoveryError, match="slskd search unavailable") as error:
        client.search("A", "T")
    assert leaked not in str(error.value)
    assert client.health() == {"state": "unavailable", "error": "slskd_unavailable"}


def test_slskd_client_has_no_transfer_api():
    client = SlskdDiscoveryClient("http://slskd", "secret")
    assert not hasattr(client, "download")
    assert not hasattr(client, "transfer")
    assert not hasattr(client, "acquire")


def test_conflict_guard_rejects_active_states_and_fails_closed_when_unavailable():
    class State:
        def conflict_state(self, track_id, album_id=None):
            return {"soularr": "idle", "lidarr": "downloading"}

    guard = SoularrLidarrConflictGuard(State())
    with pytest.raises(SourceConflict, match="active_lidarr_work"):
        guard.require_clear(7, album_id=3, operation="search")

    class Broken:
        def conflict_state(self, track_id, album_id=None):
            raise RuntimeError("http://user:secret@lidarr/private")

    with pytest.raises(ConflictStateUnavailable, match="conflict_state_unavailable") as error:
        SoularrLidarrConflictGuard(Broken()).require_clear(7, operation="acquisition")
    assert "secret" not in str(error.value)


def test_conflict_guard_requires_explicit_clear_state_for_both_services():
    for state in ({"soularr": "idle"}, None, {"soularr": "unknown", "lidarr": "idle"}):
        provider = type("State", (), {"conflict_state": lambda self, track_id, album_id=None, value=state: value})()
        with pytest.raises(ConflictStateUnavailable, match="conflict_state_unavailable"):
            SoularrLidarrConflictGuard(provider).require_clear(1, operation="search")

    provider = type("State", (), {"conflict_state": lambda self, track_id, album_id=None: {
        "soularr": "idle", "lidarr": "clear"}})()
    assert SoularrLidarrConflictGuard(provider).require_clear(1, operation="search")


def test_http_conflict_provider_is_bounded_runtime_config_and_sanitizes_failure():
    calls = []

    class Session:
        def get(self, url, **kwargs):
            calls.append((url, kwargs))
            return Response({"soularr": "idle", "lidarr": "clear"})

    provider = HttpConflictStateProvider(
        "http://conflict-state.local/check", "runtime-token", session=Session(), timeout=(1, 2)
    )
    assert provider.conflict_state(7, album_id=3) == {"soularr": "idle", "lidarr": "clear"}
    assert calls == [("http://conflict-state.local/check", {
        "params": {"track_id": 7, "album_id": 3},
        "headers": {"Authorization": "Bearer runtime-token"}, "timeout": (1, 2),
    })]

    class Broken:
        def get(self, *args, **kwargs):
            raise RuntimeError("http://user:secret@private/path")

    with pytest.raises(ConflictStateUnavailable, match="conflict_state_unavailable") as error:
        HttpConflictStateProvider("http://private", "secret", session=Broken()).conflict_state(7)
    assert "secret" not in str(error.value)
