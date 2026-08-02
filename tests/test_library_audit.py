from datetime import datetime
from pathlib import Path

from smart_lidatube.audit import AuditConfig, AuditWorker, classify_verification, recheck_seconds
from smart_lidatube.fingerprint import Verification
from smart_lidatube.store import Store


def test_audit_schema_sanitizes_evidence_and_exemptions(tmp_path):
    store = Store(tmp_path / "audit.db")
    store.upsert_audit_track(7, priority_score=400)
    store.record_audit_result(7, "suspect", {"url": "https://user:secret@example/x", "error": "Bearer abc", "reason": "duration_mismatch"})
    row = store.get_audit_track(7)
    assert row["status"] == "suspect"
    assert "secret" not in str(row["evidence_json"]) and "Bearer" not in str(row["evidence_json"])
    assert row["evidence_json"]["reason"] == "duration_mismatch"
    store.set_audit_exemption(7, do_not_audit=True)
    assert store.list_eligible_audits() == []
    assert store.get_job(999) is None  # retry ledger remains separate


def test_classifications_and_backoff_are_typed():
    assert classify_verification(Verification("accepted", "recording_match", {})) == "verified"
    assert classify_verification(Verification("rejected", "recording_mismatch", {})) == "suspect"
    assert classify_verification(Verification("inconclusive", "fingerprint_error", {})) == "unverifiable"
    assert classify_verification(Verification("inconclusive", "low_score", {})) == "likely_correct"
    assert recheck_seconds("unverifiable", 3) > recheck_seconds("unverifiable", 1)
    assert recheck_seconds("exempt", 0) is None


def test_priority_and_fairness_select_oldest_every_fifth_slot(tmp_path):
    store = Store(tmp_path / "audit.db")
    store.upsert_audit_track(1, priority_score=1000)
    store.upsert_audit_track(2, priority_score=900)
    store.upsert_audit_track(3, priority_score=1)
    # Oldest record is selected by the reserved 20% fairness slot.
    store.set_audit_last_checked(1, "2025-01-01 00:00:00")
    store.set_audit_last_checked(2, "2024-01-01 00:00:00")
    store.set_audit_last_checked(3, "2000-01-01 00:00:00")
    selected = [store.select_audit_candidate(0.20)["lidarr_track_id"] for _ in range(5)]
    assert selected[:4] == [1, 1, 1, 1]
    assert selected[4] == 3


def test_budget_idle_guard_and_read_only_single_track_audit(tmp_path):
    class Lidarr:
        def get_track(self, track_id):
            return {"id": track_id, "trackFileId": 9, "title": "Song", "artist": {"artistName": "Artist"}, "duration": 120, "musicBrainzRecordingId": "rec"}
        def get_track_file(self, file_id):
            return {"id": file_id, "path": str(media), "size": media.stat().st_size}
        @staticmethod
        def track_identity(track):
            return {"recording_id": "rec", "duration": 120, "artist": "Artist", "title": "Song", "track_file_id": 9}
        def manual_import(self, *args):
            raise AssertionError("audit must not import")
    class Verifier:
        def verify_file(self, path, identity):
            assert Path(path) == media
            return Verification("accepted", "recording_match", {})
    media = tmp_path / "organized.m4a"; media.write_bytes(b"audio")
    store = Store(tmp_path / "audit.db"); store.upsert_audit_track(8, priority_score=100)
    worker = AuditWorker(store, Lidarr(), Verifier(), AuditConfig(budget_per_hour=1, max_token_bank=2))
    assert worker.process_once() == 8
    assert store.get_audit_track(8)["status"] == "verified"
    assert worker.process_once() is None  # token budget
    store.upsert_audit_track(10, priority_score=100)
    store.enqueue_job(99, "user-work")
    assert worker.process_once() is None  # queued retry wins


def test_missing_file_and_exception_never_persist_raw_error_or_side_effects(tmp_path):
    class Lidarr:
        def get_track(self, track_id): return {"id": track_id, "trackFileId": 2, "title": "Song", "artist": {"artistName": "Artist"}}
        def get_track_file(self, file_id): return {"id": file_id, "path": str(tmp_path / "missing")}
        @staticmethod
        def track_identity(track): return {"artist": "Artist", "title": "Song"}
    store = Store(tmp_path / "audit.db"); store.upsert_audit_track(1)
    assert AuditWorker(store, Lidarr(), object(), AuditConfig()).process_once() == 1
    row = store.get_audit_track(1)
    assert row["status"] == "unavailable"
    assert str(tmp_path) not in str(row["evidence_json"])


def test_daily_digest_dedupe_pagination_and_sanitization(tmp_path):
    from smart_lidatube.telegram import TelegramBot
    store = Store(tmp_path / "audit.db")
    for track in range(12):
        store.upsert_audit_track(track)
        store.record_audit_result(track, "suspect", {"artist": "A", "title": f"T{track}", "error": "https://x:token@y", "reason": "recording_mismatch"})
    sent = []
    bot = TelegramBot("token", store, {1}, {2}, request=lambda method, payload: sent.append((method, payload)) or {})
    today = datetime.now().strftime("%Y-%m-%d")
    assert bot.send_audit_digest(2, today) is True
    assert bot.send_audit_digest(2, today) is False
    payload = sent[0][1]
    assert "token" not in payload["text"] and "https:" not in payload["text"]
    callback = payload["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    assert bot.handle_callback({"id": "x", "from": {"id": 1}, "message": {"chat": {"id": 2}}, "data": callback}) is True
    assert any(method == "sendMessage" and "Page 1/2" in body["text"] for method, body in sent)
    assert bot.send_audit_digest(2, "2000-01-01") is False  # no changes for this date


def test_bootstrap_discovers_lidarr_library_in_bounded_batches_without_side_effects(tmp_path):
    class Lidarr:
        def __init__(self): self.calls = []
        def list_audit_tracks(self, cursor, limit):
            self.calls.append((cursor, limit))
            return ([{"id": 11, "trackFileId": 101}, {"id": 12}], 2) if cursor == 1 else ([{"id": 13, "trackFileId": 103}], None)
        def manual_import(self, *args): raise AssertionError("bootstrap must not import")
    store = Store(tmp_path / "audit.db")
    lidarr = Lidarr()
    worker = AuditWorker(store, lidarr, object(), AuditConfig(bootstrap_batch_size=2))
    assert worker.bootstrap_once() == 1
    assert store.get_audit_track(11) is not None
    assert store.get_audit_track(12) is None  # no organized file to audit
    assert worker.bootstrap_once() == 1
    assert store.get_audit_track(13) is not None
    assert lidarr.calls == [(1, 2), (2, 2)]


def test_bootstrap_falls_back_to_bounded_trackfile_enumeration_when_track_paging_is_unsupported(tmp_path):
    class Lidarr:
        def __init__(self):
            self.calls = []

        def list_audit_tracks(self, cursor, limit):
            self.calls.append((cursor, limit))
            if cursor == 1:
                raise RuntimeError("track endpoint rejects pagination")
            assert cursor == "files:1"
            return ([{"id": 21, "trackFileId": 201}], None)

    store = Store(tmp_path / "audit.db")
    lidarr = Lidarr()
    worker = AuditWorker(store, lidarr, object(), AuditConfig(bootstrap_batch_size=2))
    assert worker.bootstrap_once() == 0
    # The fallback cursor is durable; the next bounded read completes bootstrap.
    assert store.get_setting("audit_bootstrap_cursor") == "files:1"
    assert worker.bootstrap_once() == 1
    assert store.get_audit_track(21) is not None
    assert lidarr.calls == [(1, 2), ("files:1", 2)]


def test_remediation_queue_only_accepts_high_confidence_or_explicit_requests_and_is_deduplicated(tmp_path):
    store = Store(tmp_path / "audit.db")
    assert store.enqueue_remediation(1, "verified") is None
    first = store.enqueue_remediation(2, "missing_or_corrupt")
    assert first is not None
    assert store.enqueue_remediation(2, "missing_or_corrupt") == first
    assert store.enqueue_remediation(3, "explicit_request") is not None
    claimed = store.claim_remediation()
    assert {key: claimed[key] for key in ("lidarr_track_id", "reason")} == {"lidarr_track_id": 2, "reason": "missing_or_corrupt"}


def test_budgeted_remediation_dispatch_creates_manual_job_and_yields_to_normal_work(tmp_path):
    from smart_lidatube.remediation import RemediationDispatcher

    store = Store(tmp_path / "audit.db")
    store.enqueue_remediation(4, "recording_mismatch")
    dispatcher = RemediationDispatcher(store, budget_per_hour=1, max_token_bank=1)
    assert dispatcher.dispatch_once() is not None
    jobs = store.list_jobs_for_track(4)
    assert len(jobs) == 1 and jobs[0]["mode"] == "manual"
    assert jobs[0]["metadata"] == {"audit_remediation": "recording_mismatch"}

    store.enqueue_remediation(5, "missing_or_corrupt")
    store.enqueue_job(99, "normal-user-work")
    assert dispatcher.dispatch_once() is None


def test_high_confidence_audit_result_enqueues_remediation_but_exemption_defers_it(tmp_path):
    store = Store(tmp_path / "audit.db")
    store.record_audit_result(7, "suspect", {"reason": "recording_mismatch"})
    claimed = store.claim_remediation()
    assert {key: claimed[key] for key in ("lidarr_track_id", "reason")} == {"lidarr_track_id": 7, "reason": "recording_mismatch"}

    store.set_audit_exemption(8, do_not_upgrade=True)
    store.record_audit_result(8, "unavailable", {"reason": "target_file_missing"})
    assert store.claim_remediation() is None


def test_quality_policy_never_claims_youtube_is_upgrade_from_extension_or_bitrate_alone():
    from smart_lidatube.quality import quality_decision

    assert quality_decision({"codec": "flac", "verified": True}, {"codec": "m4a", "bitrate": 999}) == "rejected"
    assert quality_decision({"codec": "mp3", "bitrate": 128}, {"codec": "m4a", "bitrate": 320}) == "review_only"
    assert quality_decision({"codec": "mp3", "verified": True}, {"codec": "m4a"}, edition_match=False) == "rejected"


def test_token_is_not_consumed_until_a_candidate_exists(tmp_path):
    store = Store(tmp_path / "audit.db")
    clock = lambda: datetime(2026, 1, 1)
    worker = AuditWorker(store, object(), object(), AuditConfig(max_token_bank=2), clock=clock)
    assert worker.process_once() is None
    assert store.get_setting("audit_tokens") is None


def test_remediation_claim_reserves_before_token_and_persists_job_mapping(tmp_path):
    from smart_lidatube.remediation import RemediationDispatcher

    store = Store(tmp_path / "audit.db")
    dispatcher = RemediationDispatcher(store, budget_per_hour=1, max_token_bank=1)
    assert dispatcher.dispatch_once() is None
    assert store.get_setting("remediation_search_tokens") is None

    queue_id = store.enqueue_remediation(7, "recording_mismatch")
    job_id = dispatcher.dispatch_once()
    queue = store.get_remediation(queue_id)
    assert job_id == queue["job_id"]
    assert queue["status"] == "queued"
    store.update_job(job_id, "awaiting_review")
    assert store.get_remediation(queue_id)["status"] == "awaiting_review"


def test_remediation_claim_is_released_when_no_search_token(tmp_path):
    from smart_lidatube.remediation import RemediationDispatcher

    store = Store(tmp_path / "audit.db")
    queue_id = store.enqueue_remediation(7, "recording_mismatch")
    dispatcher = RemediationDispatcher(store, budget_per_hour=0)
    assert dispatcher.dispatch_once() is None
    assert store.get_remediation(queue_id)["status"] == "eligible"


def test_digest_records_only_status_changes_and_uses_explicit_local_day(tmp_path):
    store = Store(tmp_path / "audit.db")
    store.record_audit_result(1, "verified", {"reason": "recording_match"}, audit_local_day="2026-01-01")
    store.record_audit_result(1, "verified", {"reason": "recording_match"}, audit_local_day="2026-01-02")
    store.record_audit_result(1, "suspect", {"reason": "recording_mismatch"}, audit_local_day="2026-01-02")
    assert [event["result_status"] for event in store.audit_digest_events("2026-01-01")] == ["verified"]
    assert [event["result_status"] for event in store.audit_digest_events("2026-01-02")] == ["suspect"]
