"""Read-only library integrity scheduling and verification."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

AUDIT_STATUSES = {"never_checked", "verified", "likely_correct", "suspect", "unverifiable", "unavailable", "exempt"}

@dataclass
class AuditConfig:
    enabled: bool = True
    budget_per_hour: int = 12
    max_token_bank: int = 24
    fairness_share: float = .20
    bootstrap_batch_size: int = 100
    timezone: str = "UTC"

def classify_verification(result):
    if result.verdict == "accepted": return "verified"
    if result.verdict == "rejected" or result.reason == "duration_mismatch": return "suspect"
    if result.reason in {"fingerprint_error", "acoustid_disabled"}: return "unverifiable"
    return "likely_correct"

def recheck_seconds(status, count=0):
    if status == "verified": return 60 * 60 * 24 * 270
    if status == "likely_correct": return 60 * 60 * 24 * 75
    if status == "unverifiable": return 60 * 60 * 24 * min(56, 7 * (2 ** min(count, 3)))
    if status == "suspect": return 60 * 60 * 24 * 30
    if status == "unavailable": return 60 * 60 * 24 * 7
    return None

class AuditWorker:
    """Audits an already-organized Lidarr target, without source or import APIs."""
    def __init__(self, store, lidarr, verifier, config=None, clock=None):
        self.store, self.lidarr, self.verifier = store, lidarr, verifier
        self.config, self.clock = config or AuditConfig(), clock or (lambda: datetime.now(timezone.utc))

    def _token(self):
        now=self.clock().timestamp(); raw=self.store.get_setting("audit_tokens")
        tokens, updated = (self.config.max_token_bank, now) if not raw else map(float, raw.split(":"))
        tokens=min(self.config.max_token_bank, tokens+(now-updated)*self.config.budget_per_hour/3600)
        if tokens < 1:
            self.store.set_setting("audit_tokens",f"{tokens}:{now}"); return False
        self.store.set_setting("audit_tokens",f"{tokens-1}:{now}"); return True

    def bootstrap_once(self):
        """Read one Lidarr page and add only tracks that already have a file."""
        enumerate_tracks = getattr(self.lidarr, "list_audit_tracks", None)
        if not enumerate_tracks:
            return 0
        cursor = int(self.store.get_setting("audit_bootstrap_cursor", "1"))
        tracks, next_cursor = enumerate_tracks(cursor, self.config.bootstrap_batch_size)
        added = 0
        for track in tracks:
            if track.get("id") is not None and track.get("trackFileId"):
                self.store.upsert_audit_track(track["id"])
                added += 1
        self.store.set_setting("audit_bootstrap_cursor", next_cursor if next_cursor is not None else 1)
        return added

    def process_once(self):
        if not self.config.enabled or self.store.regular_work_pending(): return None
        row=self.store.select_audit_candidate(self.config.fairness_share)
        if not row or not self._token(): return None
        track_id=row["lidarr_track_id"]
        try:
            track=self.lidarr.get_track(track_id); identity=self.lidarr.track_identity(track)
            file_id=identity.get("track_file_id") or track.get("trackFileId")
            target=self.lidarr.get_track_file(file_id) if file_id else None
            path=Path((target or {}).get("path", ""))
            if not path.is_file():
                self._save(track_id,"unavailable",{"reason":"target_file_missing","artist":identity.get("artist", ""),"title":identity.get("title", "")},count=row["check_count"]); return track_id
            marker=f"{path.stat().st_size}:{int(path.stat().st_mtime)}"
            result=self.verifier.verify_file(path,identity); status=classify_verification(result)
            self._save(track_id,status,{"reason":result.reason,"artist":identity.get("artist", ""),"title":identity.get("title", "")},marker,row["check_count"])
        except Exception:
            self._save(track_id,"unverifiable",{"reason":"audit_system_error"},count=row["check_count"])
        return track_id

    def _save(self,track_id,status,evidence,marker=None,count=0):
        seconds=recheck_seconds(status,count)
        next_at=(self.clock()+timedelta(seconds=seconds)).strftime("%Y-%m-%d %H:%M:%S") if seconds else None
        local_day=self.clock().astimezone(ZoneInfo(self.config.timezone)).date().isoformat()
        self.store.record_audit_result(track_id,status,evidence,next_at,marker,error_code=evidence.get("reason"),audit_local_day=local_day)
