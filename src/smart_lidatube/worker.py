"""Autonomous targeted replacement job lifecycle."""
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from smart_lidatube.retry import filter_candidates


def translate_staged_path(path, downloads_root, lidarr_downloads_root):
    """Translate a real, contained worker path to Lidarr's explicit volume root."""
    root = Path(downloads_root).resolve()
    try:
        candidate = Path(path).resolve(strict=True)
    except FileNotFoundError as exc:
        raise ValueError("staged path does not exist") from exc
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("staged path escapes DOWNLOADS_ROOT") from exc
    return Path(lidarr_downloads_root) / relative


class JobWorker:
    def __init__(
        self, store, lidarr, sources, verifier, downloads_root, telegram=None,
        review_chat_id=None, lidarr_downloads_root=None, lease_seconds=300,
        retry_delay=30, max_attempts=5, import_verify_interval=10,
        import_verify_timeout=900,
    ):
        self.store = store
        self.lidarr = lidarr
        self.sources = sources
        self.verifier = verifier
        self.downloads_root = Path(downloads_root)
        self.lidarr_downloads_root = Path(lidarr_downloads_root or downloads_root)
        self.telegram = telegram
        self.review_chat_id = review_chat_id
        self.lease_seconds = lease_seconds
        self.retry_delay = retry_delay
        self.max_attempts = max_attempts
        self.import_verify_interval = import_verify_interval
        self.import_verify_timeout = import_verify_timeout

    def process_once(self):
        job = self.store.claim_job(self.lease_seconds)
        if job is None:
            return None
        try:
            self._process(job)
        except Exception as exc:
            # Never retry the whole job after the durable pre-POST barrier.
            if self.store.get_job(job["id"])["status"] == "processing":
                self.store.schedule_retry(
                    job["id"], str(exc), self.retry_delay, self.max_attempts
                )
        return job["id"]

    @staticmethod
    def _age(value):
        if not value:
            return float("inf")
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - parsed).total_seconds()

    def reconcile_imports(self):
        completed = 0
        for job in self.store.list_importing():
            phase = job.get("import_phase")
            if phase == "prepared":
                if self._age(job.get("import_prepared_at")) > max(
                    5, self.import_verify_interval
                ):
                    self.store.update_job(
                        job["id"],
                        "import_attention",
                        error=(
                            "Import remained prepared; submission outcome is unknown "
                            "and was not resubmitted"
                        ),
                    )
                continue
            if phase != "submitted":
                self.store.update_job(
                    job["id"], "import_attention",
                    error="Import has no durable submission phase",
                )
                continue
            if self._age(job.get("import_submitted_at")) >= self.import_verify_timeout:
                self.store.update_job(
                    job["id"], "import_attention",
                    error="Lidarr import verification timed out",
                )
                continue
            if self._age(job.get("import_checked_at")) < self.import_verify_interval:
                continue
            try:
                track = self.lidarr.get_track(job["lidarr_track_id"])
            except Exception:
                self.store.mark_import_checked(job["id"])
                continue

            current = track.get("trackFileId")
            prior = job.get("prior_track_file_id")
            confirmed = bool(current and prior is not None and current != prior)
            if current and prior is None:
                try:
                    new_file = self.lidarr.get_track_file(current)
                    confirmed = bool(new_file and new_file.get("id") == current)
                except Exception:
                    confirmed = False
            self.store.mark_import_checked(job["id"])
            if confirmed:
                local = self._local_submitted(job.get("submitted_path"))
                attempts = self.store.list_attempts(job["id"])
                imported = next(
                    (a for a in reversed(attempts) if a.get("staged_path")), None
                )
                if imported:
                    self.store.update_attempt(imported["id"], verdict="imported")
                self.store.update_job(job["id"], "completed")
                if local:
                    directory = local.parent
                    if (
                        directory.exists()
                        and self.downloads_root.resolve() in directory.resolve().parents
                    ):
                        shutil.rmtree(directory, ignore_errors=True)
                completed += 1
        return completed

    def retry_notifications(self):
        delivered = 0
        if not self.telegram:
            return delivered
        for job in self.store.list_pending_notifications():
            try:
                sent = self.telegram.send_review(
                    job["notification_chat_id"],
                    job["notification_attempt_id"],
                    job["notification_text"],
                )
                if not sent:
                    raise RuntimeError("Telegram review chat rejected")
                if self.store.notification_delivered(
                    job["id"], job["notification_attempt_id"]
                ):
                    delivered += 1
            except Exception as exc:
                self.store.update_job(
                    job["id"], "notification_pending", error=str(exc)
                )
        return delivered

    def _local_submitted(self, submitted):
        if not submitted:
            return None
        try:
            relative = Path(submitted).relative_to(self.lidarr_downloads_root)
        except ValueError:
            return None
        return self.downloads_root / relative

    def _process(self, job):
        accepted = self._accepted_attempt(job["id"])
        track = self.lidarr.get_track(job["lidarr_track_id"])
        if accepted:
            self._import(job, track, accepted)
            return
        identity = self.lidarr.track_identity(track)
        file_id = identity.get("track_file_id")
        if file_id:
            try:
                identity["current_track_file"] = self.lidarr.get_track_file(file_id)
            except Exception as exc:
                identity["current_track_file_error"] = str(exc)
        candidates = filter_candidates(
            self.store, job["lidarr_track_id"],
            self.sources.search(identity["artist"], identity["title"]),
        )
        for candidate in candidates:
            attempt_id = self.store.add_attempt(
                job["id"], candidate["provider"], candidate["source_id"], candidate
            )
            attempt = self.store.get_attempt(attempt_id)
            if attempt["verdict"] in {"rejected", "manual_rejected"}:
                continue
            try:
                staged = self.sources.download(
                    candidate, self.downloads_root / ".smart-staging" / str(job["id"])
                )
            except Exception as exc:
                self.store.update_attempt(
                    attempt_id, verdict="pending",
                    evidence={"infrastructure_error": str(exc)},
                )
                raise
            verification = self.verifier.verify_file(staged, identity)
            self.store.update_attempt(
                attempt_id, verdict=verification.verdict,
                evidence={"reason": verification.reason, **verification.evidence},
                staged_path=staged,
            )
            if verification.verdict == "rejected":
                self.store.reject(
                    job["lidarr_track_id"], candidate["provider"],
                    candidate["source_id"], attempt_id,
                )
                continue
            if verification.verdict == "accepted" and job["mode"] == "auto":
                self._import(job, track, self.store.get_attempt(attempt_id))
                return
            self._request_review(job, attempt_id, candidate, verification)
            return
        self.store.update_job(job["id"], "exhausted", error="no candidates remain")

    def _accepted_attempt(self, job_id):
        return next(
            (
                attempt for attempt in reversed(self.store.list_attempts(job_id))
                if attempt["verdict"] == "manual_accepted"
                and attempt.get("staged_path")
            ),
            None,
        )

    def _request_review(self, job, attempt_id, candidate, verification):
        if not self.telegram or self.review_chat_id is None:
            self.store.update_attempt(attempt_id, verdict="review_unavailable")
            self.store.update_job(
                job["id"], "review_unavailable",
                error="manual review requires configured Telegram",
            )
            return
        text = (
            f"Smart retry job {job['id']}\n"
            f"{candidate.get('artist', '')} - {candidate.get('title', '')}\n"
            f"Verification: {verification.verdict} ({verification.reason})"
        )
        evidence = {
            "candidate": candidate,
            "verdict": verification.verdict,
            "reason": verification.reason,
        }
        self.store.prepare_notification(
            job["id"], attempt_id, self.review_chat_id, text, evidence
        )
        self.retry_notifications()

    @staticmethod
    def _safe_error(exc):
        """Keep diagnostic errors useful without persisting credentials in URLs."""
        return re.sub(r"(?i)(https?://[^/@\s]+:)[^@\s]+@", r"\1***@", str(exc))

    def _import(self, job, track, attempt):
        local = Path(attempt.get("staged_path") or "")
        if not local.is_file() or local.stat().st_size <= 0:
            raise RuntimeError("accepted staged candidate is missing")
        visible = translate_staged_path(
            local, self.downloads_root, self.lidarr_downloads_root
        )
        prior = track.get("trackFileId")
        self.store.prepare_import(job["id"], prior, visible)
        try:
            result = self.lidarr.manual_import(visible, track)
        except Exception as exc:
            self.store.update_job(
                job["id"], "import_attention",
                error=(
                    "Lidarr manual import submission failed: "
                    f"{self._safe_error(exc)}"
                ),
            )
            return
        succeeded = getattr(
            self.lidarr, "import_succeeded", lambda value: value is not False
        )
        self.store.mark_import_submitted(job["id"], result)
        if not succeeded(result):
            self.store.update_job(
                job["id"], "failed",
                error=f"Lidarr manual import failed: {result}",
            )
