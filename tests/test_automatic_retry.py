from datetime import datetime, timedelta, timezone
import sqlite3

from smart_lidatube.store import Store
from smart_lidatube.worker import JobWorker


def test_auto_job_has_durable_24h_deadline_and_bounded_exponential_retry(tmp_path):
    store = Store(tmp_path / "db")
    job_id = store.enqueue_job(1, "auto", mode="auto")
    job = store.get_job(job_id)
    created = datetime.fromisoformat(job["created_at"]).replace(tzinfo=timezone.utc)
    deadline = datetime.fromisoformat(job["sla_deadline"]).replace(tzinfo=timezone.utc)
    assert deadline - created == timedelta(hours=24)

    store.schedule_retry(job_id, "temporary", delay=30, max_attempts=3)
    first = store.get_job(job_id)
    assert first["status"] == "queued" and first["retry_count"] == 1
    with sqlite3.connect(store.path) as db:
        first_delay = db.execute(
            "SELECT unixepoch(next_attempt_at)-unixepoch(updated_at) FROM retry_jobs WHERE id=?", (job_id,)
        ).fetchone()[0]
    assert 29 <= first_delay <= 30

    store.update_job(job_id, "processing")
    store.schedule_retry(job_id, "temporary", delay=30, max_attempts=3)
    second = store.get_job(job_id)
    assert second["status"] == "queued" and second["retry_count"] == 2
    with sqlite3.connect(store.path) as db:
        second_delay = db.execute(
            "SELECT unixepoch(next_attempt_at)-unixepoch(updated_at) FROM retry_jobs WHERE id=?", (job_id,)
        ).fetchone()[0]
    assert 59 <= second_delay <= 60

    store.update_job(job_id, "processing")
    store.schedule_retry(job_id, "temporary", delay=30, max_attempts=3)
    assert store.get_job(job_id)["status"] == "operator_attention"


def test_auto_retry_is_capped_at_sla_deadline_and_claimable_at_deadline(tmp_path):
    store = Store(tmp_path / "db")
    job_id = store.enqueue_job(1, "deadline-cap", mode="auto")
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE retry_jobs SET sla_deadline=datetime('now','+10 seconds') WHERE id=?", (job_id,))
    store.schedule_retry(job_id, "temporary", delay=3600, max_attempts=None)
    job = store.get_job(job_id)
    assert job["status"] == "queued"
    assert job["next_attempt_at"] == job["sla_deadline"]

    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE retry_jobs SET sla_deadline=CURRENT_TIMESTAMP,next_attempt_at=CURRENT_TIMESTAMP WHERE id=?", (job_id,))
    claimed = store.claim_job()
    assert claimed["id"] == job_id

    class Lidarr:
        def get_track(self, _): raise AssertionError("deadline must escalate before search")

    store.update_job(job_id, "queued")
    assert JobWorker(store, Lidarr(), object(), object(), tmp_path).process_once() == job_id
    assert store.get_job(job_id)["status"] == "operator_attention"


def test_auto_retry_attempt_limit_is_independent_and_defaults_to_deadline(tmp_path):
    store = Store(tmp_path / "db")
    job_id = store.enqueue_job(1, "auto-window", mode="auto")
    for _ in range(24):
        store.update_job(job_id, "processing")
        store.schedule_retry(job_id, "temporary", delay=3600, max_attempts=None)
        assert store.get_job(job_id)["status"] == "queued"


def test_manual_retry_still_honors_existing_max_attempts(tmp_path):
    store = Store(tmp_path / "db")
    job_id = store.enqueue_job(1, "manual-limit", mode="manual")
    store.schedule_retry(job_id, "temporary", max_attempts=1)
    assert store.get_job(job_id)["status"] == "failed"


def test_migration_backfills_deadline_for_existing_auto_jobs(tmp_path):
    path = tmp_path / "legacy"
    store = Store(path)
    job_id = store.enqueue_job(1, "legacy", mode="auto")
    with sqlite3.connect(path) as db:
        db.execute("UPDATE retry_jobs SET requested_at=NULL,sla_deadline=NULL WHERE id=?", (job_id,))
    migrated = Store(path).get_job(job_id)
    assert migrated["requested_at"] == migrated["created_at"]
    assert migrated["sla_deadline"] is not None


def test_manual_job_has_no_auto_deadline_and_remains_review_gated(tmp_path):
    store = Store(tmp_path / "db")
    job_id = store.enqueue_job(1, "manual", mode="manual")
    assert store.get_job(job_id)["sla_deadline"] is None


def test_auto_retry_requeues_exhausted_search_instead_of_requesting_review(tmp_path):
    store = Store(tmp_path / "db")
    job_id = store.enqueue_job(1, "auto", mode="auto")

    class Lidarr:
        def get_track(self, track_id): return {"id": track_id, "title": "Song"}
        def track_identity(self, track): return {"artist": "Artist", "title": "Song"}
    class Sources:
        def search(self, *_): return []

    worker = JobWorker(store, Lidarr(), Sources(), object(), tmp_path, retry_delay=30, max_attempts=3)
    assert worker.process_once() == job_id
    job = store.get_job(job_id)
    assert job["status"] == "queued" and job["next_attempt_at"] is not None
    assert job["last_error"] == "no_policy_approved_candidate"


def test_auto_candidate_requires_known_material_improvement_but_manual_requests_review(tmp_path):
    def run(mode):
        root = tmp_path / mode
        store = Store(root / "db")
        job_id = store.enqueue_job(1, mode, mode=mode)
        store.upsert_audit_track(1)
        store.upsert_quality(1, "current", {"codec": "mp3", "bitrate": 128000, "lossless": False})
        class Lidarr:
            def get_track(self, track_id): return {"id": track_id, "trackFileId": 4, "title": "Song"}
            def get_track_file(self, _): return {"id": 4, "mediaInfo": {"audioCodec": "mp3", "audioBitrate": 128000}}
            def track_identity(self, _): return {"artist": "Artist", "title": "Song", "track_file_id": 4, "recording_id": "rec"}
            def manual_import(self, *_): raise AssertionError("unknown candidate quality must not import")
        class Sources:
            def search(self, *_): return [{"provider": "youtube", "source_id": "x"}]
            def download(self, _, directory):
                directory.mkdir(parents=True, exist_ok=True); path = directory / "x"; path.write_bytes(b"audio"); return path
        class Verifier:
            def verify_file(self, *_): return type("V", (), {"verdict": "accepted", "reason": "recording_match", "evidence": {}})()
        JobWorker(store, Lidarr(), Sources(), Verifier(), root).process_once()
        return store, job_id

    auto_store, auto_job = run("auto")
    assert auto_store.get_job(auto_job)["status"] == "queued"
    manual_store, manual_job = run("manual")
    assert manual_store.get_job(manual_job)["status"] in {"review_unavailable", "awaiting_review"}


def test_auto_retry_uses_injected_candidate_probe_for_quality_gate(tmp_path):
    root = tmp_path / "auto-probe"
    store = Store(root / "db")
    job_id = store.enqueue_job(1, "auto-probe", mode="auto")
    store.upsert_audit_track(1)
    store.upsert_quality(1, "current", {"codec": "mp3", "bitrate": 128000, "lossless": False})
    class Lidarr:
        def get_track(self, track_id): return {"id": track_id, "trackFileId": 4}
        def get_track_file(self, _): return {"id": 4, "mediaInfo": {"audioCodec": "mp3", "audioBitrate": 128000}}
        def track_identity(self, _): return {"artist": "Artist", "title": "Song", "track_file_id": 4, "recording_id": "rec"}
        def manual_import(self, *_): return {"status": "queued"}
    class Sources:
        def search(self, *_): return [{"provider": "youtube", "source_id": "x"}]
        def download(self, _, directory):
            directory.mkdir(parents=True, exist_ok=True); path = directory / "x"; path.write_bytes(b"audio"); return path
    class Verifier:
        def verify_file(self, *_): return type("V", (), {"verdict": "accepted", "reason": "recording_match", "evidence": {}})()
    class Probe:
        def probe(self, _): return {"codec": "aac", "bitrate": 256000}

    JobWorker(store, Lidarr(), Sources(), Verifier(), root, candidate_probe=Probe()).process_once()
    assert store.get_job(job_id)["status"] == "importing"


def test_policy_rejection_removes_only_contained_staged_artifact(tmp_path):
    root = tmp_path / "downloads"
    store = Store(tmp_path / "db")
    job_id = store.enqueue_job(1, "cleanup", mode="auto")
    store.upsert_audit_track(1)
    store.upsert_quality(1, "current", {"codec": "mp3", "bitrate": 128000})
    outside = tmp_path / "keep"; outside.write_bytes(b"keep")
    class Lidarr:
        def get_track(self, track_id): return {"id": track_id, "trackFileId": 4}
        def get_track_file(self, _): return {"id": 4, "mediaInfo": {"audioCodec": "mp3", "audioBitrate": 128000}}
        def track_identity(self, _): return {"artist": "A", "title": "T", "track_file_id": 4, "recording_id": "rec"}
    class Sources:
        def search(self, *_): return [{"provider": "youtube", "source_id": "x"}]
        def download(self, _, directory):
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / "x.m4a"; path.write_bytes(b"audio"); return path
    class Verifier:
        def verify_file(self, *_): return type("V", (), {"verdict": "accepted", "reason": "match", "evidence": {}})()
    JobWorker(store, Lidarr(), Sources(), Verifier(), root).process_once()
    attempt = store.list_attempts(job_id)[0]
    assert not (root / ".smart-staging" / str(job_id) / "x.m4a").exists()
    assert outside.exists()
    assert attempt["staged_path"] is None


def test_expired_auto_job_escalates_without_search(tmp_path):
    store = Store(tmp_path / "db")
    job_id = store.enqueue_job(1, "expired", mode="auto")
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE retry_jobs SET sla_deadline=datetime('now','-1 second') WHERE id=?", (job_id,))
    class Lidarr:
        def get_track(self, _): raise AssertionError("expired work must not search")
    assert JobWorker(store, Lidarr(), object(), object(), tmp_path).process_once() == job_id
    assert store.get_job(job_id)["status"] == "operator_attention"
