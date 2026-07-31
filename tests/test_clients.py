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
