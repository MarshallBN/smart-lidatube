from smart_lidatube.clients import LidarrClient, NavidromeClient


class Response:
    def __init__(self, data=None, status=200): self._data=data or {}; self.status_code=status
    def json(self): return self._data
    def raise_for_status(self):
        if self.status_code >= 400: raise RuntimeError(self.status_code)


def test_lidarr_manual_import_replace_body():
    calls=[]
    session=type("S",(),{"get":lambda s,*a,**k:Response({}), "post":lambda s,*a,**k:(calls.append((a,k)) or Response(status=202)), "delete":lambda s,*a,**k:Response()})()
    c=LidarrClient("http://lidarr", "key", session=session)
    c.manual_import("/lidatube/downloads/.smart-staging/1/candidate.m4a", {"id":12,"title":"Song","artistId":2,"albumId":3,"albumReleaseId":4})
    body=calls[0][1]["json"][0]
    assert body["replaceExistingFiles"] is True and body["path"].startswith("/lidatube/downloads/.smart-staging/")
    assert calls[0][0][0].endswith("/api/v1/manualimport")


def test_lidarr_resolve_and_delete_file_api():
    calls=[]
    session=type("S",(),{"get":lambda s,url,**k:Response({"id":7,"trackFileId":9}), "delete":lambda s,url,**k:(calls.append(url) or Response()), "post":lambda *a,**k:Response(status=202)})()
    c=LidarrClient("http://lidarr", "key", session=session)
    assert c.get_track(7)["trackFileId"] == 9
    c.delete_track_file(9)
    assert calls == ["http://lidarr/api/v1/trackfile/9"]


def test_navidrome_playlist_metadata_and_remove():
    calls=[]
    def get(url, params=None, timeout=None):
        calls.append((url,params))
        if url.endswith("getPlaylists.view"): return Response({"subsonic-response":{"playlists":{"playlist":[{"id":"p1","name":"Retry"},{"id":"p2","name":"Other"}]}}})
        if url.endswith("getPlaylist.view"): return Response({"subsonic-response":{"playlist":{"entry":[{"id":"s1","path":"A/S.mp3","title":"S","artist":"A"}]}}})
        return Response({"subsonic-response":{"status":"ok"}})
    c=NavidromeClient("http://nav/rest", "u", "p", get=get)
    items=c.retry_entries()
    assert items[0]["path"] == "A/S.mp3" and items[0]["playlist_id"] == "p1"
    c.remove_entry("p1", 0)
    assert calls[-1][1]["songIndexToRemove"] == 0


def test_track_identity_prefers_lidarr_recording_mbid_over_track_mbid():
    payload = {"id": 242282, "foreignTrackId": "musicbrainz-track-mbid",
               "foreignRecordingId": "musicbrainz-recording-mbid", "title": "Godzilla"}
    assert LidarrClient.track_identity(payload)["recording_id"] == "musicbrainz-recording-mbid"


def test_navidrome_entry_resolves_renamed_lidarr_file_by_metadata():
    class Session:
        def get(self, url, **kwargs):
            params = kwargs.get("params") or {}
            if url.endswith("/trackfile") and not params.get("artistId"):
                return Response([])  # Path differs after Lidarr rename.
            if url.endswith("/artist/lookup"):
                return Response([{"id": 1318, "artistName": "Eminem"}])
            if url.endswith("/album"):
                assert params == {"artistId": 1318, "page": 1, "pageSize": 250}
                return Response([{"id": 22984, "title": "Music to Be Murdered By (2020)"}])
            if url.endswith("/trackfile"):
                assert params["artistId"] == 1318
                return Response([{"id": 99}])
            if url.endswith("/track"):
                return Response([{"id": 242282, "artistId": 1318, "albumId": 22984,
                                  "trackFileId": 99, "title": "Godzilla", "trackNumber": 7,
                                  "discNumber": 1}])
            raise AssertionError(url)

    entry = {"path": "Eminem/Music to Be Murdered By/01-07 - Godzilla.mp3",
             "artist": "Eminem", "album": "Music to Be Murdered By (2020)",
             "title": "Godzilla", "track": 7, "discNumber": 1}
    client = LidarrClient("http://lidarr", "key", session=Session())
    assert client.resolve_track_from_navidrome_entry(entry) == 242282


def test_navidrome_entry_metadata_fallback_uses_lidarr_medium_number_for_disc():
    """Lidarr exposes the disc/medium position as mediumNumber on live tracks."""
    class Session:
        def get(self, url, **kwargs):
            params = kwargs.get("params") or {}
            if url.endswith("/trackfile") and not params.get("artistId"):
                return Response([])
            if url.endswith("/artist/lookup"):
                return Response([{"id": 1318, "artistName": "Eminem"}])
            if url.endswith("/album"):
                return Response([{"id": 22984, "title": "Music to Be Murdered By (2020)"}])
            if url.endswith("/trackfile"):
                return Response([{"id": 99}])
            if url.endswith("/track"):
                return Response([{"id": 242282, "artistId": 1318, "albumId": 22984,
                                  "trackFileId": 99, "title": "Godzilla", "trackNumber": "7",
                                  "mediumNumber": 1, "discNumber": None}])
            raise AssertionError(url)

    entry = {"path": "Eminem/Music to Be Murdered By/01-07 - Godzilla.mp3",
             "artist": "Eminem", "album": "Music to Be Murdered By (2020)",
             "title": "Godzilla", "track": 7, "discNumber": 1}
    assert LidarrClient("http://lidarr", "key", session=Session()).resolve_track_from_navidrome_entry(entry) == 242282


def test_navidrome_entry_metadata_fallback_ignores_remote_artist_lookup_suggestion():
    class Session:
        def get(self, url, **kwargs):
            params = kwargs.get("params") or {}
            if url.endswith("/trackfile") and not params.get("artistId"):
                return Response([])  # Path differs after Lidarr rename.
            if url.endswith("/artist/lookup"):
                return Response([
                    {"id": 1318, "artistName": "Eminem", "path": "/music/Eminem", "monitored": True},
                    {"id": None, "artistName": "Eminem", "path": None, "monitored": False},
                ])
            if url.endswith("/album"):
                assert params == {"artistId": 1318, "page": 1, "pageSize": 250}
                return Response([{"id": 22984, "title": "Music to Be Murdered By (2020)"}])
            if url.endswith("/trackfile"):
                assert params["artistId"] == 1318
                return Response([{"id": 99}])
            if url.endswith("/track"):
                return Response([{"id": 242282, "artistId": 1318, "albumId": 22984,
                                  "trackFileId": 99, "title": "Godzilla", "trackNumber": 7,
                                  "discNumber": 1}])
            raise AssertionError(url)

    entry = {"path": "Eminem/Music to Be Murdered By/01-07 - Godzilla.mp3",
             "artist": "Eminem", "album": "Music to Be Murdered By (2020)",
             "title": "Godzilla", "track": 7, "discNumber": 1}
    assert LidarrClient("http://lidarr", "key", session=Session()).resolve_track_from_navidrome_entry(entry) == 242282


def test_navidrome_entry_metadata_fallback_fails_closed_for_multiple_local_artist_ids():
    class Session:
        def get(self, url, **kwargs):
            params = kwargs.get("params") or {}
            if url.endswith("/trackfile") and not params.get("artistId"):
                return Response([])
            if url.endswith("/artist/lookup"):
                return Response([
                    {"id": 1318, "artistName": "Eminem", "path": "/music/Eminem", "monitored": True},
                    {"id": 1319, "artistName": "Eminem", "path": "/music/Eminem 2", "monitored": True},
                ])
            raise AssertionError("ambiguous artist lookup must not query further endpoints")

    entry = {"path": "Eminem/Album/01 - Song.mp3", "artist": "Eminem",
             "album": "Album", "title": "Song", "track": 1}
    assert LidarrClient("http://lidarr", "key", session=Session()).resolve_track_from_navidrome_entry(entry) is None


def test_navidrome_entry_metadata_fallback_fails_closed_on_ambiguity():
    class Session:
        def get(self, url, **kwargs):
            params = kwargs.get("params") or {}
            if url.endswith("/trackfile") and not params.get("artistId"):
                return Response([])
            if url.endswith("/artist/lookup"):
                return Response([{"id": 1, "artistName": "Artist"}])
            if url.endswith("/album"):
                return Response([{"id": 2, "title": "Album"}])
            if url.endswith("/trackfile"):
                return Response([{"id": 10}, {"id": 11}])
            if url.endswith("/track"):
                return Response([
                    {"id": 7, "albumId": 2, "trackFileId": 10, "title": "Song", "trackNumber": 1},
                    {"id": 8, "albumId": 2, "trackFileId": 11, "title": "Song", "trackNumber": 1},
                ])
            raise AssertionError(url)

    entry = {"path": "Artist/Album/01 - Song.mp3", "artist": "Artist",
             "album": "Album", "title": "Song", "track": 1}
    assert LidarrClient("http://lidarr", "key", session=Session()).resolve_track_from_navidrome_entry(entry) is None
