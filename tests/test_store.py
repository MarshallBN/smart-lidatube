import sqlite3
from smart_lidatube.store import Store


def test_jobs_attempts_rejections_and_idempotency(tmp_path):
    store = Store(tmp_path / "smart.db")
    job = store.enqueue_job(42, "playlist:retry:song-1", mode="auto", metadata={"path": "Artist/x.mp3"})
    assert store.enqueue_job(42, "playlist:retry:song-1", mode="auto") == job
    attempt = store.add_attempt(job, "youtube", "abc", provenance={"url": "https://youtu.be/abc"})
    store.set_attempt_verdict(attempt, "wrong", {"reason": "recording_mismatch"})
    store.reject(42, "youtube", "abc", attempt)
    assert store.is_rejected(42, "youtube", "abc")
    assert not store.is_rejected(43, "youtube", "abc")
    assert store.get_attempt(attempt)["evidence"]["reason"] == "recording_mismatch"
    assert store.get_job(job)["metadata"]["path"] == "Artist/x.mp3"


def test_unknown_prior_source_is_explicit(tmp_path):
    store = Store(tmp_path / "smart.db")
    job = store.enqueue_job(8, "api:8", metadata={})
    assert store.get_job(job)["prior_source"] == "unknown"
