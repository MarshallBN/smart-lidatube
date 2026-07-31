"""Autonomous targeted replacement job lifecycle."""
import shutil
from pathlib import Path

from smart_lidatube.retry import filter_candidates


def translate_staged_path(path, downloads_root, lidarr_downloads_root):
    """Translate a real, contained worker path to Lidarr's explicit volume root."""
    root=Path(downloads_root).resolve()
    try:
        candidate=Path(path).resolve(strict=True)
    except FileNotFoundError as exc:
        raise ValueError("staged path does not exist") from exc
    try: relative=candidate.relative_to(root)
    except ValueError as exc: raise ValueError("staged path escapes DOWNLOADS_ROOT") from exc
    return Path(lidarr_downloads_root) / relative


class JobWorker:
    def __init__(self,store,lidarr,sources,verifier,downloads_root,telegram=None,
                 review_chat_id=None,lidarr_downloads_root=None,lease_seconds=300,
                 retry_delay=30,max_attempts=5):
        self.store=store; self.lidarr=lidarr; self.sources=sources; self.verifier=verifier
        self.downloads_root=Path(downloads_root); self.lidarr_downloads_root=Path(lidarr_downloads_root or downloads_root)
        self.telegram=telegram; self.review_chat_id=review_chat_id
        self.lease_seconds=lease_seconds; self.retry_delay=retry_delay; self.max_attempts=max_attempts

    def process_once(self):
        job=self.store.claim_job(self.lease_seconds)
        if job is None:return None
        try:self._process(job)
        except Exception as exc:self.store.schedule_retry(job["id"],str(exc),self.retry_delay,self.max_attempts)
        return job["id"]

    def reconcile_imports(self):
        completed=0
        for job in self.store.list_importing():
            try: track=self.lidarr.get_track(job["lidarr_track_id"])
            except Exception: continue
            current=track.get("trackFileId")
            # A changed associated file is the strongest practical Lidarr signal.
            # Missing staged source plus an associated file also covers Lidarr move.
            local=self._local_submitted(job.get("submitted_path"))
            if current and (current != job.get("prior_track_file_id") or (local and not local.exists())):
                attempts=self.store.list_attempts(job["id"])
                imported=next((a for a in reversed(attempts) if a.get("staged_path")),None)
                if imported:self.store.update_attempt(imported["id"],verdict="imported")
                self.store.update_job(job["id"],"completed")
                if local:
                    directory=local.parent
                    if directory.exists() and self.downloads_root.resolve() in directory.resolve().parents:
                        shutil.rmtree(directory,ignore_errors=True)
                completed += 1
        return completed

    def _local_submitted(self,submitted):
        if not submitted:return None
        try: rel=Path(submitted).relative_to(self.lidarr_downloads_root)
        except ValueError:return None
        return self.downloads_root/rel

    def _process(self,job):
        accepted=self._accepted_attempt(job["id"]); track=self.lidarr.get_track(job["lidarr_track_id"])
        if accepted:self._import(job,track,accepted);return
        identity=self.lidarr.track_identity(track); file_id=identity.get("track_file_id")
        if file_id:
            try:identity["current_track_file"]=self.lidarr.get_track_file(file_id)
            except Exception as exc:identity["current_track_file_error"]=str(exc)
        candidates=filter_candidates(self.store,job["lidarr_track_id"],self.sources.search(identity["artist"],identity["title"]))
        for candidate in candidates:
            attempt_id=self.store.add_attempt(job["id"],candidate["provider"],candidate["source_id"],candidate)
            attempt=self.store.get_attempt(attempt_id)
            if attempt["verdict"] in {"rejected","manual_rejected"}:continue
            try:
                staged=self.sources.download(candidate,self.downloads_root/".smart-staging"/str(job["id"]))
            except Exception as exc:
                # Infrastructure failures are resumable and do not burn candidates.
                self.store.update_attempt(attempt_id,verdict="pending",evidence={"infrastructure_error":str(exc)})
                raise
            verification=self.verifier.verify_file(staged,identity)
            self.store.update_attempt(attempt_id,verdict=verification.verdict,evidence={"reason":verification.reason,**verification.evidence},staged_path=staged)
            if verification.verdict=="rejected":
                self.store.reject(job["lidarr_track_id"],candidate["provider"],candidate["source_id"],attempt_id);continue
            if verification.verdict=="accepted" and job["mode"]=="auto":self._import(job,track,self.store.get_attempt(attempt_id));return
            self._request_review(job,attempt_id,candidate,verification);return
        self.store.update_job(job["id"],"exhausted",error="no candidates remain")

    def _accepted_attempt(self,job_id):
        return next((a for a in reversed(self.store.list_attempts(job_id)) if a["verdict"]=="manual_accepted" and a.get("staged_path")),None)

    def _request_review(self,job,attempt_id,candidate,verification):
        self.store.update_attempt(attempt_id,verdict="awaiting_review")
        if not self.telegram or self.review_chat_id is None:
            self.store.update_job(job["id"],"review_unavailable",error="manual review requires configured Telegram")
            return
        self.store.update_job(job["id"],"awaiting_review")
        text=f"Smart retry job {job['id']}\n{candidate.get('artist','')} - {candidate.get('title','')}\nVerification: {verification.verdict} ({verification.reason})"
        try:
            if not self.telegram.send_review(self.review_chat_id,attempt_id,text):raise RuntimeError("Telegram review chat rejected")
        except Exception as exc:
            self.store.update_job(job["id"],"notification_pending",error=str(exc))

    def _import(self,job,track,attempt):
        local=Path(attempt.get("staged_path") or "")
        if not local.is_file() or local.stat().st_size<=0:raise RuntimeError("accepted staged candidate is missing")
        visible=translate_staged_path(local,self.downloads_root,self.lidarr_downloads_root)
        prior=track.get("trackFileId")
        # Persist the importing barrier before the HTTP request. A process death
        # can leave a conservative resumable reconciliation, never a duplicate POST.
        self.store.begin_import(job["id"],prior,visible,{"status":"submission_pending"})
        result=self.lidarr.manual_import(visible,track)
        succeeded=getattr(self.lidarr,"import_succeeded",lambda value:value is not False)
        self.store.record_import_result(job["id"],result)
        if not succeeded(result):
            self.store.update_job(job["id"],"failed",error=f"Lidarr manual import failed: {result}")
