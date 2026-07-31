import os
import sqlite3
from pathlib import Path

import pytest

from smart_lidatube.clients import LidarrClient, NavidromeClient, YouTubeClient
from smart_lidatube.fingerprint import AcoustIDClient, FileVerifier, Fingerprint
from smart_lidatube.retry import PlaylistPoller
from smart_lidatube.store import Store
from smart_lidatube.telegram import TelegramBot
from smart_lidatube.worker import JobWorker, translate_staged_path


class Response:
    def __init__(self, data=None, status=200, headers=None):
        self.data, self.status_code, self.headers = data, status, headers or {}
    def json(self):
        if isinstance(self.data, Exception): raise self.data
        return self.data
    def raise_for_status(self):
        if self.status_code >= 400: raise RuntimeError(self.status_code)


def test_staged_path_translation_is_explicit_and_fails_closed(tmp_path):
    root = tmp_path / "downloads"
    candidate = root / ".smart-staging/1/a.m4a"
    candidate.parent.mkdir(parents=True); candidate.write_bytes(b"x")
    assert translate_staged_path(candidate, root, "/lidarr/incoming") == Path("/lidarr/incoming/.smart-staging/1/a.m4a")
    with pytest.raises(ValueError): translate_staged_path(tmp_path / "escape.m4a", root, "/lidarr/incoming")
    escaped = root / ".smart-staging/link"
    escaped.parent.mkdir(parents=True, exist_ok=True); escaped.symlink_to(tmp_path / "escape.m4a")
    with pytest.raises(ValueError): translate_staged_path(escaped, root, "/lidarr/incoming")


def test_claim_lease_recovery_retry_schedule_and_import_state(tmp_path):
    store = Store(tmp_path / "db")
    job = store.enqueue_job(1, "lease")
    claimed = store.claim_job(lease_seconds=60)
    assert claimed["claim_token"] and claimed["status"] == "processing"
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE retry_jobs SET claimed_at=datetime('now','-2 hours') WHERE id=?", (job,))
    assert store.recover_stale(60) == 1
    assert store.get_job(job)["status"] == "queued"
    store.schedule_retry(job, "network", delay=30)
    assert store.get_job(job)["retry_count"] == 1
    assert store.claim_job() is None
    store.begin_import(job, 9, "/lidarr/stage/a.m4a", {"status":"queued"})
    saved = store.get_job(job)
    assert saved["status"] == "importing" and saved["prior_track_file_id"] == 9


def test_manual_import_discovery_round_trip_and_required_ids():
    calls = []
    class Session:
        def get(self, url, **kwargs):
            calls.append(("get", url, kwargs))
            return Response([{"path":"/stage/a.m4a", "quality":{"quality":{"id":7}}, "artistId":99}])
        def post(self, url, **kwargs):
            calls.append(("post", url, kwargs)); return Response({"status":"queued"}, 202)
    track={"id":1,"title":"S","artistId":2,"albumId":3,"albumReleaseId":4}
    LidarrClient("http://l", "k", session=Session()).manual_import("/stage/a.m4a", track)
    body=calls[-1][2]["json"][0]
    assert calls[0][0] == "get" and body["quality"]["quality"]["id"] == 7
    assert body["artistId"] == 2 and body["trackIds"] == [1] and body["replaceExistingFiles"] is True
    with pytest.raises(ValueError): LidarrClient("http://l", "k", session=Session()).manual_import("/stage/a", {"id":1})


def test_paginated_path_resolution_translation_and_ambiguity():
    class Session:
        def get(self, url, **kwargs):
            params=kwargs.get("params") or {}
            if url.endswith("trackfile"):
                page=params.get("page",1)
                return Response({"page":page,"pageSize":1,"totalRecords":2,"records":[{"id":page,"path":f"/lidarr/music/A/{'X' if page==1 else 'S'}.mp3"}]})
            return Response([{"id":7,"trackFileId":2}])
    c=LidarrClient("http://l","k",session=Session(), navidrome_music_root="/nav/music", lidarr_music_root="/lidarr/music")
    assert c.resolve_track_by_path("/nav/music/A/S.mp3") == 7
    class Amb(Session):
        def get(self,url,**kwargs):
            if url.endswith("trackfile"): return Response([{"id":1,"path":"/x/A/S.mp3"},{"id":2,"path":"/y/A/S.mp3"}])
            return Response([])
    assert LidarrClient("http://l","k",session=Amb()).resolve_track_by_path("A/S.mp3") is None


def test_import_submission_is_resumable_and_verified_before_cleanup(tmp_path):
    store=Store(tmp_path/"db"); job=store.enqueue_job(7,"j"); attempt=store.add_attempt(job,"youtube","x")
    staged=tmp_path/"downloads/.smart-staging/1/x.m4a"; staged.parent.mkdir(parents=True); staged.write_bytes(b"audio")
    store.update_attempt(attempt, verdict="manual_accepted", staged_path=staged)
    class Lidarr:
        def __init__(self): self.posts=0; self.file_id=10
        def get_track(self,_): return {"id":7,"trackFileId":self.file_id,"artistId":2,"albumId":3,"albumReleaseId":4}
        def track_identity(self,t): return {"track_file_id":t.get("trackFileId"),"artist":"A","title":"S"}
        def manual_import(self,path,track): self.posts+=1; assert str(path).startswith("/visible/"); return {"status":"queued"}
    lid=Lidarr(); worker=JobWorker(store,lid,None,None,tmp_path/"downloads",lidarr_downloads_root="/visible")
    assert worker.process_once()==job
    assert store.get_job(job)["status"] == "importing" and lid.posts == 1 and staged.exists()
    assert worker.process_once() is None  # importing is reconciled separately, never submitted twice
    assert worker.reconcile_imports() == 0 and lid.posts == 1
    lid.file_id=11
    assert worker.reconcile_imports() == 1
    assert store.get_job(job)["status"] == "completed" and not staged.parent.exists()


def test_repeat_playlist_occurrence_after_consumption_creates_new_job(tmp_path):
    entry={"id":"s","path":"A/S.mp3","title":"S","playlist_id":"p","playlist_name":"Retry","playlist_index":0}
    class Nav:
        def retry_entries(self): return [dict(entry)]
        def remove_entry(self,*args,**kwargs): return True
    store=Store(tmp_path/"db"); poller=PlaylistPoller(Nav(),store,lambda e:1)
    first=poller.poll_once()[0]; assert poller.poll_once()[0] != first


def test_removal_compares_full_occurrence():
    class Client(NavidromeClient):
        def __init__(self): pass
        def _call(self,name,**params):
            if name=="getPlaylist": return {"playlist":{"entry":[{"id":"s","path":"B/wrong.mp3","title":"S"}]}}
            raise AssertionError("must not remove")
    assert Client().remove_entry("p",0,expected={"id":"s","path":"A/S.mp3","title":"S"}) is False


def test_telegram_callback_one_shot_and_offset_persisted(tmp_path):
    store=Store(tmp_path/"db"); job=store.enqueue_job(1,"x"); attempt=store.add_attempt(job,"youtube","x")
    store.update_attempt(attempt,verdict="awaiting_review"); store.update_job(job,"awaiting_review")
    sent=[]; bot=TelegramBot("t",store,{1},{2},request=lambda m,p: sent.append((m,p)) or {"result":[]})
    q={"id":"q","from":{"id":1},"message":{"chat":{"id":2}},"data":f"attempt:{attempt}:accept"}
    assert bot.handle_callback(q) is True
    assert bot.handle_callback(q) is False and "already" in sent[-1][1].get("text","").lower()
    store.set_setting("telegram_update_offset","42")
    assert TelegramBot("t",store,{1},{2},request=lambda m,p:{"result":[]}).offset == 42


def test_acoustid_without_key_is_inconclusive():
    class FP:
        def calculate(self,path): return Fingerprint(10,"fp")
    result=FileVerifier(FP(),AcoustIDClient("")).verify_file("x",{"duration":10})
    assert result.verdict == "inconclusive" and result.reason == "acoustid_disabled"


def test_ytdlp_ignores_sidecars_and_uses_fresh_attempt_dir(tmp_path):
    class YDL:
        def __init__(self,o): self.o=o
        def __enter__(self): return self
        def __exit__(self,*a): pass
        def extract_info(self,url,download):
            target=Path(self.o["outtmpl"].replace("%(ext)s","m4a")); target.write_bytes(b"audio")
            target.with_suffix(".json").write_text("{}")
            return {"requested_downloads":[{"filepath":str(target)}]}
    c=YouTubeClient(ytmusic=object(),ydl_factory=YDL)
    first=c.download({"source_id":"x","url":"u"},tmp_path)
    second=c.download({"source_id":"x","url":"u"},tmp_path)
    assert first.suffix==second.suffix==".m4a" and first.parent != second.parent
