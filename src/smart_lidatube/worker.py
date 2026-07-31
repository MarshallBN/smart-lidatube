"""Autonomous targeted replacement job lifecycle."""

from pathlib import Path

from smart_lidatube.retry import filter_candidates


class JobWorker:
    def __init__(
        self,
        store,
        lidarr,
        sources,
        verifier,
        downloads_root,
        telegram=None,
        review_chat_id=None,
    ):
        self.store = store
        self.lidarr = lidarr
        self.sources = sources
        self.verifier = verifier
        self.downloads_root = Path(downloads_root)
        self.telegram = telegram
        self.review_chat_id = review_chat_id

    def process_once(self):
        job = self.store.claim_job()
        if job is None:
            return None
        try:
            self._process(job)
        except Exception as exc:
            self.store.update_job(job["id"], "failed", error=str(exc))
        return job["id"]

    def _process(self, job):
        accepted = self._accepted_attempt(job["id"])
        track = self.lidarr.get_track(job["lidarr_track_id"])
        if accepted:
            self._import(job, track, accepted)
            return

        identity = self.lidarr.track_identity(track)
        candidates = self.sources.search(identity["artist"], identity["title"])
        candidates = filter_candidates(
            self.store, job["lidarr_track_id"], candidates
        )
        for candidate in candidates:
            attempt_id = self.store.add_attempt(
                job["id"],
                candidate["provider"],
                candidate["source_id"],
                provenance=candidate,
            )
            attempt = self.store.get_attempt(attempt_id)
            if attempt["verdict"] in {"rejected", "manual_rejected"}:
                continue
            try:
                staged = self.sources.download(
                    candidate,
                    self.downloads_root / ".smart-staging" / str(job["id"]),
                )
                verification = self.verifier.verify_file(staged, identity)
            except Exception as exc:
                self.store.update_attempt(
                    attempt_id,
                    verdict="download_failed",
                    evidence={"error": str(exc)},
                )
                continue
            self.store.update_attempt(
                attempt_id,
                verdict=verification.verdict,
                evidence={
                    "reason": verification.reason,
                    **verification.evidence,
                },
                staged_path=staged,
            )
            if verification.verdict == "rejected":
                self.store.reject(
                    job["lidarr_track_id"],
                    candidate["provider"],
                    candidate["source_id"],
                    attempt_id,
                )
                continue
            if verification.verdict == "accepted" and job["mode"] == "auto":
                self._import(job, track, self.store.get_attempt(attempt_id))
                return
            self._request_review(job, attempt_id, candidate, verification)
            return
        self.store.update_job(job["id"], "exhausted", error="no candidates remain")

    def _accepted_attempt(self, job_id):
        for attempt in reversed(self.store.list_attempts(job_id)):
            if attempt["verdict"] == "manual_accepted" and attempt.get("staged_path"):
                return attempt
        return None

    def _request_review(self, job, attempt_id, candidate, verification):
        self.store.update_attempt(attempt_id, verdict="awaiting_review")
        self.store.update_job(job["id"], "awaiting_review")
        if self.telegram and self.review_chat_id is not None:
            text = (
                f"Smart retry job {job['id']}\n"
                f"{candidate.get('artist', '')} - {candidate.get('title', '')}\n"
                f"Verification: {verification.verdict} ({verification.reason})"
            )
            self.telegram.send_review(self.review_chat_id, attempt_id, text)

    def _import(self, job, track, attempt):
        path = attempt.get("staged_path")
        if not path or not Path(path).is_file():
            raise RuntimeError("accepted staged candidate is missing")
        result = self.lidarr.manual_import(path, track)
        succeeded = getattr(self.lidarr, "import_succeeded", lambda value: value is not False)
        if not succeeded(result):
            raise RuntimeError(f"Lidarr manual import failed: {result}")
        self.store.update_attempt(attempt["id"], verdict="imported")
        self.store.update_job(job["id"], "completed")
