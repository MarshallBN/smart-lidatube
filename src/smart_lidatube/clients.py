"""HTTP and media-source clients used by the smart worker."""

from pathlib import Path, PurePosixPath

import requests
import yt_dlp
from ytmusicapi import YTMusic


class LidarrClient:
    def __init__(self, base_url, api_key, session=requests, timeout=30):
        self.base = base_url.rstrip("/")
        self.session = session
        self.timeout = timeout
        self.headers = {"X-Api-Key": api_key}

    def _get(self, endpoint, **params):
        response = self.session.get(
            f"{self.base}/api/v1/{endpoint}",
            headers=self.headers,
            params=params or None,
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response.json()

    def get_track(self, track_id):
        return self._get(f"track/{track_id}")

    def get_track_file(self, file_id):
        return self._get(f"trackfile/{file_id}")

    def delete_track_file(self, file_id):
        response = self.session.delete(
            f"{self.base}/api/v1/trackfile/{file_id}",
            headers=self.headers,
            timeout=self.timeout,
        )
        response.raise_for_status()

    @staticmethod
    def _records(payload):
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            return payload.get("records", [])
        return []

    def resolve_track_by_path(self, song_path):
        """Resolve a Navidrome path without relying on its database IDs."""
        wanted = PurePosixPath(str(song_path).replace("\\", "/"))
        files = self._records(self._get("trackfile", pageSize=100000))
        match = None
        for track_file in files:
            candidate = PurePosixPath(str(track_file.get("path", "")).replace("\\", "/"))
            candidate_parts = candidate.parts
            if candidate == wanted or (
                len(candidate_parts) >= len(wanted.parts)
                and candidate_parts[-len(wanted.parts) :] == wanted.parts
            ):
                match = track_file
                break
        if not match:
            return None
        file_id = match.get("id")
        tracks = self._records(self._get("track", trackFileId=file_id))
        for track in tracks:
            if track.get("trackFileId") == file_id or len(tracks) == 1:
                return track.get("id")
        return None

    @staticmethod
    def track_identity(track):
        """Tolerantly extract expected MB recording ID, duration and labels."""
        recording_id = next(
            (
                value
                for value in (
                    track.get("foreignTrackId"),
                    track.get("musicBrainzTrackId"),
                    track.get("musicBrainzRecordingId"),
                    track.get("foreignId"),
                    (track.get("recording") or {}).get("id"),
                )
                if value
            ),
            None,
        )
        raw_duration = next(
            (
                value
                for value in (
                    track.get("duration"),
                    track.get("durationMs"),
                    track.get("length"),
                    track.get("trackDuration"),
                )
                if value is not None
            ),
            None,
        )
        duration = float(raw_duration) if raw_duration else None
        if duration and duration > 10000:
            duration /= 1000
        artist = track.get("artist") or {}
        if isinstance(artist, dict):
            artist = artist.get("artistName") or artist.get("name")
        album = track.get("album") or {}
        return {
            "recording_id": recording_id,
            "duration": duration,
            "artist": artist or track.get("artistName") or "",
            "title": track.get("title") or "",
            "album": album.get("title") if isinstance(album, dict) else album,
            "track_file_id": track.get("trackFileId"),
        }

    @staticmethod
    def _release_id(track):
        direct = track.get("albumReleaseId")
        if direct:
            return direct
        album = track.get("album") or {}
        releases = album.get("releases", []) if isinstance(album, dict) else []
        monitored = next((item for item in releases if item.get("monitored")), None)
        selected = monitored or (releases[0] if releases else None)
        return selected.get("id") if selected else None

    def manual_import(self, path, track):
        body = {
            "id": track["id"],
            "path": str(path),
            "name": track.get("title", ""),
            "artistId": track.get("artistId"),
            "albumId": track.get("albumId"),
            "albumReleaseId": self._release_id(track),
            "quality": {},
            "releaseGroup": "",
            "indexerFlags": 0,
            "downloadId": "",
            "additionalFile": False,
            "replaceExistingFiles": True,
            "disableReleaseSwitching": False,
            "rejections": [],
        }
        response = self.session.post(
            f"{self.base}/api/v1/manualimport",
            json=[body],
            headers={**self.headers, "Content-Type": "application/json"},
            timeout=self.timeout,
        )
        response.raise_for_status()
        try:
            return response.json()
        except (ValueError, AttributeError):
            return None

    def import_succeeded(self, result):
        """Reject an explicit Lidarr failure; async queued responses are successful."""
        if isinstance(result, dict):
            return str(result.get("status", "queued")).lower() not in {
                "failed",
                "aborted",
            }
        return result is not False


class NavidromeClient:
    def __init__(self, base_url, user, password, get=requests.get, timeout=15):
        self.base = base_url.rstrip("/")
        self.get = get
        self.timeout = timeout
        self.auth = {
            "u": user,
            "p": password,
            "v": "1.16.1",
            "c": "smart-lidatube",
            "f": "json",
        }

    def _call(self, name, **params):
        response = self.get(
            f"{self.base}/{name}.view",
            params={**self.auth, **params},
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response.json()["subsonic-response"]

    def retry_entries(self):
        playlists = (
            self._call("getPlaylists").get("playlists", {}).get("playlist", [])
        )
        output = []
        for playlist in playlists:
            if playlist["name"] not in ("Retry", "Manual Retry"):
                continue
            entries = (
                self._call("getPlaylist", id=playlist["id"])
                .get("playlist", {})
                .get("entry", [])
            )
            output.extend(
                {
                    **song,
                    "playlist_id": playlist["id"],
                    "playlist_name": playlist["name"],
                    "playlist_index": index,
                }
                for index, song in enumerate(entries)
            )
        return output

    def remove_entry(self, playlist_id, index, expected_id=None):
        if expected_id is not None:
            entries = (
                self._call("getPlaylist", id=playlist_id)
                .get("playlist", {})
                .get("entry", [])
            )
            if index >= len(entries) or str(entries[index].get("id")) != str(expected_id):
                return False
        self._call(
            "updatePlaylist", playlistId=playlist_id, songIndexToRemove=index
        )
        return True


class YouTubeClient:
    """YTMusic candidate search and yt-dlp audio download adapter."""

    def __init__(self, ytmusic=None, ydl_factory=None, cookies=None):
        self.ytmusic = ytmusic or YTMusic()
        self.ydl_factory = ydl_factory or yt_dlp.YoutubeDL
        self.cookies = cookies

    def search(self, artist, title, limit=10):
        results = self.ytmusic.search(
            query=f"{artist} - {title}", filter="songs", limit=limit
        )
        candidates = []
        for result in results:
            source_id = result.get("videoId")
            if not source_id:
                continue
            artists = result.get("artists") or []
            candidates.append(
                {
                    "provider": "youtube",
                    "source_id": source_id,
                    "url": f"https://www.youtube.com/watch?v={source_id}",
                    "title": result.get("title"),
                    "artist": ", ".join(
                        item.get("name", "") for item in artists if item.get("name")
                    ),
                    "duration": result.get("duration_seconds")
                    or result.get("durationSeconds"),
                }
            )
        return candidates

    def download(self, candidate, directory):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        template = str(directory / f"{candidate['source_id']}.%(ext)s")
        options = {
            "format": "bestaudio/best",
            "outtmpl": template,
            "noplaylist": True,
            "quiet": True,
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "m4a",
                    "preferredquality": "0",
                }
            ],
        }
        if self.cookies:
            options["cookiefile"] = self.cookies
        with self.ydl_factory(options) as downloader:
            downloader.extract_info(candidate["url"], download=True)
        matches = sorted(directory.glob(f"{candidate['source_id']}.*"))
        if not matches:
            raise RuntimeError("yt-dlp completed without producing an audio file")
        return matches[0]
