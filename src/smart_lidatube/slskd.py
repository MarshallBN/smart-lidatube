"""Review-only slskd discovery and fail-closed source conflict policy."""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import Protocol

import requests


class SlskdDiscoveryError(RuntimeError):
    """Public-safe slskd discovery failure."""


class SourceConflict(RuntimeError):
    """A competing manager is actively handling this track or album."""


class ConflictStateUnavailable(RuntimeError):
    """Competing-manager state could not be established safely."""


class ConflictStateProvider(Protocol):
    def conflict_state(self, track_id: int, album_id: int | None = None) -> dict[str, str]: ...


class HttpConflictStateProvider:
    """Runtime-configured adapter for a Soularr/Lidarr conflict-state service."""

    def __init__(self, endpoint, token, *, session=requests, timeout=(2, 5)):
        self.endpoint = str(endpoint)
        self.headers = {"Authorization": f"Bearer {token}"}
        self.session = session
        self.timeout = timeout

    def conflict_state(self, track_id, album_id=None):
        params = {"track_id": track_id}
        if album_id is not None:
            params["album_id"] = album_id
        try:
            response = self.session.get(
                self.endpoint, params=params, headers=self.headers, timeout=self.timeout
            )
            response.raise_for_status()
            return response.json()
        except Exception:
            raise ConflictStateUnavailable("conflict_state_unavailable") from None


class SoularrLidarrConflictGuard:
    """Require explicit idle state from Soularr and Lidarr before source work."""

    CLEAR_STATES = {"idle", "clear", "completed", "not_wanted"}
    ACTIVE_STATES = {"wanted", "downloading", "importing", "active", "queued"}

    def __init__(self, state_provider: ConflictStateProvider):
        self.state_provider = state_provider

    def require_clear(self, track_id, album_id=None, operation="search"):
        try:
            state = self.state_provider.conflict_state(track_id, album_id=album_id)
        except Exception as exc:
            raise ConflictStateUnavailable("conflict_state_unavailable") from None
        if not isinstance(state, dict) or set(state) != {"soularr", "lidarr"}:
            raise ConflictStateUnavailable("conflict_state_unavailable")
        normalized = {name: str(value).casefold() for name, value in state.items()}
        for service in ("soularr", "lidarr"):
            value = normalized[service]
            if value in self.ACTIVE_STATES:
                raise SourceConflict(f"active_{service}_work")
            if value not in self.CLEAR_STATES:
                raise ConflictStateUnavailable("conflict_state_unavailable")
        return True


class ReviewGatedDiscoverySources:
    """Route slskd metadata only to explicit Manual Retry review discovery."""

    def __init__(self, youtube, slskd, conflict_guard):
        self.youtube = youtube
        self.slskd = slskd
        self.conflict_guard = conflict_guard
        self._slskd_block = None

    def search_for_job(self, job, identity):
        youtube = self.youtube.search(identity["artist"], identity["title"])
        metadata = job.get("metadata") or {}
        explicit_manual = (
            job.get("mode") == "manual"
            and metadata.get("playlist_name") == "Manual Retry"
            and not metadata.get("audit_remediation")
        )
        if not explicit_manual:
            return youtube
        try:
            self.conflict_guard.require_clear(
                job["lidarr_track_id"], identity.get("album_id"), operation="search"
            )
        except (SourceConflict, ConflictStateUnavailable) as exc:
            self._slskd_block = type(exc).__name__
            return youtube
        self._slskd_block = None
        discovered = self.slskd.search(
            identity["artist"], identity["title"], album=identity.get("album")
        )
        return discovered + youtube

    def search(self, artist, title):
        """Compatibility route: never expose slskd without job review context."""
        return self.youtube.search(artist, title)

    def download(self, candidate, directory):
        if candidate.get("provider") == "slskd":
            raise SlskdDiscoveryError("slskd acquisition is not enabled")
        return self.youtube.download(candidate, directory)

    def health(self):
        slskd = self.slskd.health()
        if self._slskd_block:
            error = (
                "conflict_state_unavailable"
                if self._slskd_block == "ConflictStateUnavailable"
                else "active_manager_conflict"
            )
            slskd = {"state": "blocked", "error": error}
        return {
            "youtube": {"state": "available", "error": None},
            "slskd": slskd,
        }


class SlskdDiscoveryClient:
    """Bounded metadata-only slskd search client; it intentionally cannot transfer."""

    MAX_RESULT_CAP = 50

    def __init__(
        self, base_url, api_key, *, session=requests, timeout=(2, 5), result_cap=20,
        poll_attempts=3, poll_interval=0.2, opaque_key=None,
    ):
        self.base = str(base_url).rstrip("/")
        self.headers = {"X-API-Key": api_key}
        self.session = session
        self.timeout = timeout
        self.result_cap = min(self.MAX_RESULT_CAP, max(1, int(result_cap)))
        self.poll_attempts = min(10, max(1, int(poll_attempts)))
        self.poll_interval = max(0, float(poll_interval))
        # This key is an opacity salt, not an API credential. Deployments should
        # provide a stable runtime value if IDs must survive process restarts.
        self.opaque_key = str(opaque_key or api_key).encode()

    def search(self, artist, title, album=None, limit=None):
        cap = min(self.result_cap, max(1, int(limit or self.result_cap)))
        query = " ".join(part for part in (f"{artist} - {title}", album) if part)
        try:
            response = self.session.post(
                f"{self.base}/api/v0/searches", json={"searchText": query},
                headers=self.headers, timeout=self.timeout,
            )
            response.raise_for_status()
            search_id = response.json().get("id")
            if not search_id:
                raise ValueError("missing search id")
            payload = None
            for attempt in range(self.poll_attempts):
                result = self.session.get(
                    f"{self.base}/api/v0/searches/{search_id}",
                    headers=self.headers, timeout=self.timeout,
                )
                result.raise_for_status()
                payload = result.json()
                if str(payload.get("state", "")).casefold() in {"completed", "complete"}:
                    break
                if attempt + 1 < self.poll_attempts:
                    time.sleep(self.poll_interval)
            return self._safe_results(payload or {}, artist, title, album, cap)
        except Exception:
            raise SlskdDiscoveryError("slskd search unavailable") from None

    def health(self):
        try:
            response = self.session.get(
                f"{self.base}/api/v0/application", headers=self.headers,
                timeout=self.timeout,
            )
            response.raise_for_status()
            return {"state": "available", "error": None}
        except Exception:
            return {"state": "unavailable", "error": "slskd_unavailable"}

    def _safe_results(self, payload, artist, title, album, cap):
        output = []
        for peer_index, response in enumerate(payload.get("responses") or []):
            for file_index, item in enumerate(response.get("files") or []):
                filename = str(item.get("filename") or item.get("path") or "")
                codec = filename.rsplit(".", 1)[-1].casefold() if "." in filename else None
                source_material = "\0".join((
                    str(payload.get("id") or ""), str(response.get("username") or peer_index),
                    filename or str(file_index), str(item.get("size") or ""),
                )).encode()
                source_id = "slskd:" + hmac.new(
                    self.opaque_key, source_material, hashlib.sha256
                ).hexdigest()[:32]
                output.append({
                    "provider": "slskd", "source_id": source_id,
                    "artist": str(artist or ""), "title": str(title or ""),
                    "album": str(album or ""), "codec": codec,
                    "bitrate": self._bitrate(item.get("bitRate") or item.get("bitrate")),
                    "sample_rate": self._integer(item.get("sampleRate") or item.get("sample_rate")),
                    "bit_depth": self._integer(item.get("bitDepth") or item.get("bit_depth")),
                    "size_band": self._size_band(item.get("size")),
                    "duration": self._number(item.get("length") or item.get("duration")),
                })
                if len(output) >= cap:
                    return output
        return output

    @staticmethod
    def _integer(value):
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    @classmethod
    def _bitrate(cls, value):
        value = cls._integer(value)
        return value * 1000 if value is not None and value < 10000 else value

    @staticmethod
    def _number(value):
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    @classmethod
    def _size_band(cls, value):
        size = cls._integer(value)
        if size is None:
            return "unknown"
        if size < 10_000_000:
            return "small"
        if size < 100_000_000:
            return "medium"
        return "large"
