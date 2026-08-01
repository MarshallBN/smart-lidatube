"""HTTP and media-source clients used by the smart worker."""

from pathlib import Path, PurePosixPath
from uuid import uuid4

import requests
import yt_dlp
from ytmusicapi import YTMusic


class LidarrClient:
    def __init__(self, base_url, api_key, session=requests, timeout=30,
                 navidrome_music_root=None, lidarr_music_root=None):
        self.base = base_url.rstrip("/")
        self.session = session
        self.timeout = timeout
        self.headers = {"X-Api-Key": api_key}
        self.navidrome_music_root = navidrome_music_root
        self.lidarr_music_root = lidarr_music_root

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

    def get_album(self, album_id):
        return self._get(f"album/{album_id}")

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

    def _paginated(self, endpoint, **params):
        """Return a bounded Lidarr collection, following its page envelope."""
        records = []
        page = 1
        while True:
            payload = self._get(endpoint, **params, page=page, pageSize=250)
            records.extend(self._records(payload))
            if not isinstance(payload, dict) or len(records) >= payload.get("totalRecords", len(records)):
                return records
            page += 1

    def resolve_track_by_path(self, song_path):
        """Resolve a Navidrome path without relying on its database IDs."""
        raw = str(song_path).replace("\\", "/")
        if self.navidrome_music_root and self.lidarr_music_root:
            try:
                relative = PurePosixPath(raw).relative_to(PurePosixPath(self.navidrome_music_root))
                raw = str(PurePosixPath(self.lidarr_music_root) / relative)
            except ValueError:
                return None
        wanted = PurePosixPath(raw)
        files = self._paginated("trackfile")
        matches = []
        for track_file in files:
            candidate = PurePosixPath(str(track_file.get("path", "")).replace("\\", "/"))
            candidate_parts = candidate.parts
            if candidate == wanted or (
                len(candidate_parts) >= len(wanted.parts)
                and candidate_parts[-len(wanted.parts) :] == wanted.parts
            ):
                matches.append(track_file)
        if len(matches) != 1:
            return None
        match = matches[0]
        file_id = match.get("id")
        tracks = self._records(self._get("track", trackFileId=file_id))
        for track in tracks:
            if track.get("trackFileId") == file_id or len(tracks) == 1:
                return track.get("id")
        return None

    @staticmethod
    def _normalized_metadata(value):
        """Compare display metadata without making filename-derived guesses."""
        import re

        value = re.sub(r"\s*\(\d{4}\)\s*", " ", str(value or ""))
        return re.sub(r"[^a-z0-9]+", "", value.casefold())

    @staticmethod
    def _entry_number(entry, *names):
        for name in names:
            value = entry.get(name)
            if value not in (None, ""):
                try:
                    return int(value)
                except (TypeError, ValueError):
                    return None
        return None

    def resolve_track_from_navidrome_entry(self, entry):
        """Resolve an entry by path first, then narrowly by Lidarr metadata.

        The fallback deliberately requires unique artist, album and track results;
        it never picks an arbitrary similarly named recording.
        """
        track_id = self.resolve_track_by_path(entry.get("path", ""))
        if track_id is not None:
            return track_id

        artist_name = entry.get("artist") or entry.get("artistName")
        album_title = entry.get("album") or entry.get("albumName")
        title = entry.get("title")
        if not all((artist_name, album_title, title)):
            return None
        artists = self._records(self._get("artist/lookup", term=artist_name))
        wanted_artist = self._normalized_metadata(artist_name)
        artists = [
            artist for artist in artists
            if self._normalized_metadata(artist.get("artistName") or artist.get("name")) == wanted_artist
            and isinstance(artist.get("id"), int)
            and not isinstance(artist.get("id"), bool)
        ]
        if len(artists) != 1:
            return None
        artist_id = artists[0]["id"]
        wanted_album = self._normalized_metadata(album_title)
        albums = [album for album in self._paginated("album", artistId=artist_id)
                  if self._normalized_metadata(album.get("title")) == wanted_album]
        if len(albums) != 1:
            return None
        album_id = albums[0].get("id")
        if album_id is None:
            return None
        file_ids = {item.get("id") for item in self._paginated("trackfile", artistId=artist_id)
                    if item.get("id") is not None}
        if not file_ids:
            return None
        wanted_title = self._normalized_metadata(title)
        track_number = self._entry_number(entry, "track", "trackNumber")
        disc_number = self._entry_number(entry, "discNumber", "disc")
        matches = []
        for track in self._paginated("track", albumId=album_id):
            if track.get("trackFileId") not in file_ids:
                continue
            if self._normalized_metadata(track.get("title")) != wanted_title:
                continue
            if track_number is not None and self._entry_number(track, "trackNumber", "track") != track_number:
                continue
            # Lidarr's current API calls the disc position mediumNumber; retain
            # legacy disc fields only as a fallback so mismatched media fail closed.
            if disc_number is not None and self._entry_number(track, "mediumNumber", "discNumber", "disc") != disc_number:
                continue
            matches.append(track)
        return matches[0].get("id") if len(matches) == 1 else None

    @staticmethod
    def track_identity(track):
        """Tolerantly extract expected MB recording ID, duration and labels."""
        recording_id = next(
            (
                value
                for value in (
                    track.get("musicBrainzRecordingId"),
                    track.get("foreignRecordingId"),
                    track.get("recordingId"),
                    track.get("foreignId"),
                    (track.get("recording") or {}).get("id"),
                    track.get("foreignTrackId"),
                    track.get("musicBrainzTrackId"),
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
    def _release_id(track, album=None):
        direct = track.get("albumReleaseId")
        if direct:
            return direct
        album = album if album is not None else track.get("album") or {}
        releases = album.get("releases", []) if isinstance(album, dict) else []
        valid = [release for release in releases if release.get("id") not in (None, "")]
        monitored = [release for release in valid if release.get("monitored")]
        if monitored:
            return sorted(monitored, key=lambda release: str(release["id"]))[0]["id"]
        official = [
            release for release in valid
            if str(release.get("status", "")).casefold() == "official"
        ]
        if official:
            return sorted(official, key=lambda release: str(release["id"]))[0]["id"]
        return None

    def _manual_import_release_id(self, track):
        release_id = self._release_id(track)
        if release_id:
            return release_id
        album_id = track.get("albumId")
        if album_id in (None, ""):
            return None
        album = self.get_album(album_id)
        release_id = self._release_id(track, album)
        if release_id:
            return release_id
        raise ValueError(f"manual import album {album_id} has no releases suitable for import")

    def manual_import(self, path, track):
        required = {"id": track.get("id"), "artistId": track.get("artistId"),
                    "albumId": track.get("albumId"), "albumReleaseId": self._manual_import_release_id(track)}
        if any(value in (None, "") for value in required.values()):
            raise ValueError("manual import requires track, artist, album and release IDs")
        body = {
            "name": "ManualImport",
            "files": [{
                "path": str(path),
                "artistId": required["artistId"],
                "albumId": required["albumId"],
                "albumReleaseId": required["albumReleaseId"],
                "trackIds": [required["id"]],
                "quality": {},
                "indexerFlags": 0,
                "downloadId": "",
                "disableReleaseSwitching": False,
            }],
            "importMode": "Auto",
            "replaceExistingFiles": True,
        }
        response = self.session.post(
            f"{self.base}/api/v1/command",
            json=body,
            headers={**self.headers, "Content-Type": "application/json"},
            timeout=self.timeout,
        )
        response.raise_for_status()
        try:
            result = response.json()
        except (ValueError, AttributeError):
            return None
        return result

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

    def remove_entry(self, playlist_id, index, expected_id=None, expected=None):
        expected = expected or ({"id": expected_id} if expected_id is not None else None)
        if expected is not None:
            entries = (
                self._call("getPlaylist", id=playlist_id)
                .get("playlist", {})
                .get("entry", [])
            )
            fields = ("id", "path", "title", "artist", "album")
            if index >= len(entries) or any(str(entries[index].get(key, "")) != str(expected.get(key, "")) for key in fields if key in expected):
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
        directory = Path(directory) / f"attempt-{uuid4().hex}"
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
            info = downloader.extract_info(candidate["url"], download=True)
        reported = [Path(item["filepath"]) for item in (info or {}).get("requested_downloads", []) if item.get("filepath")]
        audio_ext = {".m4a", ".mp3", ".flac", ".ogg", ".opus", ".wav", ".aac", ".webm"}
        matches = [item for item in reported + sorted(directory.glob(f"{candidate['source_id']}.*"))
                   if item.suffix.lower() in audio_ext and item.is_file() and item.stat().st_size > 0]
        if not matches:
            raise RuntimeError("yt-dlp completed without producing an audio file")
        return matches[0]
