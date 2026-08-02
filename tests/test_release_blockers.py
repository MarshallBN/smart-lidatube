import sqlite3
import time
from pathlib import Path

from smart_lidatube.healthcheck import worker_is_healthy
from smart_lidatube.runner import build_components
from smart_lidatube.store import Store
from smart_lidatube.worker import JobWorker


def age_job(store, job_id, column, seconds):
    with sqlite3.connect(store.path) as db:
        db.execute(
            f"UPDATE retry_jobs SET {column}=datetime('now', ?) WHERE id=?",
            (f"-{seconds} seconds", job_id),
        )


def test_compose_overrides_worker_healthcheck_but_not_web():
    text = (
        Path(__file__).parents[1] / "docker-compose.smart.example.yml"
    ).read_text()
    web, worker = text.split("  smart-worker:", 1)
    assert "healthcheck:" not in web  # it retains the Dockerfile HTTP probe
    assert 'test: ["CMD", "python", "-m", "smart_lidatube.healthcheck"]' in worker


def test_worker_health_uses_fresh_persisted_heartbeat(tmp_path):
    store = Store(tmp_path / "smart.db")
    assert not worker_is_healthy(store.path, max_age=30)
    store.set_setting("worker_status", "running")
    store.set_setting("worker_heartbeat", str(time.time()))
    assert worker_is_healthy(store.path, max_age=30)
    store.set_setting("worker_heartbeat", str(time.time() - 31))
    assert not worker_is_healthy(store.path, max_age=30)
    store.set_setting("worker_status", "stopped")
    store.set_setting("worker_heartbeat", str(time.time()))
    assert not worker_is_healthy(store.path, max_age=30)


def test_import_phases_are_durable_and_prepared_is_not_resubmitted(tmp_path):
    store = Store(tmp_path / "db")
    job = store.enqueue_job(7, "prepared")
    store.prepare_import(job, 10, "/visible/a.m4a")
    saved = store.get_job(job)
    assert saved["import_phase"] == "prepared"
    assert saved["import_prepared_at"] and saved["import_submitted_at"] is None

    age_job(store, job, "import_prepared_at", 11)

    class Lidarr:
        def get_track(self, _):
            raise AssertionError("prepared request outcome is unknowable; do not resubmit")

    worker = JobWorker(
        store, Lidarr(), None, None, tmp_path,
        import_verify_interval=10, import_verify_timeout=60,
    )
    assert worker.reconcile_imports() == 0
    saved = store.get_job(job)
    assert saved["status"] == "import_attention"
    assert "prepared" in saved["last_error"].lower()


def test_manual_import_exception_after_prepared_barrier_requires_attention(tmp_path):
    store = Store(tmp_path / "db")
    job = store.enqueue_job(7, "manual-import-exception")
    staged = tmp_path / ".smart-staging" / str(job) / "candidate.m4a"
    staged.parent.mkdir(parents=True)
    staged.write_bytes(b"audio")
    attempt = store.add_attempt(job, "youtube", "candidate")
    store.update_attempt(attempt, verdict="manual_accepted", staged_path=staged)

    class Lidarr:
        def get_track(self, _):
            return {"id": 7, "trackFileId": 10}

        def manual_import(self, _, __):
            raise RuntimeError("album release lookup failed at https://user:secret@lidarr")

    worker = JobWorker(store, Lidarr(), None, None, tmp_path)
    assert worker.process_once() == job
    saved = store.get_job(job)
    assert saved["status"] == "import_attention"
    assert saved["import_phase"] == "prepared"
    assert saved["import_submitted_at"] is None
    assert saved["last_error"] == (
        "Lidarr manual import submission failed: "
        "album release lookup failed at https://user:***@lidarr"
    )
    assert "secret" not in saved["last_error"]


def test_submitted_import_times_out_and_never_uses_missing_stage_as_success(tmp_path):
    store = Store(tmp_path / "db")
    job = store.enqueue_job(7, "submitted")
    store.prepare_import(job, 10, "/visible/a.m4a")
    store.mark_import_submitted(job, {"status": "queued"})
    age_job(store, job, "import_submitted_at", 61)

    class Lidarr:
        def get_track(self, _):
            return {"id": 7, "trackFileId": 10}

    worker = JobWorker(
        store, Lidarr(), None, None, tmp_path,
        lidarr_downloads_root="/visible",
        import_verify_interval=0, import_verify_timeout=60,
    )
    assert worker.reconcile_imports() == 0
    saved = store.get_job(job)
    assert saved["status"] == "import_attention"
    assert "timed out" in saved["last_error"].lower()


def test_import_without_prior_id_requires_inspectable_new_trackfile(tmp_path):
    store = Store(tmp_path / "db")
    job = store.enqueue_job(7, "no-prior")
    store.prepare_import(job, None, "/visible/a.m4a")
    store.mark_import_submitted(job, {"status": "queued"})

    class Lidarr:
        def __init__(self):
            self.inspectable = False

        def get_track(self, _):
            return {"id": 7, "trackFileId": 22}

        def get_track_file(self, file_id):
            assert file_id == 22
            if not self.inspectable:
                raise RuntimeError("not indexed yet")
            return {"id": 22, "path": "/music/A/S.m4a"}

    lidarr = Lidarr()
    worker = JobWorker(store, lidarr, None, None, tmp_path, import_verify_interval=0)
    assert worker.reconcile_imports() == 0
    assert store.get_job(job)["status"] == "importing"
    lidarr.inspectable = True
    assert worker.reconcile_imports() == 1
    assert store.get_job(job)["status"] == "completed"


def test_notification_pending_is_reconstructed_and_retried(tmp_path):
    store = Store(tmp_path / "db")
    job = store.enqueue_job(1, "notify", mode="manual")
    attempt = store.add_attempt(job, "youtube", "abc")
    store.update_attempt(attempt, staged_path="/stage/a.m4a")
    store.prepare_notification(job, attempt, 9, "review text", {"reason": "inconclusive"})

    class Telegram:
        def __init__(self):
            self.calls = []

        def send_review(self, chat_id, attempt_id, text):
            self.calls.append((chat_id, attempt_id, text))
            return True

    telegram = Telegram()
    worker = JobWorker(store, None, None, None, tmp_path, telegram=telegram)
    assert worker.retry_notifications() == 1
    assert telegram.calls == [(9, attempt, "review text")]
    assert store.get_job(job)["status"] == "awaiting_review"
    assert store.get_attempt(attempt)["verdict"] == "awaiting_review"
    assert worker.retry_notifications() == 0


def test_runner_wires_all_worker_lifecycle_settings(monkeypatch, tmp_path):
    values = {
        "SMART_DB_PATH": str(tmp_path / "db"),
        "SMART_CLAIM_TIMEOUT": "41",
        "SMART_RETRY_DELAY": "7",
        "SMART_MAX_ATTEMPTS": "8",
        "SMART_IMPORT_VERIFY_INTERVAL": "3",
        "SMART_IMPORT_VERIFY_TIMEOUT": "17",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    worker, _, _ = build_components()
    assert worker.lease_seconds == 41
    assert worker.retry_delay == 7
    assert worker.max_attempts == 8
    assert worker.import_verify_interval == 3
    assert worker.import_verify_timeout == 17


def test_build_components_retains_three_item_public_return(monkeypatch, tmp_path):
    monkeypatch.setenv("SMART_DB_PATH", str(tmp_path / "db"))
    components = build_components()
    assert len(components) == 3
    assert hasattr(components[0], "audit_worker")
    assert hasattr(components[0], "remediation_dispatcher")
