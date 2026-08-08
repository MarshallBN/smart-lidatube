import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from smart_lidatube.audit import AuditConfig, AuditWorker
from smart_lidatube.fingerprint import Verification
from smart_lidatube.quality import FFprobe, ProbeError
from smart_lidatube.store import Store


def test_quality_inventory_migrates_existing_database_and_replaces_file_marker(tmp_path):
    path = tmp_path / "legacy.db"
    sqlite3.connect(path).execute("CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT NOT NULL)").connection.close()
    store = Store(path)
    store.upsert_quality(7, "10:20", {"codec": "mp3", "container": "mp3", "bitrate": 192000,
        "sample_rate": 44100, "bit_depth": None, "channels": 2, "lossless": False,
        "duration": 123.5, "size_band": "small", "source": "lidarr", "confidence": "reported"})
    store.upsert_quality(7, "11:21", {"codec": "flac", "container": "flac", "lossless": True,
        "size_band": "medium", "source": "probe", "confidence": "measured"})
    row = store.get_quality(7)
    assert row["file_marker"] == "11:21" and row["codec"] == "flac"
    assert "path" not in row
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM quality_inventory WHERE lidarr_track_id=7").fetchone()[0] == 1


def test_ffprobe_is_shell_free_and_reports_timeout_or_malformed_safely():
    seen = {}
    def timeout(args, **kwargs):
        seen.update(args=args, kwargs=kwargs)
        raise subprocess.TimeoutExpired(args, kwargs["timeout"], output="/secret/path")
    with pytest.raises(ProbeError, match="media probe unavailable"):
        FFprobe(run=timeout, timeout=3).probe(Path("/secret/file.mp3"))
    assert seen["kwargs"]["shell"] is False and seen["kwargs"]["timeout"] == 3

    malformed = lambda *a, **k: subprocess.CompletedProcess(a, 0, "not-json /secret", "raw secret")
    with pytest.raises(ProbeError, match="media probe unavailable"):
        FFprobe(run=malformed).probe(Path("/secret/file.mp3"))


def test_audit_collects_lidarr_quality_then_probes_only_missing_facts_without_refingerprint(tmp_path):
    media = tmp_path / "music" / "Artist" / "Song.m4a"
    media.parent.mkdir(parents=True); media.write_bytes(b"audio")
    class Lidarr:
        def get_track(self, track_id): return {"id": track_id, "trackFileId": 9}
        def get_track_file(self, file_id): return {"id": file_id, "path": "/Music/Artist/Song.m4a", "size": 5,
            "mediaInfo": {"audioCodec": "aac", "audioBitrate": 256000, "audioChannels": 2}}
        @staticmethod
        def track_identity(track): return {"track_file_id": 9, "artist": "Artist", "title": "Song"}
    class Verifier:
        calls = 0
        def verify_file(self, path, identity): self.calls += 1; return Verification("accepted", "recording_match", {})
    class Probe:
        calls = 0
        def probe(self, path): self.calls += 1; return {"container": "mov,mp4", "sample_rate": 48000,
            "bit_depth": 24, "duration": 100.0, "lossless": False}
    store = Store(tmp_path / "db"); store.upsert_audit_track(7)
    verifier, probe = Verifier(), Probe()
    worker = AuditWorker(store, Lidarr(), verifier, AuditConfig(), probe=probe,
        lidarr_music_root="/Music", audit_music_root=str(tmp_path / "music"))
    assert worker.process_once() == 7
    saved = store.get_quality(7)
    assert {k: saved[k] for k in ("codec", "bitrate", "sample_rate", "bit_depth", "channels", "source")} == {
        "codec": "aac", "bitrate": 256000, "sample_rate": 48000, "bit_depth": 24, "channels": 2, "source": "lidarr+probe"}
    assert verifier.calls == 1 and probe.calls == 1
    assert str(tmp_path) not in json.dumps(saved)


def test_complete_lidarr_media_info_skips_probe(tmp_path):
    media = tmp_path / "music" / "song.flac"; media.parent.mkdir(); media.write_bytes(b"audio")
    class Lidarr:
        def get_track(self, _): return {"id": 1, "trackFileId": 2}
        def get_track_file(self, _): return {"path": "/Music/song.flac", "mediaInfo": {
            "audioCodec": "flac", "containerFormat": "flac", "audioBitrate": 900000,
            "audioSampleRate": 44100, "audioBits": 16, "audioChannels": 2, "duration": 20}}
        @staticmethod
        def track_identity(_): return {"track_file_id": 2}
    class Probe:
        def probe(self, _): raise AssertionError("complete Lidarr facts must skip probe")
    class Verifier:
        def verify_file(self, *_): return Verification("accepted", "recording_match", {})
    store = Store(tmp_path / "db"); store.upsert_audit_track(1)
    assert AuditWorker(store, Lidarr(), Verifier(), AuditConfig(), probe=Probe(),
        lidarr_music_root="/Music", audit_music_root=str(media.parent)).process_once() == 1
    assert store.get_quality(1)["source"] == "lidarr"
